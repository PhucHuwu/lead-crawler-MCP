"""Tests for the processing pipeline.

The pipeline owns the two behaviours that are easy to get wrong and expensive to
debug in production: a failing source must not take the run down with it, and
every dropped lead must be traceable to the stage that dropped it.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from typing import Any

import pytest

from src.config import FilterSettings, Settings, load_settings
from src.crawlers.base import BaseCrawler
from src.models.enums import DedupStrategy, RejectionReason, SeniorityLevel
from src.models.lead import RawLead, StandardizedLead
from src.models.results import MAX_RECORDED_REJECTIONS, CrawlResult
from src.processors import Pipeline
from src.processors.filters import LeadFilter
from src.processors.validator import LeadValidator
from src.utils.errors import CrawlerError, SourceAuthError, SourceUnavailableError
from tests.conftest import make_raw_lead


class FakeCrawler(BaseCrawler):
    """A source whose behaviour a test can dictate exactly."""

    provider = "fake"
    display_name = "Fake"

    def __init__(
        self,
        settings: Settings,
        leads: Sequence[RawLead] = (),
        *,
        error: BaseException | None = None,
        provider: str = "fake",
    ) -> None:
        super().__init__(settings)
        # ``provider`` is a ClassVar on the base class (it names the adapter, not
        # the instance), but a test double needs to vary it per instance.
        self.provider = provider  # type: ignore[misc]
        self._leads = list(leads)
        self._error = error
        self.requested_limit: int | None = None

    async def crawl(self, limit: int) -> list[RawLead]:
        self.requested_limit = limit
        if self._error is not None:
            raise self._error
        return self._leads[:limit]


def rich_raw(provider: str = "a", **overrides: Any) -> RawLead:
    """A record that survives validation with something left over to enrich."""
    defaults: dict[str, Any] = {
        "provider": provider,
        "external_id": f"{provider}-rich",
        "first_name": "Ada",
        "last_name": "Lovelace",
        "job_title": "VP of Engineering",
        "email": "ada@acme.com",
        "phone": "+14155550142",
        "linkedin_url": "https://www.linkedin.com/in/ada",
        "company_name": "Acme Corp",
        "company_domain": "acme.com",
        "company_industry": "Software",
        "company_employee_count": "250",
        "company_country": "United States",
        "company_city": "Austin",
    }
    defaults.update(overrides)
    return RawLead(**defaults)


def sparse_raw(provider: str = "a", **overrides: Any) -> RawLead:
    """A record that is valid but thin: name plus a company signal, nothing else."""
    defaults: dict[str, Any] = {
        "provider": provider,
        "external_id": f"{provider}-sparse",
        "first_name": "Grace",
        "last_name": "Hopper",
        "company_name": "Navy Systems",
        "company_domain": "navy.example.com",
    }
    defaults.update(overrides)
    return RawLead(**defaults)


def unrunnable_raw(provider: str = "a") -> RawLead:
    """No person identity and no company signal: validation must reject it."""
    return make_raw_lead(
        provider=provider,
        first_name=None,
        last_name=None,
        full_name=None,
        email=None,
        linkedin_url=None,
        company_name=None,
        company_domain=None,
        company_website=None,
    )


async def run_pipeline(
    settings: Settings,
    crawlers: Sequence[BaseCrawler],
    *,
    limit: int = 100,
    max_leads: int | None = None,
) -> CrawlResult:
    return await Pipeline(settings).run(crawlers, limit=limit, max_leads=max_leads)


class TestHappyPath:
    async def test_end_to_end_from_a_single_source(self, settings: Settings) -> None:
        result = await run_pipeline(settings, [FakeCrawler(settings, [rich_raw()])])

        assert len(result.leads) == 1
        lead = result.leads[0]
        assert lead.person.full_name == "Ada Lovelace"
        assert lead.person.seniority is SeniorityLevel.VP
        assert lead.company.country == "US"  # normalized from "United States"
        assert result.stats.raw_collected == 1
        assert result.stats.normalized == 1
        assert result.stats.exported == 1
        assert result.is_empty is False

    async def test_stats_are_finalized(self, settings: Settings) -> None:
        result = await run_pipeline(settings, [FakeCrawler(settings, [rich_raw()])])
        assert result.stats.finished_at is not None
        assert result.stats.duration_seconds is not None
        assert result.stats.duration_seconds >= 0

    async def test_limit_is_forwarded_to_every_source(self, settings: Settings) -> None:
        crawler = FakeCrawler(settings, [rich_raw()])
        await run_pipeline(settings, [crawler], limit=42)
        assert crawler.requested_limit == 42

    async def test_no_sources_produces_an_empty_result(self, settings: Settings) -> None:
        result = await run_pipeline(settings, [])
        assert result.is_empty
        assert result.stats.source_errors == {}

    async def test_per_provider_counts_are_recorded(self, settings: Settings) -> None:
        result = await run_pipeline(
            settings,
            [
                FakeCrawler(settings, [rich_raw("a")], provider="a"),
                FakeCrawler(settings, [sparse_raw("b")], provider="b"),
            ],
        )
        assert result.stats.per_provider == {"a": 1, "b": 1}

    async def test_summary_line_is_populated(self, settings: Settings) -> None:
        result = await run_pipeline(settings, [FakeCrawler(settings, [rich_raw()])])
        assert "1 leads" in result.summary_line()


class TestSourceIsolation:
    @pytest.mark.parametrize(
        "error",
        [
            CrawlerError("flaky", "exploded"),
            SourceAuthError("flaky", "bad key"),
            SourceUnavailableError("flaky", "503"),
            RuntimeError("a bug in the adapter"),
        ],
    )
    async def test_one_failing_source_does_not_stop_the_run(
        self, settings: Settings, error: BaseException
    ) -> None:
        # Even an unexpected exception (not just a typed CrawlerError) is
        # contained: one bad adapter must not cost the whole run.
        result = await run_pipeline(
            settings,
            [
                FakeCrawler(settings, error=error, provider="flaky"),
                FakeCrawler(settings, [rich_raw("ok")], provider="ok"),
            ],
        )

        assert len(result.leads) == 1
        assert result.stats.source_errors == {"flaky": str(error)}
        assert result.stats.failed_providers == ["flaky"]
        # The healthy source is still counted.
        assert result.stats.per_provider == {"ok": 1}

    async def test_every_source_failing_leaves_nothing_to_export(self, settings: Settings) -> None:
        result = await run_pipeline(
            settings,
            [
                FakeCrawler(settings, error=CrawlerError("x", "boom"), provider="x"),
                FakeCrawler(settings, error=CrawlerError("y", "boom"), provider="y"),
            ],
        )
        assert result.is_empty
        # Enough for the CLI to distinguish this from an empty-but-healthy run.
        assert set(result.stats.source_errors) == {"x", "y"}

    async def test_a_failing_source_does_not_skew_the_counters(self, settings: Settings) -> None:
        result = await run_pipeline(
            settings,
            [
                FakeCrawler(settings, error=CrawlerError("flaky", "boom"), provider="flaky"),
                FakeCrawler(settings, [rich_raw("ok"), sparse_raw("ok")], provider="ok"),
            ],
        )
        assert result.stats.raw_collected == 2
        assert result.stats.normalized == 2


class TestStageAccounting:
    async def test_invalid_records_are_counted_and_recorded(self, settings: Settings) -> None:
        result = await run_pipeline(settings, [FakeCrawler(settings, [unrunnable_raw()])])

        assert result.is_empty
        assert result.stats.validation_failed == 1
        assert result.stats.normalized == 1
        reasons = {item.reason for item in result.rejections}
        assert RejectionReason.VALIDATION_FAILED in reasons
        # The rejection carries enough to identify the record later.
        assert result.rejections[0].provider == "a"
        assert result.rejections[0].detail

    async def test_filtered_records_are_attributed_to_a_rule(self) -> None:
        configured = load_settings(filters={"require_email": True})
        result = await run_pipeline(
            configured, [FakeCrawler(configured, [rich_raw(email=None), rich_raw()])]
        )

        assert len(result.leads) == 1  # the one with an email survived
        assert result.stats.filtered_out == 1
        assert result.stats.per_filter_reason == {"require_email": 1}
        assert result.rejections[0].reason is RejectionReason.FILTERED_OUT
        assert result.rejections[0].detail == "no email address"

    async def test_duplicates_are_counted_and_labelled(self, settings: Settings) -> None:
        leads = [rich_raw("a"), rich_raw("a", external_id="a-dupe", phone=None)]
        result = await run_pipeline(settings, [FakeCrawler(settings, leads)])

        assert len(result.leads) == 1
        assert result.stats.duplicates_removed == 1
        duplicate = next(r for r in result.rejections if r.reason is RejectionReason.DUPLICATE)
        assert "merged into" in (duplicate.detail or "")

    async def test_total_rejected_sums_every_stage(self) -> None:
        configured = load_settings(filters={"require_email": True})
        result = await run_pipeline(
            configured,
            [
                FakeCrawler(
                    configured,
                    [
                        unrunnable_raw(),  # validation
                        rich_raw(email=None),  # filter
                        rich_raw(),  # kept
                        rich_raw(),  # duplicate of the one above
                    ],
                )
            ],
        )
        stats = result.stats
        assert (stats.validation_failed, stats.filtered_out, stats.duplicates_removed) == (1, 1, 1)
        assert stats.total_rejected == 3
        assert len(result.leads) == 1

    async def test_rejection_sample_is_capped_but_counters_stay_exact(self) -> None:
        configured = load_settings(max_recorded_rejections=0)
        result = await run_pipeline(
            configured, [FakeCrawler(configured, [unrunnable_raw(), unrunnable_raw()])]
        )

        assert result.rejections == []
        assert result.stats.validation_failed == 2
        assert result.rejections_truncated is True

    async def test_rejections_are_not_flagged_as_truncated_below_the_cap(
        self, settings: Settings
    ) -> None:
        result = await run_pipeline(settings, [FakeCrawler(settings, [unrunnable_raw()])])
        assert result.rejections_truncated is False
        assert result.stats.total_rejected <= MAX_RECORDED_REJECTIONS


class TestMultiSourceEnrichment:
    async def test_a_second_source_completes_the_first(self, settings: Settings) -> None:
        # This is the whole reason to crawl more than one source.
        result = await run_pipeline(
            settings,
            [
                FakeCrawler(settings, [rich_raw("a", phone=None, linkedin_url=None)], provider="a"),
                FakeCrawler(
                    settings,
                    [
                        rich_raw(
                            "b", phone="+14155550142", linkedin_url="https://linkedin.com/in/ada"
                        )
                    ],
                    provider="b",
                ),
            ],
        )

        assert len(result.leads) == 1
        assert result.stats.duplicates_removed == 1
        assert result.leads[0].person.phone == "+14155550142"
        # The second source's URL is canonicalized on the way in.
        assert result.leads[0].person.linkedin_url == "https://www.linkedin.com/in/ada"

    async def test_distinct_people_are_both_kept(self, settings: Settings) -> None:
        result = await run_pipeline(
            settings,
            [FakeCrawler(settings, [rich_raw("a"), sparse_raw("a")])],
        )
        assert len(result.leads) == 2

    async def test_dedup_can_be_disabled(self) -> None:
        configured = load_settings(dedup_strategy=DedupStrategy.NONE)
        result = await run_pipeline(configured, [FakeCrawler(configured, [rich_raw(), rich_raw()])])
        assert len(result.leads) == 2
        assert result.stats.duplicates_removed == 0


class TestOrderingAndCapping:
    async def test_leads_are_ordered_most_complete_first(self, settings: Settings) -> None:
        result = await run_pipeline(settings, [FakeCrawler(settings, [sparse_raw(), rich_raw()])])
        scores = [lead.completeness for lead in result.leads]
        assert scores == sorted(scores, reverse=True)
        assert result.leads[0].person.full_name == "Ada Lovelace"

    async def test_sorting_can_be_turned_off(self) -> None:
        configured = load_settings(sort_by_completeness=False)
        result = await run_pipeline(
            configured, [FakeCrawler(configured, [sparse_raw(), rich_raw()])]
        )
        # Collection order is preserved, so the thin lead stays first.
        assert result.leads[0].person.full_name == "Grace Hopper"

    async def test_max_leads_keeps_the_best_records(self, settings: Settings) -> None:
        result = await run_pipeline(
            settings,
            [
                FakeCrawler(
                    settings,
                    [
                        sparse_raw(),
                        rich_raw(),
                        rich_raw(
                            external_id="third",
                            email="alan@bletchley.example.com",
                            first_name="Alan",
                            last_name="Turing",
                        ),
                    ],
                )
            ],
            max_leads=1,
        )
        assert len(result.leads) == 1
        assert result.leads[0].person.full_name == "Ada Lovelace"
        assert result.stats.exported == 1

    async def test_max_leads_above_the_result_size_is_a_noop(self, settings: Settings) -> None:
        result = await run_pipeline(settings, [FakeCrawler(settings, [rich_raw()])], max_leads=99)
        assert len(result.leads) == 1


class TestInjectedStages:
    async def test_stages_can_be_replaced_for_testing(self, settings: Settings) -> None:
        # The pipeline takes its stages by injection, so a caller can substitute
        # a stricter filter without touching the pipeline.
        strict = LeadFilter(FilterSettings(require_linkedin=True))
        pipeline = Pipeline(settings, lead_filter=strict)
        result = await pipeline.run(
            [FakeCrawler(settings, [rich_raw(), rich_raw(external_id="no-li", linkedin_url=None)])],
            limit=10,
        )
        assert len(result.leads) == 1
        assert result.stats.per_filter_reason == {"require_linkedin": 1}


class ExplodingValidator(LeadValidator):
    """A stage that fails on one specific record, as a real bug would."""

    def __init__(self, bad_full_name: str) -> None:
        self.bad_full_name = bad_full_name

    def validate(self, lead: StandardizedLead) -> Any:
        if lead.person.full_name == self.bad_full_name:
            raise RuntimeError("validator exploded")
        return super().validate(lead)


class ExplodingFilter(LeadFilter):
    """A filter whose rule raises for one specific record."""

    def __init__(
        self, criteria: FilterSettings, bad_full_name: str, *, min_completeness: float = 0.0
    ) -> None:
        super().__init__(criteria, min_completeness=min_completeness)
        self.bad_full_name = bad_full_name

    def evaluate(self, lead: StandardizedLead) -> Any:
        if lead.person.full_name == self.bad_full_name:
            raise RuntimeError("filter exploded")
        return super().evaluate(lead)


class TestPerRecordIsolation:
    """One bad record must cost one lead, never the run."""

    async def test_a_raising_validator_is_contained(self, settings: Settings) -> None:
        pipeline = Pipeline(settings, validator=ExplodingValidator("Ada Lovelace"))
        result = await pipeline.run(
            [
                FakeCrawler(
                    settings,
                    [
                        rich_raw(),
                        sparse_raw(),
                        rich_raw(
                            external_id="other",
                            email="alan@bletchley.example.com",
                            first_name="Alan",
                            last_name="Turing",
                            company_name="Bletchley Park",
                            company_domain="bletchley.example.com",
                        ),
                    ],
                )
            ],
            limit=10,
        )

        # The two healthy records survived; only the poisoned one was lost.
        assert len(result.leads) == 2
        assert result.stats.processing_failed == 1
        rejection = next(
            item for item in result.rejections if item.reason is RejectionReason.PROCESSING_FAILED
        )
        assert rejection.detail == "validator exploded"

    async def test_a_raising_filter_is_contained(self, settings: Settings) -> None:
        pipeline = Pipeline(
            settings,
            lead_filter=ExplodingFilter(
                settings.filters,
                "Ada Lovelace",
                min_completeness=settings.min_completeness,
            ),
        )
        result = await pipeline.run([FakeCrawler(settings, [rich_raw(), sparse_raw()])], limit=10)

        assert len(result.leads) == 1
        assert result.stats.processing_failed == 1
        # A processor bug is not miscounted as bad input.
        assert result.stats.validation_failed == 0
        assert result.stats.normalization_failed == 0

    async def test_a_processing_failure_counts_toward_the_totals(self, settings: Settings) -> None:
        pipeline = Pipeline(settings, validator=ExplodingValidator("Ada Lovelace"))
        result = await pipeline.run([FakeCrawler(settings, [rich_raw()])], limit=10)

        assert result.is_empty
        assert result.stats.total_rejected == 1
        assert result.stats.records_invalid == 1
        assert result.stats.normalized == 1  # it parsed fine; a later stage broke


class TestRunSummaryLog:
    """The nine fields an operator needs from a single record."""

    REQUIRED = (
        "sources",
        "started_at",
        "finished_at",
        "records_discovered",
        "records_parsed",
        "records_invalid",
        "duplicates_removed",
        "records_exported",
        "errors",
    )

    async def test_the_record_carries_every_required_field(
        self, settings: Settings, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.INFO, logger="src.processors.pipeline"):
            result = await run_pipeline(settings, [FakeCrawler(settings, [rich_raw()])])

        summaries = [r for r in caplog.records if r.getMessage() == "run summary"]
        assert len(summaries) == 1, "the run summary must be emitted exactly once"

        fields = summaries[0].__dict__
        missing = [name for name in self.REQUIRED if name not in fields]
        assert missing == [], f"run summary is missing {missing}"

        assert fields["sources"] == ["fake"]
        assert fields["records_discovered"] == 1
        assert fields["records_parsed"] == 1
        assert fields["records_exported"] == result.stats.exported
        assert fields["errors"] == {}

    async def test_errors_are_reported_per_source(
        self, settings: Settings, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.INFO, logger="src.processors.pipeline"):
            await run_pipeline(
                settings,
                [
                    FakeCrawler(
                        settings, error=CrawlerError("dead", "connection refused"), provider="dead"
                    ),
                    FakeCrawler(settings, [rich_raw("ok")], provider="ok"),
                ],
            )

        summary = next(r for r in caplog.records if r.getMessage() == "run summary")
        assert "connection refused" in summary.__dict__["errors"]["dead"]
        # The healthy source is still named, so the record says what did run.
        assert summary.__dict__["sources"] == ["dead", "ok"]

    async def test_the_record_is_json_serializable(
        self, settings: Settings, caplog: pytest.LogCaptureFixture
    ) -> None:
        # The fields must survive --log-format json, which json.dumps the extras.
        with caplog.at_level(logging.INFO, logger="src.processors.pipeline"):
            await run_pipeline(settings, [FakeCrawler(settings, [rich_raw()])])

        summary = next(r for r in caplog.records if r.getMessage() == "run summary")
        extras = {k: v for k, v in summary.__dict__.items() if k in self.REQUIRED}
        assert json.loads(json.dumps(extras, default=str))["records_discovered"] == 1


class TestValidationWarnings:
    """A record can be kept *and* flagged.

    A lead whose company domain was garbled upstream is still addressable: it has
    a name, an employer and a mailbox. Dropping it would throw away good data to
    punish one bad field — so it ships, and the run report says so.
    """

    @staticmethod
    def warned(**overrides: Any) -> RawLead:
        """A lead that survives validation but lost a value on the way in."""
        defaults: dict[str, Any] = {
            "external_id": "warned-1",
            # No fallback exists: a consumer mailbox yields no company domain and
            # there is no website to derive one from, so the garbled domain is
            # genuinely lost.
            "email": "ada@gmail.com",
            "company_domain": "not a domain",
        }
        defaults.update(overrides)
        return make_raw_lead(**defaults)

    @staticmethod
    def fatal(**overrides: Any) -> RawLead:
        """A lead with nothing to identify and no company signal."""
        defaults: dict[str, Any] = {
            "external_id": "fatal-1",
            "first_name": None,
            "last_name": None,
            "full_name": "Info",
            "email": None,
            "company_name": None,
            "company_domain": None,
        }
        defaults.update(overrides)
        return make_raw_lead(**defaults)

    async def test_a_warned_record_is_still_exported(self, settings: Settings) -> None:
        result = await run_pipeline(settings, [FakeCrawler(settings, [self.warned()])])

        assert len(result.leads) == 1
        assert result.stats.validation_warned == 1
        assert result.stats.exported == 1

    async def test_a_warning_is_attributed_to_its_rule(self, settings: Settings) -> None:
        result = await run_pipeline(settings, [FakeCrawler(settings, [self.warned()])])

        assert result.stats.per_validation_reason == {"invalid_domain": 1}

    async def test_a_warning_is_not_a_rejection(self, settings: Settings) -> None:
        # The counters a run report leads with must not move: nothing was dropped.
        result = await run_pipeline(settings, [FakeCrawler(settings, [self.warned()])])

        assert result.stats.total_rejected == 0
        assert result.stats.records_invalid == 0
        assert result.stats.validation_failed == 0
        assert result.rejections == []

    async def test_a_clean_record_is_neither_warned_nor_failed(self, settings: Settings) -> None:
        # The guard against over-reporting: an ordinary lead raises nothing.
        result = await run_pipeline(settings, [FakeCrawler(settings, [rich_raw()])])

        assert result.stats.validation_warned == 0
        assert result.stats.per_validation_reason == {}

    async def test_warnings_and_errors_are_counted_separately(self, settings: Settings) -> None:
        result = await run_pipeline(
            settings, [FakeCrawler(settings, [self.warned(), self.fatal()])]
        )

        assert result.stats.validation_warned == 1
        assert result.stats.validation_failed == 1
        # Only the fatal record is unusable, so only it is subtracted from the run.
        assert result.stats.records_invalid == 1
        assert result.stats.total_rejected == 1
        assert len(result.leads) == 1

    async def test_a_fatal_rule_is_attributed_too(self, settings: Settings) -> None:
        # Attribution is about which checks are noisy, not about outcomes, so a
        # record that dies still names the rule that killed it.
        result = await run_pipeline(settings, [FakeCrawler(settings, [self.fatal()])])

        assert result.stats.per_validation_reason
        assert result.stats.validation_warned == 0
        assert "non_person_name" in result.stats.per_validation_reason

    async def test_the_flag_is_logged_per_record(
        self, settings: Settings, caplog: pytest.LogCaptureFixture
    ) -> None:
        # The counters give the total; this line is what says *which* lead.
        with caplog.at_level(logging.DEBUG, logger="src.processors.pipeline"):
            await run_pipeline(settings, [FakeCrawler(settings, [self.warned()])])

        flagged = [r for r in caplog.records if r.getMessage() == "lead flagged"]
        assert len(flagged) == 1
        detail = flagged[0].__dict__["detail"]
        assert "company_domain" in detail
        assert "not a domain" in detail

    async def test_the_run_summary_reports_warnings_and_rules(
        self, settings: Settings, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.INFO, logger="src.processors.pipeline"):
            await run_pipeline(settings, [FakeCrawler(settings, [self.warned(), self.fatal()])])

        summary = next(r for r in caplog.records if r.getMessage() == "run summary")
        fields = summary.__dict__
        assert fields["records_warned"] == 1
        assert fields["validation_rules"]["invalid_domain"] == 1
        assert fields["validation_rules"]["non_person_name"] == 1
        # The summary is emitted as JSON under --log-format json, so the new
        # mapping must serialize rather than merely exist.
        assert json.loads(json.dumps(fields["validation_rules"]))
