"""Tests for the output writers and the run report.

Two properties matter beyond "the file exists": the CSV header must stay in sync
with the model (a drifted column silently drops a field for every future run),
and the run report must be safe to attach to a ticket.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from src.config import Settings, load_settings
from src.exporters import (
    LEAD_COLUMNS,
    CsvExporter,
    JsonExporter,
    JsonLinesExporter,
    build_exporter,
    build_report,
    lead_to_dict,
    registered_exporters,
    source_slug,
    write_run_report,
)
from src.exporters.base import UNKNOWN_SOURCE_SLUG, ExportResult, available_formats
from src.models.enums import ExportFormat, RejectionReason
from src.models.results import CrawlResult, CrawlStats, RejectedLead
from src.processors.normalizer import Normalizer
from src.utils.errors import ExportError
from tests.conftest import make_lead
from tests.fixtures.records import (
    INTERNATIONAL_COMPANIES,
    INTERNATIONAL_NAMES,
    complete_lead,
    missing_company,
    missing_email,
    sparse_lead,
    vietnamese_lead,
)


class TestRegistry:
    def test_builtin_formats_are_registered(self) -> None:
        assert {ExportFormat.CSV, ExportFormat.JSON, ExportFormat.JSONL} <= set(
            registered_exporters()
        )

    def test_available_formats_follow_enum_order(self) -> None:
        formats = available_formats()
        assert formats == [fmt for fmt in ExportFormat if fmt in set(formats)]

    def test_format_without_an_exporter_is_rejected(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Reached when an ExportFormat member is added but its writer is not —
        # the registry is emptied here to stand in for that omission.
        from src.exporters import base as exporter_base

        monkeypatch.setattr(exporter_base, "_REGISTRY", {})
        with pytest.raises(ExportError, match="no exporter registered"):
            build_exporter(ExportFormat.CSV, settings)

    def test_each_exporter_declares_an_extension_and_description(self) -> None:
        for entry in registered_exporters().values():
            assert entry.extension.startswith(".")
            assert entry.description


class TestCsvExporter:
    async def test_header_matches_the_model(self, settings: Settings) -> None:
        # If these drift, a field stops being written and nobody notices.
        assert tuple(make_lead().flatten()) == LEAD_COLUMNS

    async def test_writes_a_header_and_one_row_per_lead(
        self, settings: Settings, tmp_path: Path
    ) -> None:
        path = tmp_path / "leads.csv"
        result = await CsvExporter(settings).export([make_lead(), make_lead()], path)

        with path.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))

        assert result.records == 2
        assert result.format is ExportFormat.CSV
        assert result.bytes_written == path.stat().st_size
        assert len(rows) == 2
        assert rows[0]["person_full_name"] == "Ada Lovelace"
        assert rows[0]["company_name"] == "Acme Corp"

    async def test_the_raw_job_title_reaches_its_own_column(
        self, settings: Settings, tmp_path: Path
    ) -> None:
        # Normalization threw away the "at Acme Corp" clause; the export is the
        # last place that clause can still be seen, so it needs its own column.
        lead = make_lead(job_title="CTO")
        lead.person.job_title_raw = "CTO at Acme Corp"
        path = tmp_path / "titles.csv"
        await CsvExporter(settings).export([lead, make_lead()], path)

        with path.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))

        assert rows[0]["person_job_title"] == "CTO"
        assert rows[0]["person_job_title_raw"] == "CTO at Acme Corp"
        # A title the normalizer left alone stays blank rather than duplicating
        # the column beside it, which keeps the sheet readable.
        assert rows[1]["person_job_title_raw"] == ""

    async def test_provenance_reaches_its_own_column(
        self, settings: Settings, tmp_path: Path
    ) -> None:
        # After a merge, the export is the only place the contributing sources
        # are visible as a list; the columns beside it name just the primary.
        lead = make_lead(provider="apollo")
        path = tmp_path / "sources.csv"
        await CsvExporter(settings).export([lead, make_lead()], path)

        with path.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))

        assert rows[0]["sources"] == "apollo"
        assert rows[0]["source_provider"] == "apollo"

    async def test_multiple_sources_are_semicolon_separated(
        self, settings: Settings, tmp_path: Path
    ) -> None:
        # A comma would be read as a column break by every spreadsheet that opens
        # the file, which is exactly what this column exists to be read from.
        lead = make_lead(provider="apollo")
        lead.source.sources.append("company_website")
        path = tmp_path / "multi-sources.csv"
        await CsvExporter(settings).export([lead], path)

        with path.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))

        assert rows[0]["sources"] == "apollo; company_website"

    async def test_empty_result_still_produces_a_usable_header(
        self, settings: Settings, tmp_path: Path
    ) -> None:
        # An empty export is the common case for a too-strict filter; the file
        # should still open as a valid spreadsheet.
        path = tmp_path / "empty.csv"
        result = await CsvExporter(settings).export([], path)

        header = path.read_text(encoding="utf-8-sig").strip().splitlines()
        assert result.records == 0
        assert header == [",".join(LEAD_COLUMNS)]

    async def test_a_bom_is_written_so_excel_detects_utf8(
        self, settings: Settings, tmp_path: Path
    ) -> None:
        path = tmp_path / "bom.csv"
        await CsvExporter(settings).export([make_lead(full_name="José Alvarez")], path)
        assert path.read_bytes().startswith(b"\xef\xbb\xbf")

    async def test_delimiter_is_configurable(self, tmp_path: Path) -> None:
        # European locales expect semicolons.
        path = tmp_path / "semi.csv"
        exporter = CsvExporter(load_settings(output_csv_delimiter=";"))
        await exporter.export([make_lead()], path)

        with path.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle, delimiter=";")
            assert next(reader)["person_full_name"] == "Ada Lovelace"

    async def test_embedded_delimiters_are_quoted(self, settings: Settings, tmp_path: Path) -> None:
        path = tmp_path / "quoted.csv"
        await CsvExporter(settings).export([make_lead(company_name='Acme, "Inc"')], path)
        with path.open(encoding="utf-8-sig", newline="") as handle:
            assert next(csv.DictReader(handle))["company_name"] == 'Acme, "Inc"'

    async def test_missing_person_renders_as_empty_cells(
        self, settings: Settings, tmp_path: Path
    ) -> None:
        path = tmp_path / "sparse.csv"
        lead = make_lead(
            email=None,
            phone=None,
            linkedin_url=None,
            full_name=None,
            first_name=None,
            last_name=None,
        )
        await CsvExporter(settings).export([lead], path)
        with path.open(encoding="utf-8-sig", newline="") as handle:
            row = next(csv.DictReader(handle))
        assert row["person_email"] == "" and row["person_full_name"] == ""
        assert row["lead_id"]  # the id is always present

    async def test_existing_file_is_replaced_not_appended(
        self, settings: Settings, tmp_path: Path
    ) -> None:
        path = tmp_path / "leads.csv"
        exporter = CsvExporter(settings)
        await exporter.export([make_lead(), make_lead(), make_lead()], path)
        await exporter.export([make_lead()], path)

        with path.open(encoding="utf-8-sig", newline="") as handle:
            assert len(list(csv.DictReader(handle))) == 1

    async def test_a_failed_write_raises_and_leaves_no_debris(
        self, settings: Settings, tmp_path: Path
    ) -> None:
        # A path that is already a directory cannot be replaced by a file, so the
        # write fails at publication time — after the content was written.
        target = tmp_path / "occupied"
        target.mkdir()

        with pytest.raises(ExportError, match="could not write CSV"):
            await CsvExporter(settings).export([make_lead()], target)

        # The half-written temporary file must not be left behind.
        assert list(tmp_path.glob(".*.tmp")) == []
        assert target.is_dir()

    async def test_written_files_are_world_readable(
        self, settings: Settings, tmp_path: Path
    ) -> None:
        # NamedTemporaryFile creates 0600; exports are shared business files.
        path = tmp_path / "leads.csv"
        await CsvExporter(settings).export([make_lead()], path)
        assert path.stat().st_mode & 0o644 == 0o644

    async def test_build_path_names_the_prefix_source_and_timestamp(
        self, settings: Settings
    ) -> None:
        path = CsvExporter(settings).build_path(
            Path("out"), "leads", "apollo", "2026-10-05_14-03-22"
        )
        assert path == Path("out/leads_apollo_2026-10-05_14-03-22.csv")


class TestJsonExporter:
    async def test_writes_a_valid_array_with_the_nested_schema(
        self, settings: Settings, tmp_path: Path
    ) -> None:
        path = tmp_path / "leads.json"
        result = await JsonExporter(settings).export([make_lead()], path)

        payload = json.loads(path.read_text(encoding="utf-8"))
        assert result.records == 1
        assert payload[0]["person"]["full_name"] == "Ada Lovelace"
        assert payload[0]["company"]["name"] == "Acme Corp"
        assert payload[0]["source"]["provider"] == "test"

    async def test_computed_completeness_is_serialized(
        self, settings: Settings, tmp_path: Path
    ) -> None:
        path = tmp_path / "leads.json"
        await JsonExporter(settings).export([make_lead()], path)
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert 0.0 < payload[0]["completeness"] <= 1.0

    async def test_enums_and_timestamps_are_plain_json_types(
        self, settings: Settings, tmp_path: Path
    ) -> None:
        path = tmp_path / "leads.json"
        await JsonExporter(settings).export([make_lead()], path)
        raw = path.read_text(encoding="utf-8")
        entry = json.loads(raw)[0]
        # No custom encoder should be needed downstream.
        assert isinstance(entry["person"]["seniority"], str)
        assert entry["source"]["collected_at"].endswith("Z")

    async def test_non_ascii_is_preserved(self, settings: Settings, tmp_path: Path) -> None:
        path = tmp_path / "leads.json"
        await JsonExporter(settings).export([make_lead(first_name="José")], path)
        assert "José" in path.read_text(encoding="utf-8")

    async def test_empty_result_is_an_empty_array(self, settings: Settings, tmp_path: Path) -> None:
        path = tmp_path / "leads.json"
        await JsonExporter(settings).export([], path)
        assert json.loads(path.read_text(encoding="utf-8")) == []

    def test_lead_to_dict_includes_computed_fields(self) -> None:
        assert "completeness" in lead_to_dict(make_lead())


class TestJsonLinesExporter:
    async def test_one_object_per_line(self, settings: Settings, tmp_path: Path) -> None:
        path = tmp_path / "leads.jsonl"
        result = await JsonLinesExporter(settings).export([make_lead(), make_lead()], path)

        lines = path.read_text(encoding="utf-8").splitlines()
        assert result.records == 2
        assert len(lines) == 2
        # Compact: no indentation, so a line is always a single record.
        assert "\n" not in lines[0]
        assert json.loads(lines[0])["person"]["full_name"] == "Ada Lovelace"

    async def test_empty_result_is_an_empty_file(self, settings: Settings, tmp_path: Path) -> None:
        path = tmp_path / "leads.jsonl"
        result = await JsonLinesExporter(settings).export([], path)
        assert result.records == 0
        assert path.read_text(encoding="utf-8") == ""

    async def test_build_path_uses_the_jsonl_extension(self, settings: Settings) -> None:
        path = JsonLinesExporter(settings).build_path(Path("out"), "leads", "apollo", "T")
        assert path.suffix == ".jsonl"


class TestSourceSlug:
    """The run's sources become one filename component.

    A name that reaches a filesystem has to be safe there and stable across
    runs; both properties are cheaper to assert here than to debug as a file
    someone cannot find.
    """

    def test_a_single_source_keeps_its_name(self) -> None:
        assert source_slug(["apollo"]) == "apollo"

    def test_several_sources_are_sorted_so_listing_order_does_not_matter(self) -> None:
        assert source_slug(["website", "apollo"]) == "apollo-website"
        assert source_slug(["apollo", "website"]) == source_slug(["website", "apollo"])

    def test_repeats_are_collapsed(self) -> None:
        assert source_slug(["apollo", "apollo"]) == "apollo"

    def test_a_hostile_name_cannot_introduce_a_path_separator(self) -> None:
        # Nothing here is expected from a real provider; the point is that the
        # slug is built by whitelisting rather than by escaping, so no input can
        # climb out of the output directory or add a second extension.
        slug = source_slug(["Apollo.io/../../etc/passwd"])
        assert "/" not in slug and ".." not in slug
        assert slug == "apollo-io-etc-passwd"

    def test_a_run_with_no_usable_source_name_still_names_a_file(self) -> None:
        assert source_slug([]) == UNKNOWN_SOURCE_SLUG
        assert source_slug(["", "///"]) == UNKNOWN_SOURCE_SLUG


class TestUnicodePreservation:
    """Vietnamese and other non-Latin text must survive every writer intact.

    The failure mode here is silent: a mangled export still opens, still parses,
    and is simply wrong — and it is found by whoever tries to contact the person
    it misnames. So these assert on bytes as well as on the parsed value.
    """

    #: Diacritics on both a vowel and a consonant (Nguyễn, Hương), which makes
    #: this a better probe than an accent-only name.
    VIETNAMESE = "Nguyễn Thị Hương"

    async def test_csv_round_trips_vietnamese(self, settings: Settings, tmp_path: Path) -> None:
        path = tmp_path / "leads.csv"
        await CsvExporter(settings).export([make_lead(full_name=self.VIETNAMESE)], path)

        with path.open(encoding="utf-8-sig", newline="") as handle:
            row = next(csv.DictReader(handle))

        assert row["person_full_name"] == self.VIETNAMESE

    async def test_csv_holds_the_utf8_bytes_themselves(
        self, settings: Settings, tmp_path: Path
    ) -> None:
        # Read as bytes, not text: this is the assertion that fails if the file
        # were written as latin-1, or with the diacritics folded to ASCII.
        path = tmp_path / "leads.csv"
        await CsvExporter(settings).export([make_lead(full_name=self.VIETNAMESE)], path)
        assert self.VIETNAMESE.encode("utf-8") in path.read_bytes()

    async def test_json_keeps_characters_literal_rather_than_escaped(
        self, settings: Settings, tmp_path: Path
    ) -> None:
        # ``ensure_ascii`` would render this as ễện: still valid JSON,
        # but no longer readable in an editor or by grep, which is how these
        # files are actually inspected.
        path = tmp_path / "leads.json"
        await JsonExporter(settings).export([make_lead(full_name=self.VIETNAMESE)], path)

        raw = path.read_text(encoding="utf-8")
        assert self.VIETNAMESE in raw
        assert "\\u1ec5" not in raw
        assert json.loads(raw)[0]["person"]["full_name"] == self.VIETNAMESE

    async def test_jsonl_round_trips_vietnamese(self, settings: Settings, tmp_path: Path) -> None:
        path = tmp_path / "leads.jsonl"
        await JsonLinesExporter(settings).export([make_lead(full_name=self.VIETNAMESE)], path)

        line = path.read_text(encoding="utf-8").splitlines()[0]
        assert json.loads(line)["person"]["full_name"] == self.VIETNAMESE

    async def test_scripts_beyond_latin_survive_too(
        self, settings: Settings, tmp_path: Path
    ) -> None:
        # Vietnamese is the stated requirement; this keeps the guarantee from
        # being accidentally narrowed to one script.
        company = "東京テクノロジー株式会社"
        path = tmp_path / "leads.csv"
        await CsvExporter(settings).export([make_lead(company_name=company)], path)

        with path.open(encoding="utf-8-sig", newline="") as handle:
            row = next(csv.DictReader(handle))

        assert row["company_name"] == company

    async def test_vietnamese_survives_the_run_report(
        self, settings: Settings, tmp_path: Path
    ) -> None:
        # The rejection sample is the one place a lead's own text reaches the
        # report, so the report needs the same guarantee as the exports.
        result = CrawlResult(
            rejections=[
                RejectedLead(
                    provider="apollo",
                    label=self.VIETNAMESE,
                    reason=RejectionReason.FILTERED_OUT,
                )
            ]
        )
        path = tmp_path / "report.json"
        await write_run_report(result, path, settings)

        assert self.VIETNAMESE in path.read_text(encoding="utf-8")


class TestRealisticRecordExport:
    """Whole records through the whole output path.

    The per-field tests above prove each writer handles each value. This proves
    the *pipeline's* output is faithful: fixtures shaped like real source records
    are normalized and written, and the values that survive are compared to what
    went in. That is the layer where a rename, a dropped column or an over-eager
    cleaner would actually be noticed, because it is the layer the user sees.
    """

    @pytest.fixture
    def normalizer(self) -> Normalizer:
        return Normalizer()

    @pytest.mark.parametrize(
        "raw_factory",
        [complete_lead, missing_email, missing_company, sparse_lead],
        ids=["complete", "no-email", "no-company", "sparse"],
    )
    async def test_a_normalized_record_survives_json_unchanged(
        self, settings: Settings, tmp_path: Path, normalizer: Normalizer, raw_factory: object
    ) -> None:
        raw = raw_factory()  # type: ignore[operator]
        lead = normalizer.normalize(raw)
        path = tmp_path / "leads.json"
        await JsonExporter(settings).export([lead], path)

        entry = json.loads(path.read_text(encoding="utf-8"))[0]
        assert entry["person"]["email"] == lead.person.email
        assert entry["company"]["name"] == lead.company.name
        assert entry["company"]["domain"] == lead.company.domain
        assert entry["lead_id"] == lead.lead_id

    @pytest.mark.parametrize(
        "raw_factory",
        [complete_lead, missing_email, missing_company, sparse_lead],
        ids=["complete", "no-email", "no-company", "sparse"],
    )
    async def test_a_normalized_record_survives_csv_unchanged(
        self, settings: Settings, tmp_path: Path, normalizer: Normalizer, raw_factory: object
    ) -> None:
        raw = raw_factory()  # type: ignore[operator]
        lead = normalizer.normalize(raw)
        path = tmp_path / "leads.csv"
        await CsvExporter(settings).export([lead], path)

        with path.open(encoding="utf-8-sig", newline="") as handle:
            row = next(csv.DictReader(handle))

        assert row["person_email"] == (lead.person.email or "")
        assert row["company_name"] == (lead.company.name or "")
        assert row["lead_id"] == lead.lead_id

    async def test_every_declared_column_is_written(
        self, settings: Settings, tmp_path: Path, normalizer: Normalizer
    ) -> None:
        # A field present on the model but missing from the header is dropped
        # for every future run, silently. Comparing the written header to
        # ``LEAD_COLUMNS`` is what catches a model change that forgot the CSV.
        path = tmp_path / "leads.csv"
        lead = normalizer.normalize(complete_lead())
        await CsvExporter(settings).export([lead], path)

        with path.open(encoding="utf-8-sig", newline="") as handle:
            header = next(csv.reader(handle))

        assert tuple(header) == LEAD_COLUMNS
        assert len(header) == len(set(header)), "duplicate column names"

    async def test_a_fully_populated_record_writes_no_blank_cells(
        self, settings: Settings, tmp_path: Path, normalizer: Normalizer
    ) -> None:
        # ``complete_lead`` claims to be the "nothing is missing" reference, so
        # every column it feeds should be populated. A blank here means either
        # the fixture or the normalizer is dropping a field it was given.
        path = tmp_path / "leads.csv"
        await CsvExporter(settings).export([normalizer.normalize(complete_lead())], path)

        with path.open(encoding="utf-8-sig", newline="") as handle:
            row = next(csv.DictReader(handle))

        empty = [name for name, value in row.items() if value == ""]
        assert empty == [], f"columns unexpectedly empty: {empty}"

    @pytest.mark.parametrize(
        ("value", "script"),
        [*INTERNATIONAL_NAMES, *INTERNATIONAL_COMPANIES],
    )
    async def test_every_script_round_trips_through_every_format(
        self, settings: Settings, tmp_path: Path, value: str, script: str
    ) -> None:
        # The same text through all three writers, compared as bytes as well as
        # parsed values: a writer that transliterated or escaped would still
        # produce a file that opens.
        leads = [make_lead(full_name=value, company_name=value)]
        csv_path, json_path, jsonl_path = (
            tmp_path / "leads.csv",
            tmp_path / "leads.json",
            tmp_path / "leads.jsonl",
        )

        await CsvExporter(settings).export(leads, csv_path)
        await JsonExporter(settings).export(leads, json_path)
        await JsonLinesExporter(settings).export(leads, jsonl_path)

        with csv_path.open(encoding="utf-8-sig", newline="") as handle:
            assert next(csv.DictReader(handle))["person_full_name"] == value

        assert json.loads(json_path.read_text(encoding="utf-8"))[0]["person"]["full_name"] == value
        line = jsonl_path.read_text(encoding="utf-8").splitlines()[0]
        assert json.loads(line)["person"]["full_name"] == value

        # Byte-level, so a lossy encode cannot pass on a lucky decode.
        assert value.encode("utf-8") in csv_path.read_bytes()
        assert value.encode("utf-8") in json_path.read_bytes()
        assert value.encode("utf-8") in jsonl_path.read_bytes()

    async def test_a_vietnamese_record_from_a_messy_source_lands_intact(
        self, settings: Settings, tmp_path: Path, normalizer: Normalizer
    ) -> None:
        # End to end from the raw shape a Vietnamese vendor would export,
        # including the row whose website column holds a sentence.
        lead = normalizer.normalize(vietnamese_lead("NFD"))
        path = tmp_path / "leads.csv"
        await CsvExporter(settings).export([lead], path)

        with path.open(encoding="utf-8-sig", newline="") as handle:
            row = next(csv.DictReader(handle))

        # Composed on the way out regardless of how it arrived — a decomposed
        # export is a file that looks fine and breaks every string comparison.
        assert row["person_full_name"] == "Nguyễn Văn An"
        assert row["company_city"] == "Hà Nội"
        # The legal form arrives folded (`TNHH` -> `Tnhh`); see
        # TestVietnameseData::test_legal_forms_are_folded_like_shouting for why
        # that is recorded as a limitation rather than fixed here.
        assert row["company_name"] == "Công ty Tnhh Giải pháp Số"


class TestExportResult:
    def test_describe_names_the_file_and_size(self, tmp_path: Path) -> None:
        result = ExportResult(
            format=ExportFormat.CSV, path=tmp_path / "a.csv", records=3, bytes_written=2048
        )
        assert "3 leads" in result.describe()
        assert "2.0 KiB" in result.describe()


class TestRunReport:
    def build_result(self) -> CrawlResult:
        stats = CrawlStats(
            raw_collected=10,
            normalized=10,
            filtered_out=2,
            exported=1,
            records_before_deduplication=10,
            exact_duplicates=2,
            probable_duplicates=1,
        )
        stats.per_filter_reason["exclude_free_email"] = 2
        stats.source_errors["apollo"] = "boom"
        stats.finalize()
        return CrawlResult(
            leads=[make_lead()],
            stats=stats,
            rejections=[
                RejectedLead(
                    provider="test",
                    label="Ada Lovelace",
                    reason=RejectionReason.FILTERED_OUT,
                    detail="free email",
                )
            ],
        )

    def test_reports_counters_and_attribution(self, settings: Settings) -> None:
        report = build_report(self.build_result(), settings)
        assert report["stats"]["raw_collected"] == 10
        assert report["rejections"]["by_rule"] == {"exclude_free_email": 2}
        assert report["sources"]["errors"] == {"apollo": "boom"}
        assert report["rejections"]["recorded"] == 1
        assert report["rejections"]["sample"][0]["reason"] == "filtered_out"

    def test_the_summary_states_the_whole_run_in_one_block(self, settings: Settings) -> None:
        summary = build_report(self.build_result(), settings)["summary"]
        assert summary["discovered"] == 10
        assert summary["valid"] == 10
        assert summary["filtered"] == 2
        assert summary["duplicates_removed"] == 3
        assert summary["exported"] == 1
        assert summary["errors"] == 0

    def test_the_summary_carries_exactly_the_documented_keys(self, settings: Settings) -> None:
        # A stable, small shape: consumers of the summary should not have to
        # re-check which keys exist each release.
        summary = build_report(self.build_result(), settings)["summary"]
        assert set(summary) == {
            "source",
            "started_at",
            "finished_at",
            "discovered",
            "valid",
            "filtered",
            "duplicates_removed",
            "exported",
            "errors",
            "pages_visited",
            "browser_errors",
            "selector_failures",
            "auth_failures",
        }

    def test_the_summary_distinguishes_never_reaching_a_site_from_finding_nothing(
        self, settings: Settings
    ) -> None:
        # Zero leads is ambiguous on its own — an empty site and a crawl that
        # never loaded a page produce the same record counts. The browser
        # counters are what tell the two apart, so they belong in the summary
        # rather than only in the nested stats block.
        result = self.build_result()
        result.stats.record_browser_counts(
            {
                "pages_visited": 7,
                "browser_errors": 1,
                "selector_failures": 2,
                "auth_failures": 3,
            }
        )
        result.stats.finalize()
        summary = build_report(result, settings)["summary"]
        assert summary["pages_visited"] == 7
        assert summary["browser_errors"] == 1
        assert summary["selector_failures"] == 2
        assert summary["auth_failures"] == 3

    def test_the_summary_errors_count_records_that_could_not_be_used(
        self, settings: Settings
    ) -> None:
        # Distinct from `sources.errors` below, which counts failed providers:
        # this is the record-level figure, and the two are never the same number.
        result = self.build_result()
        result.stats.validation_failed = 3
        result.stats.record_validation("require_email")
        result.stats.finalize()
        assert build_report(result, settings)["summary"]["errors"] == 3

    def test_the_summary_names_the_requested_sources(self, settings: Settings) -> None:
        report = build_report(self.build_result(), settings, sources=["apollo", "website"])
        assert report["summary"]["source"] == "apollo-website"

    def test_the_summary_source_is_the_one_in_the_export_filename(self, settings: Settings) -> None:
        # The report and the files must agree about which run they describe;
        # they share one slug so they cannot drift apart.
        report = build_report(self.build_result(), settings, sources=["apollo"])
        slug = report["summary"]["source"]
        path = CsvExporter(settings).build_path(Path("out"), "leads", slug, "2026-10-05_14-03-22")
        assert path.name == "leads_apollo_2026-10-05_14-03-22.csv"

    def test_a_run_without_a_recorded_source_still_reports_one(self, settings: Settings) -> None:
        assert build_report(CrawlResult(), settings)["summary"]["source"] == UNKNOWN_SOURCE_SLUG

    def test_the_summary_timestamps_bound_the_run(self, settings: Settings) -> None:
        summary = build_report(self.build_result(), settings)["summary"]
        assert summary["started_at"] <= summary["finished_at"]

    def test_reports_all_four_deduplication_statistics(self, settings: Settings) -> None:
        # `records_after_deduplication` is derived, so it is absent from `stats`
        # have to know which of the four are stored and which are arithmetic.
        report = build_report(self.build_result(), settings)
        assert report["deduplication"] == {
            "records_before_deduplication": 10,
            "exact_duplicates": 2,
            "probable_duplicates": 1,
            "records_after_deduplication": 7,
        }

    def test_the_deduplication_statistics_agree_with_each_other(self, settings: Settings) -> None:
        dedup = build_report(self.build_result(), settings)["deduplication"]
        assert (
            dedup["records_before_deduplication"]
            - dedup["exact_duplicates"]
            - dedup["probable_duplicates"]
            == dedup["records_after_deduplication"]
        )

    def test_active_filters_are_echoed(self) -> None:
        configured = load_settings(filters={"require_email": True, "include_countries": ["US"]})
        report = build_report(CrawlResult(), configured)
        assert report["run"]["filters"]["require_email"] is True
        assert report["run"]["filters"]["include_countries"] == ["US"]
        assert report["run"]["filters_active"] is True

    def test_report_never_contains_credentials(self) -> None:
        # The report is meant to be attachable to a ticket.
        configured = load_settings(apollo={"api_key": "super-secret-key"})
        serialized = json.dumps(build_report(self.build_result(), configured))
        assert "super-secret-key" not in serialized
        assert "api_key" not in serialized

    async def test_writes_the_report_atomically(self, settings: Settings, tmp_path: Path) -> None:
        path = tmp_path / "report.json"
        written = await write_run_report(self.build_result(), path, settings, limit=50)
        assert written == path
        assert json.loads(path.read_text(encoding="utf-8"))["run"]["limit_per_source"] == 50

    async def test_limit_is_null_when_not_supplied(
        self, settings: Settings, tmp_path: Path
    ) -> None:
        path = tmp_path / "report.json"
        await write_run_report(CrawlResult(), path, settings)
        assert json.loads(path.read_text(encoding="utf-8"))["run"]["limit_per_source"] is None

    async def test_unwritable_destination_raises_export_error(
        self, settings: Settings, tmp_path: Path
    ) -> None:
        target = tmp_path / "occupied"
        target.mkdir()
        with pytest.raises(ExportError, match="could not write run report"):
            await write_run_report(CrawlResult(), target, settings)
