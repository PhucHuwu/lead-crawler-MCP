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
    write_run_report,
)
from src.exporters.base import ExportResult, available_formats
from src.models.enums import ExportFormat, RejectionReason
from src.models.results import CrawlResult, CrawlStats, RejectedLead
from src.utils.errors import ExportError
from tests.conftest import make_lead


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
        assert rows[0]["full_name"] == "Ada Lovelace"
        assert rows[0]["company_name"] == "Acme Corp"

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
            assert next(reader)["full_name"] == "Ada Lovelace"

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
        assert row["email"] == "" and row["full_name"] == ""
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

    async def test_build_path_uses_the_prefix_and_timestamp(self, settings: Settings) -> None:
        path = CsvExporter(settings).build_path(Path("out"), "leads", "20260101T000000Z")
        assert path == Path("out/leads_20260101T000000Z.csv")


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
        path = JsonLinesExporter(settings).build_path(Path("out"), "leads", "T")
        assert path.suffix == ".jsonl"


class TestExportResult:
    def test_describe_names_the_file_and_size(self, tmp_path: Path) -> None:
        result = ExportResult(
            format=ExportFormat.CSV, path=tmp_path / "a.csv", records=3, bytes_written=2048
        )
        assert "3 leads" in result.describe()
        assert "2.0 KiB" in result.describe()


class TestRunReport:
    def build_result(self) -> CrawlResult:
        stats = CrawlStats(raw_collected=10, normalized=10, filtered_out=2, exported=1)
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
