"""Tests for the crawler registry and the built-in sources.

The registry tests are as much about the *extension contract* as the built-ins:
a new source must be one module plus one registration, with nothing else in the
codebase aware of it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.config import Settings, load_settings
from src.crawlers import registry as registry_module
from src.crawlers.base import BaseCrawler
from src.crawlers.csv_source import CsvCrawler
from src.crawlers.mock import MockCrawler
from src.crawlers.registry import (
    available_providers,
    build_crawler,
    get_crawler_class,
    register_crawler,
)
from src.models.lead import RawLead
from src.processors.normalizer import Normalizer
from src.utils.errors import ConfigError, CrawlerError, SourceNotFoundError
from tests.conftest import log_record
from tests.fixtures.records import VIETNAMESE_CSV

BUILTIN_PROVIDERS = {"apollo", "csv", "mock"}


class TestRegistry:
    def test_builtin_sources_are_registered(self) -> None:
        assert set(available_providers()) >= BUILTIN_PROVIDERS

    def test_provider_list_is_sorted_for_stable_cli_output(self) -> None:
        providers = available_providers()
        assert providers == sorted(providers)

    def test_lookup_is_case_and_whitespace_insensitive(self) -> None:
        assert get_crawler_class("CSV") is CsvCrawler
        assert get_crawler_class("  mock  ") is MockCrawler

    def test_unknown_source_names_the_alternatives(self) -> None:
        with pytest.raises(SourceNotFoundError) as excinfo:
            get_crawler_class("salesforce")
        # The message is shown verbatim by the CLI, so it must be actionable.
        assert "salesforce" in str(excinfo.value)
        assert "mock" in str(excinfo.value)

    def test_duplicate_slug_is_rejected_at_import_time(self) -> None:
        # Failing here rather than at run time is the point: a shadowed source
        # would otherwise be silently unreachable.
        with pytest.raises(ConfigError, match="already registered"):

            @register_crawler
            class _ShadowMock(BaseCrawler):
                provider = "mock"

                async def crawl(self, limit: int) -> list[RawLead]:
                    return []

    def test_missing_slug_is_rejected(self) -> None:
        with pytest.raises(ConfigError, match="non-empty"):

            @register_crawler
            class _NoSlug(BaseCrawler):
                async def crawl(self, limit: int) -> list[RawLead]:
                    return []

    def test_a_new_source_needs_only_a_registration(
        self, monkeypatch: pytest.MonkeyPatch, settings: Settings
    ) -> None:
        """An isolated copy of the registry stands in for a fresh import."""
        monkeypatch.setattr(registry_module, "_REGISTRY", dict(registry_module._REGISTRY))

        @register_crawler
        class CustomCrawler(BaseCrawler):
            provider = "custom"
            display_name = "Custom source"
            description = "Registered by a test."

            async def crawl(self, limit: int) -> list[RawLead]:
                return []

        assert "custom" in available_providers()
        # Resolvable and constructible through the same path the CLI uses.
        assert isinstance(build_crawler("custom", settings), CustomCrawler)

    def test_build_unavailable_source_reports_the_missing_setting(self, settings: Settings) -> None:
        # No --csv-path configured, so the source cannot run.
        with pytest.raises(ConfigError, match="csv-path"):
            build_crawler("csv", settings)

    def test_build_unknown_source_raises(self, settings: Settings) -> None:
        with pytest.raises(SourceNotFoundError):
            build_crawler("nope", settings)

    def test_base_crawler_cannot_be_instantiated(self, settings: Settings) -> None:
        with pytest.raises(TypeError):
            BaseCrawler(settings)  # type: ignore[abstract]


class TestMockCrawler:
    async def test_respects_the_limit(self, settings: Settings) -> None:
        crawler = MockCrawler(load_settings(mock={"seed": 1}))
        assert len(await crawler.crawl(7)) == 7

    async def test_zero_limit_returns_nothing(self, settings: Settings) -> None:
        assert await MockCrawler(settings).crawl(0) == []

    async def test_generated_content_is_reproducible(self, settings: Settings) -> None:
        # Same seed, same records. The collection timestamp is naturally excluded:
        # it describes when the run happened, not what was generated.
        configured = load_settings(mock={"seed": 7, "messy_ratio": 0.0, "duplicate_ratio": 0.0})
        first = await MockCrawler(configured).crawl(5)
        second = await MockCrawler(configured).crawl(5)
        assert [lead.model_dump(exclude={"collected_at"}) for lead in first] == [
            lead.model_dump(exclude={"collected_at"}) for lead in second
        ]

    async def test_different_seeds_produce_different_records(self) -> None:
        one = await MockCrawler(load_settings(mock={"seed": 7})).crawl(5)
        two = await MockCrawler(load_settings(mock={"seed": 8})).crawl(5)
        assert [(lead.first_name, lead.email) for lead in one] != [
            (lead.first_name, lead.email) for lead in two
        ]

    async def test_messy_records_are_degraded_on_purpose(self) -> None:
        # A demo run has to exercise the normalizer, not just the happy path.
        crawler = MockCrawler(load_settings(mock={"seed": 3, "messy_ratio": 1.0}))
        leads = await crawler.crawl(3)
        for lead in leads:
            first_name = lead.first_name
            assert first_name is not None
            assert first_name == first_name.upper()
        email = leads[0].email
        assert email is not None and email.startswith(" ")  # padding, as a spreadsheet export has
        assert leads[0].company_employee_count == "N/A"  # index 0 -> placeholder

    async def test_duplicates_can_be_forced(self) -> None:
        # With every record recycled there is only one identity to copy. The
        # copies are re-degraded independently, so they match once normalized —
        # which is exactly the duplicate the pipeline has to collapse.
        crawler = MockCrawler(load_settings(mock={"seed": 5, "duplicate_ratio": 1.0}))
        leads = await crawler.crawl(5)
        normalizer = Normalizer()
        emails = {normalizer.normalize(lead).person.email for lead in leads}
        assert emails == {normalizer.normalize(leads[0]).person.email}
        assert len(emails) == 1
        # The raw strings genuinely differ, so this is not a trivially clean set.
        assert len({lead.email for lead in leads}) > 1

    async def test_every_lead_carries_provenance(self, settings: Settings) -> None:
        lead = (await MockCrawler(settings).crawl(1))[0]
        assert lead.provider == "mock"
        assert lead.external_id is not None and lead.external_id.startswith("mock-")
        assert lead.source_url is not None
        assert lead.raw  # the unmodified record is retained for debugging

    def test_class_metadata_is_complete(self) -> None:
        assert MockCrawler.display_name
        assert MockCrawler.description
        assert MockCrawler.requires_credentials is False

    def test_is_available_without_configuration(self, settings: Settings) -> None:
        assert MockCrawler(settings).is_available() == (True, "")


def csv_settings(path: Path, **csv_source: object) -> Settings:
    return load_settings(csv_source={"path": path, **csv_source})


class TestCsvCrawler:
    async def test_reads_rows_through_header_aliases(self, sample_csv: Path) -> None:
        leads = await CsvCrawler(csv_settings(sample_csv)).crawl(100)
        first = leads[0]
        # "First Name" -> first_name, "Position" -> job_title, "Website" -> website.
        assert first.first_name == "Ada"
        assert first.last_name == "Lovelace"
        assert first.job_title == "VP of Engineering"
        assert first.company_website == "acme.com"
        assert first.company_employee_count == "201-500"

    async def test_values_are_not_normalized_by_the_crawler(self, sample_csv: Path) -> None:
        # Cleaning is the pipeline's job; the adapter's only job is to map fields.
        second = (await CsvCrawler(csv_settings(sample_csv)).crawl(100))[1]
        assert second.first_name == "GRACE"  # whitespace trimmed, case untouched
        assert second.email == "GRACE@NAVY.EXAMPLE.COM"

    async def test_blank_cells_become_absent_fields(self, sample_csv: Path) -> None:
        third = (await CsvCrawler(csv_settings(sample_csv)).crawl(100))[2]
        assert third.job_title is None
        # The original row is preserved, minus the empties.
        assert "Position" not in third.raw

    async def test_row_without_a_contact_is_still_returned(self, sample_csv: Path) -> None:
        # A company-only row is real data; the validator decides it is unusable,
        # not the reader.
        leads = await CsvCrawler(csv_settings(sample_csv)).crawl(100)
        last = leads[-1]
        assert last.company_name == "Ghost Corp"
        assert last.first_name is None and last.email is None

    async def test_row_number_is_recorded_for_troubleshooting(self, sample_csv: Path) -> None:
        leads = await CsvCrawler(csv_settings(sample_csv)).crawl(100)
        assert [lead.raw["_row_number"] for lead in leads] == [2, 3, 4, 5]

    async def test_respects_the_limit(self, sample_csv: Path) -> None:
        assert len(await CsvCrawler(csv_settings(sample_csv)).crawl(2)) == 2

    async def test_unrecognized_columns_are_preserved(self, tmp_path: Path) -> None:
        path = tmp_path / "extra.csv"
        path.write_text("Email,Favourite Colour\nada@acme.com,green\n", encoding="utf-8")
        lead = (await CsvCrawler(csv_settings(path)).crawl(1))[0]
        assert lead.raw["Favourite Colour"] == "green"

    async def test_column_map_overrides_the_builtin_aliases(self, tmp_path: Path) -> None:
        path = tmp_path / "custom.csv"
        path.write_text("Email,Job\nada@acme.com,CTO\n", encoding="utf-8")
        crawler = CsvCrawler(csv_settings(path, column_map=["job_title=Job"]))
        lead = (await crawler.crawl(1))[0]
        assert lead.job_title == "CTO"

    async def test_malformed_column_map_entry_is_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "x.csv"
        path.write_text("Email\nada@acme.com\n", encoding="utf-8")
        crawler = CsvCrawler(csv_settings(path, column_map=["job_title"]))
        with pytest.raises(SourceNotFoundError, match="expected 'target_field="):
            await crawler.crawl(1)

    async def test_unknown_column_map_target_is_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "x.csv"
        path.write_text("Email\nada@acme.com\n", encoding="utf-8")
        crawler = CsvCrawler(csv_settings(path, column_map=["nickname=Job"]))
        with pytest.raises(SourceNotFoundError, match="not a known field"):
            await crawler.crawl(1)

    async def test_delimiter_can_be_sniffed(self, tmp_path: Path) -> None:
        path = tmp_path / "semi.csv"
        path.write_text("Email;Company\nada@acme.com;Acme Corp\n", encoding="utf-8")
        crawler = CsvCrawler(csv_settings(path, delimiter="auto"))
        lead = (await crawler.crawl(1))[0]
        assert lead.company_name == "Acme Corp"

    async def test_configured_delimiter_is_honoured(self, tmp_path: Path) -> None:
        path = tmp_path / "tab.tsv"
        path.write_text("Email\tCompany\nada@acme.com\tAcme Corp\n", encoding="utf-8")
        crawler = CsvCrawler(csv_settings(path, delimiter="\t"))
        assert (await crawler.crawl(1))[0].company_name == "Acme Corp"

    async def test_header_only_file_yields_nothing(self, tmp_path: Path) -> None:
        path = tmp_path / "empty.csv"
        path.write_text("Email,Company\n", encoding="utf-8")
        assert await CsvCrawler(csv_settings(path)).crawl(10) == []

    async def test_wrong_encoding_is_reported_with_a_remedy(self, tmp_path: Path) -> None:
        path = tmp_path / "latin.csv"
        path.write_bytes(b"Email,Name\nada@acme.com,Jos\xe9\n")
        crawler = CsvCrawler(csv_settings(path, encoding="utf-8"))
        with pytest.raises(SourceNotFoundError, match="ENCODING"):
            await crawler.crawl(1)

    async def test_crawl_without_a_path_is_an_error(self, settings: Settings) -> None:
        with pytest.raises(SourceNotFoundError, match="no CSV path"):
            await CsvCrawler(settings).crawl(1)

    def test_is_available_requires_a_path(self, settings: Settings) -> None:
        ok, reason = CsvCrawler(settings).is_available()
        assert ok is False
        assert "--csv-path" in reason

    def test_is_available_rejects_a_missing_file(self, tmp_path: Path) -> None:
        ok, reason = CsvCrawler(csv_settings(tmp_path / "nope.csv")).is_available()
        assert ok is False
        assert "file not found" in reason

    def test_is_available_rejects_a_directory(self, tmp_path: Path) -> None:
        ok, reason = CsvCrawler(csv_settings(tmp_path)).is_available()
        assert ok is False
        assert "not a file" in reason

    def test_a_configured_path_makes_the_crawler_available(self, tmp_path: Path) -> None:
        # This is how the CLI's --csv-path flag reaches the instance: it is
        # resolved into settings before construction, not applied afterwards.
        path = tmp_path / "later.csv"
        path.write_text("Email\nada@acme.com\n", encoding="utf-8")
        assert CsvCrawler(csv_settings(path)).is_available() == (True, "")

    def test_class_metadata_is_complete(self) -> None:
        assert CsvCrawler.display_name
        assert CsvCrawler.description
        assert CsvCrawler.requires_credentials is False


class TestCsvCrawlerResilience:
    """A bad file must degrade to a partial read, not to a failed source."""

    async def test_blank_rows_do_not_consume_the_limit(self, tmp_path: Path) -> None:
        # `--limit` counts leads. A sheet padded with blank rows is common, and
        # letting them eat the budget would return fewer leads than were asked
        # for while more were sitting right there in the file.
        path = tmp_path / "gaps.csv"
        path.write_text(
            "Email,Company\nada@acme.com,Acme Corp\n,,\ngrace@navy.example.com,Navy Systems\n",
            encoding="utf-8",
        )
        leads = await CsvCrawler(csv_settings(path)).crawl(2)
        assert [lead.email for lead in leads] == ["ada@acme.com", "grace@navy.example.com"]

    async def test_a_malformed_row_keeps_every_row_read_before_it(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        # The csv module raises from the middle of the stream and cannot resume,
        # so the alternative to this behaviour is losing the whole file — a
        # 50 000-row export discarded over one pathological cell.
        path = tmp_path / "huge.csv"
        path.write_text(
            f"Email,Company\nada@acme.com,Acme Corp\ngrace@navy.example.com,{'x' * 200_000}\n",
            encoding="utf-8",
        )
        with caplog.at_level("WARNING", logger="crawlers.csv"):
            leads = await CsvCrawler(csv_settings(path)).crawl(100)

        assert [lead.email for lead in leads] == ["ada@acme.com"]
        stopped = log_record(caplog, "stopped reading csv at a malformed row")
        # The line number is what lets an operator open the file and look.
        assert stopped.row == 3
        assert "field larger than field limit" in stopped.error

    async def test_an_unreadable_file_names_the_path(self, tmp_path: Path) -> None:
        # A directory is the portable stand-in for "the open itself failed": no
        # permissions to arrange, and it raises the same OSError family.
        crawler = CsvCrawler(csv_settings(tmp_path))
        with pytest.raises(CrawlerError, match="cannot read") as excinfo:
            await crawler.crawl(1)
        assert str(tmp_path) in str(excinfo.value)

    async def test_a_row_that_cannot_be_mapped_does_not_kill_the_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        path = tmp_path / "leads.csv"
        path.write_text(
            "Email,Company\nada@acme.com,Acme Corp\ngrace@navy.example.com,Navy Systems\n",
            encoding="utf-8",
        )
        crawler = CsvCrawler(csv_settings(path))
        original = crawler._row_to_lead

        def flaky(row: object, targets: object, number: int) -> RawLead | None:
            if isinstance(row, dict) and row.get("Email") == "grace@navy.example.com":
                raise ValueError("unreadable cell")
            return original(row, targets, number)  # type: ignore[arg-type]

        monkeypatch.setattr(crawler, "_row_to_lead", flaky)
        with caplog.at_level("WARNING", logger="crawlers.csv"):
            leads = await crawler.crawl(10)

        assert [lead.email for lead in leads] == ["ada@acme.com"]
        warning = log_record(caplog, "could not map csv row; skipping it")
        assert warning.record == "row 3"
        assert "unreadable cell" in warning.error

    async def test_the_limit_stops_the_file_being_read_further(self, tmp_path: Path) -> None:
        # The reader is a generator precisely so a limit does not require
        # materializing the whole file first.
        path = tmp_path / "long.csv"
        rows = "".join(f"user{index}@acme.com,Acme Corp\n" for index in range(5000))
        path.write_text(f"Email,Company\n{rows}", encoding="utf-8")
        assert len(await CsvCrawler(csv_settings(path)).crawl(3)) == 3


class TestVietnameseCsvSource:
    """A CSV as a Vietnamese vendor exports it, read end to end.

    The header row is Vietnamese, so nothing matches the built-in English
    aliases and the run depends on ``column_map`` — which is exactly how a real
    user reads one of these files, and a path no other test exercises. The
    diacritics run through the whole adapter, and one row carries a sentence in
    its website column instead of a domain.
    """

    #: ``target=Source Header``, the way the CLI takes it. Written out rather
    #: than derived from the fixture so the header text is asserted, not assumed.
    HEADER_MAP = [
        "full_name=Họ và tên",
        "job_title=Chức danh",
        "email=Email",
        "company_name=Công ty",
        "company_website=Website",
        "company_city=Thành phố",
        "company_country=Quốc gia",
    ]

    @pytest.fixture
    def vietnamese_csv(self, tmp_path: Path) -> Path:
        path = tmp_path / "khach-hang.csv"
        path.write_text(VIETNAMESE_CSV, encoding="utf-8")
        return path

    async def test_every_row_is_read(self, vietnamese_csv: Path) -> None:
        leads = await CsvCrawler(csv_settings(vietnamese_csv, column_map=self.HEADER_MAP)).crawl(
            100
        )
        assert len(leads) == 3

    async def test_vietnamese_headers_map_onto_the_schema(self, vietnamese_csv: Path) -> None:
        first = (
            await CsvCrawler(csv_settings(vietnamese_csv, column_map=self.HEADER_MAP)).crawl(100)
        )[0]
        assert first.full_name == "Nguyễn Văn An"
        assert first.job_title == "Giám đốc Kỹ thuật"
        assert first.email == "an.nguyen@congty.vn"
        assert first.company_name == "Công ty TNHH Giải pháp Số"
        assert first.company_city == "Hà Nội"
        assert first.company_country == "Việt Nam"

    async def test_diacritics_are_not_folded_by_the_adapter(self, vietnamese_csv: Path) -> None:
        # The adapter maps columns and nothing else; if it were transliterating,
        # the accents would be gone before the normalizer ever saw them.
        leads = await CsvCrawler(csv_settings(vietnamese_csv, column_map=self.HEADER_MAP)).crawl(
            100
        )
        assert [lead.full_name for lead in leads] == [
            "Nguyễn Văn An",
            "Trần Thị Bích",
            "Phạm Minh Đức",
        ]

    async def test_a_sentence_in_the_website_column_does_not_stop_the_read(
        self, vietnamese_csv: Path
    ) -> None:
        # The third row's website is prose, which yields no domain. It must cost
        # that row its website and nothing else: the row survives, the domain is
        # recovered from the corporate address instead, and the unusable value
        # is recorded rather than silently dropped.
        leads = await CsvCrawler(csv_settings(vietnamese_csv, column_map=self.HEADER_MAP)).crawl(
            100
        )
        third = leads[2]
        assert third.company_website == "see our website it is great"
        assert third.full_name == "Phạm Minh Đức"

        lead = Normalizer().normalize(third)
        assert lead.company.domain == "khac.vn"  # from duc.pham@khac.vn
        # The website is rebuilt from that recovered domain rather than kept as
        # the unusable text — the prose is discarded, not stored in its place.
        assert lead.company.website == "https://khac.vn"
        assert [issue.rule for issue in lead.normalization_issues] == ["malformed_url"]
        assert lead.normalization_issues[0].field == "company_website"

    async def test_the_other_rows_keep_the_domain_from_their_website(
        self, vietnamese_csv: Path
    ) -> None:
        # The contrast that makes the row above meaningful: a real domain in the
        # website column is used, and raises no issue.
        leads = await CsvCrawler(csv_settings(vietnamese_csv, column_map=self.HEADER_MAP)).crawl(
            100
        )
        lead = Normalizer().normalize(leads[0])
        assert lead.company.domain == "congty.vn"
        assert lead.normalization_issues == []

    async def test_the_header_row_is_not_read_as_a_lead(self, vietnamese_csv: Path) -> None:
        leads = await CsvCrawler(csv_settings(vietnamese_csv, column_map=self.HEADER_MAP)).crawl(
            100
        )
        assert "Họ và tên" not in [lead.full_name for lead in leads]

    async def test_the_limit_applies(self, vietnamese_csv: Path) -> None:
        crawler = CsvCrawler(csv_settings(vietnamese_csv, column_map=self.HEADER_MAP))
        assert len(await crawler.crawl(2)) == 2
