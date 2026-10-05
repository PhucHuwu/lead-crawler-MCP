"""Tests for the domain model in :mod:`src.models`."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from src.exporters.csv_exporter import LEAD_COLUMNS
from src.models.enums import DedupStrategy, SeniorityLevel
from src.models.lead import (
    LeadSource,
    RawLead,
    StandardizedLead,
    format_social_links,
    slugify_identity,
)
from src.models.results import CrawlStats
from tests.conftest import make_lead, make_raw_lead


class TestLeadId:
    def test_is_stable_across_instances(self) -> None:
        assert make_lead().lead_id == make_lead().lead_id

    def test_derived_from_email_when_present(self) -> None:
        first = make_lead(email="ada@acme.com")
        second = make_lead(email="ada@acme.com", full_name="Ada B Lovelace")
        # Same email means same identity, even if other fields differ.
        assert first.lead_id == second.lead_id

    def test_differs_for_different_people(self) -> None:
        assert make_lead(email="ada@acme.com").lead_id != make_lead(email="alan@acme.com").lead_id

    def test_scoped_by_provider(self) -> None:
        # Two providers reporting the same person stay distinct until dedup runs.
        assert make_lead(provider="a").lead_id != make_lead(provider="b").lead_id

    def test_explicit_id_is_preserved(self) -> None:
        lead = StandardizedLead(
            lead_id="custom-id",
            person=make_lead().person,
            company=make_lead().company,
            source=LeadSource(provider="test"),
        )
        assert lead.lead_id == "custom-id"

    def test_falls_back_to_name_and_company(self) -> None:
        lead = make_lead(email=None, full_name="Ada Lovelace", company_name="Acme Corp")
        assert lead.lead_id


class TestCompleteness:
    def test_full_lead_scores_one(self) -> None:
        lead = make_lead(linkedin_url="https://www.linkedin.com/in/ada", phone="+14155550142")
        assert lead.completeness == 1.0

    def test_sparse_lead_scores_low(self) -> None:
        lead = make_lead(email=None, job_title=None, company_domain=None, country=None)
        assert lead.completeness < 0.5

    def test_empty_lead_scores_zero(self) -> None:
        lead = make_lead(
            first_name=None,
            last_name=None,
            full_name=None,
            email=None,
            job_title=None,
            company_name=None,
            company_domain=None,
            country=None,
        )
        assert lead.completeness == 0.0

    def test_never_exceeds_one(self) -> None:
        lead = make_lead(linkedin_url="x", phone="+1", country="US")
        assert 0.0 <= lead.completeness <= 1.0


class TestIdentityKeys:
    def test_ladder_order_is_strongest_first(self) -> None:
        lead = make_lead(linkedin_url="https://www.linkedin.com/in/ada", phone="+14155550142")
        assert list(lead.identity_keys()) == [
            "email",
            "linkedin",
            "phone_name",
            "name_domain",
            "name_company",
        ]

    def test_the_source_id_heads_the_ladder(self) -> None:
        # A provider saying "these two records are one record" is the strongest
        # signal there is, so it has to outrank every inferred key.
        lead = make_lead(external_id="p-1", linkedin_url="https://www.linkedin.com/in/ada")
        keys = lead.identity_keys()
        assert next(iter(keys)) == "source_id"
        assert keys["source_id"] == "test:p-1"

    def test_the_source_id_is_scoped_by_provider(self) -> None:
        # Two sources routinely use overlapping external ids; only the pair
        # (provider, id) identifies a record.
        assert make_lead(external_id="1", provider="a").identity_keys()["source_id"] == "a:1"
        assert make_lead(external_id="1", provider="b").identity_keys()["source_id"] == "b:1"

    def test_omits_absent_signals(self) -> None:
        lead = make_lead(email=None, linkedin_url=None, company_domain=None, company_name=None)
        assert lead.identity_keys() == {}

    def test_a_surname_at_a_company_is_not_an_identity(self) -> None:
        # Two different people called Smith at one employer share a surname and a
        # domain. Treating that as identity would merge them, so the ladder must
        # never offer a last-name-anchored key.
        keys = make_lead(full_name="John Smith", company_domain="acme.com").identity_keys()
        assert "lastname_domain" not in keys

    def test_name_slugging_ignores_punctuation_and_case(self) -> None:
        assert slugify_identity("Ada O'Neill") == "adaoneill"
        assert slugify_identity(None) == ""

    def test_name_keys_are_normalized(self) -> None:
        keys = make_lead(full_name="ADA  O'NEILL").identity_keys()
        assert keys["name_domain"] == "adaoneill@acme.com"


class TestFlatten:
    def test_columns_match_csv_exporter(self) -> None:
        # The CSV header is declared separately so empty results still get one;
        # this guards the two from drifting apart.
        assert tuple(make_lead().flatten()) == LEAD_COLUMNS

    def test_timestamps_use_z_suffix(self) -> None:
        lead = make_lead()
        assert str(lead.flatten()["collected_at"]).endswith("Z")

    def test_enum_rendered_as_value(self) -> None:
        assert make_lead().flatten()["person_seniority"] == SeniorityLevel.UNKNOWN.value


class TestEnrichmentColumns:
    """The three columns the website source populates.

    A spreadsheet column cannot hold a mapping, so social links are rendered as
    one string — and that rendering has to be deterministic, or two exports of
    the same lead would differ in a diff.
    """

    def test_social_links_render_as_sorted_pairs(self) -> None:
        rendered = format_social_links(
            {"linkedin": "https://www.linkedin.com/company/acme", "github": "https://github.com/a"}
        )
        assert (
            rendered
            == "github=https://github.com/a; linkedin=https://www.linkedin.com/company/acme"
        )

    def test_no_links_render_as_none(self) -> None:
        # Empty dict and missing map must be indistinguishable in the export.
        assert format_social_links({}) is None

    def test_the_enrichment_fields_reach_the_row(self) -> None:
        lead = make_lead()
        lead.company.description = "Acme builds widgets."
        lead.company.contact_url = "https://acme.com/contact"
        lead.company.social_links = {"github": "https://github.com/acme"}
        row = lead.flatten()
        assert row["company_description"] == "Acme builds widgets."
        assert row["company_contact_url"] == "https://acme.com/contact"
        assert row["company_social_links"] == "github=https://github.com/acme"

    def test_enrichment_fields_default_to_blank(self) -> None:
        row = make_lead().flatten()
        assert row["company_description"] is None
        assert row["company_contact_url"] is None
        assert row["company_social_links"] is None


class TestRawLead:
    def test_rejects_unknown_fields(self) -> None:
        # A typo in an adapter should fail loudly rather than silently drop data.
        with pytest.raises(Exception, match="extra_forbidden"):
            RawLead(provider="test", emial="typo@acme.com")  # type: ignore[call-arg]

    def test_accepts_string_or_int_employee_count(self) -> None:
        assert make_raw_lead(company_employee_count="201-500").company_employee_count == "201-500"
        assert make_raw_lead(company_employee_count=250).company_employee_count == 250

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ({"email": "a@b.com"}, "a@b.com"),
            ({"email": None, "full_name": "Ada"}, "Ada"),
            ({"email": None, "full_name": None, "external_id": "x-1"}, "x-1"),
            ({"email": None, "full_name": None, "external_id": None}, "<unidentified>"),
        ],
    )
    def test_label_prefers_the_most_specific_identifier(
        self, raw: dict[str, object], expected: str
    ) -> None:
        lead = make_raw_lead(**raw)
        assert lead.label() == expected


class TestCrawlStats:
    def test_total_rejected_sums_every_drop_reason(self) -> None:
        stats = CrawlStats()
        stats.validation_failed = 2
        stats.filtered_out = 3
        stats.exact_duplicates = 1
        assert stats.total_rejected == 6

    def test_duplicates_removed_is_the_sum_of_its_parts(self) -> None:
        # Derived, so a run report can never quote a total that disagrees with
        # the exact/probable split printed beside it.
        stats = CrawlStats()
        stats.exact_duplicates = 3
        stats.probable_duplicates = 2
        assert stats.duplicates_removed == 5

    def test_dedup_before_and_after_bracket_the_stage(self) -> None:
        stats = CrawlStats()
        stats.records_before_deduplication = 10
        stats.exact_duplicates = 3
        stats.probable_duplicates = 1
        assert stats.records_after_deduplication == 6

    def test_duplicates_count_toward_the_rejected_total(self) -> None:
        stats = CrawlStats()
        stats.exact_duplicates = 1
        stats.probable_duplicates = 2
        assert stats.total_rejected == 3

    def test_total_rejected_counts_processor_failures(self) -> None:
        # A stage that raised on a record dropped it just as surely as a rule did.
        stats = CrawlStats()
        stats.normalization_failed = 1
        stats.processing_failed = 2
        assert stats.total_rejected == 3

    def test_records_invalid_sums_the_three_ways_a_record_is_unusable(self) -> None:
        stats = CrawlStats()
        stats.normalization_failed = 1
        stats.processing_failed = 2
        stats.validation_failed = 3
        stats.filtered_out = 99  # filtered, not invalid: the record itself was fine
        assert stats.records_invalid == 6

    def test_record_filter_tracks_per_rule(self) -> None:
        stats = CrawlStats()
        stats.record_filter("min_employees")
        stats.record_filter("min_employees")
        stats.record_filter("require_email")
        assert stats.filtered_out == 3
        assert stats.per_filter_reason == {"min_employees": 2, "require_email": 1}

    def test_finalize_is_idempotent(self) -> None:
        stats = CrawlStats()
        stats.finalize()
        first = stats.finished_at
        stats.finalize()
        assert stats.finished_at == first
        assert stats.duration_seconds is not None

    def test_failed_providers_is_sorted(self) -> None:
        stats = CrawlStats()
        stats.record_source_error("zeta", "boom")
        stats.record_source_error("alpha", "boom")
        assert stats.failed_providers == ["alpha", "zeta"]

    def test_record_validation_tracks_per_rule(self) -> None:
        stats = CrawlStats()
        stats.record_validation("invalid_domain")
        stats.record_validation("invalid_domain")
        stats.record_validation("no_identity")
        assert stats.per_validation_reason == {"invalid_domain": 2, "no_identity": 1}

    def test_record_validation_does_not_touch_the_drop_counters(self) -> None:
        # Rule attribution is separate from the verdict: a warning is recorded
        # here and the record still ships.
        stats = CrawlStats()
        stats.record_validation("invalid_domain")
        assert stats.validation_failed == 0
        assert stats.validation_warned == 0

    def test_warned_records_are_not_counted_as_rejected(self) -> None:
        stats = CrawlStats()
        stats.normalized = 10
        stats.validation_warned = 3
        assert stats.total_rejected == 0
        assert stats.records_invalid == 0


class TestLeadSource:
    def test_collected_at_defaults_to_utc_now(self) -> None:
        source = LeadSource(provider="test")
        assert source.collected_at.tzinfo is not None

    def test_naive_datetimes_are_assumed_utc_by_normalizer(self) -> None:
        from src.processors.normalizer import Normalizer

        raw = make_raw_lead()
        raw.collected_at = datetime(2026, 1, 1, 12, 0)
        assert Normalizer().normalize(raw).source.collected_at == datetime(
            2026, 1, 1, 12, 0, tzinfo=UTC
        )


class TestEnums:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("c_suite", SeniorityLevel.C_SUITE),
            ("C_SUITE", SeniorityLevel.C_SUITE),
            ("Vice President", SeniorityLevel.VP),
            ("CTO", SeniorityLevel.C_SUITE),
            ("owner", SeniorityLevel.FOUNDER),
            (SeniorityLevel.DIRECTOR, SeniorityLevel.DIRECTOR),
            ("wizard", SeniorityLevel.UNKNOWN),
            (None, SeniorityLevel.UNKNOWN),
        ],
    )
    def test_seniority_coercion(self, value: object, expected: SeniorityLevel) -> None:
        assert SeniorityLevel.coerce(value) == expected

    def test_dedup_strategy_values_are_stable(self) -> None:
        # These strings appear in .env files and CI flags.
        assert [s.value for s in DedupStrategy] == ["none", "email", "identity", "aggressive"]
