"""Tests for the command-line entry point.

The exit codes are a contract: a scheduler or a shell pipeline branches on them,
so each one gets a test that proves it is reachable.
"""

from __future__ import annotations

import csv
import json
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest

from src.crawlers import registry as registry_module
from src.crawlers.base import BaseCrawler
from src.crawlers.registry import register_crawler
from src.main import (
    EXIT_ALL_SOURCES_FAILED,
    EXIT_CONFIG,
    EXIT_EMPTY,
    EXIT_ERROR,
    EXIT_OK,
    cli,
)
from src.models.lead import RawLead
from src.utils.errors import CrawlerError
from src.utils.logging import configure_logging


@pytest.fixture(autouse=True)
def _restore_logging() -> Iterator[None]:
    """Put the root handlers back after each CLI invocation.

    ``configure_logging`` binds a handler to the *current* stderr, which pytest
    replaces during capture. Without this, a later test that logs would write to
    a closed stream.
    """
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    yield
    for handler in list(root.handlers):
        root.removeHandler(handler)
    for handler in saved_handlers:
        root.addHandler(handler)
    root.setLevel(saved_level)
    configure_logging("CRITICAL", "console")


@pytest.fixture
def csv_with_one_email_less_row(tmp_path: Path) -> Path:
    """Three usable leads, one of which has no email address."""
    path = tmp_path / "input.csv"
    path.write_text(
        "First Name,Last Name,Job Title,Company,Website,Email\n"
        "Ada,Lovelace,CTO,Acme Corp,acme.com,ada@acme.com\n"
        "Grace,Hopper,COO,Navy Systems,navy.example.com,\n"
        "Alan,Turing,Head of Research,Bletchley Park,"
        "bletchley.example.com,alan@bletchley.example.com\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def exploding_source(monkeypatch: pytest.MonkeyPatch) -> str:
    """Register a source that is available but always fails mid-crawl.

    Registration is the real path a new adapter takes, and it produces the only
    situation the "all sources failed" exit code can be reached from: the source
    passes its configuration check, so the failure happens during collection.
    """
    monkeypatch.setattr(registry_module, "_REGISTRY", dict(registry_module._REGISTRY))

    @register_crawler
    class ExplodingCrawler(BaseCrawler):
        provider = "exploding"
        display_name = "Exploding source"
        description = "Always fails; used to exercise the failure path."

        async def crawl(self, limit: int) -> list[RawLead]:
            raise CrawlerError(self.provider, "the source is down")

    return "exploding"


def exported_leads(directory: Path, suffix: str = ".csv") -> list[dict[str, str]]:
    """Read back whatever the run wrote, so tests assert on real output.

    Matches the configured ``leads_`` filename prefix rather than every file in
    the directory, because the input CSV the run read from also lives there.
    """
    files = sorted(directory.glob(f"leads_*{suffix}"))
    assert len(files) == 1, f"expected exactly one {suffix} export, found {files}"
    text = files[0].read_text(encoding="utf-8-sig")
    return list(csv.DictReader(text.splitlines()))


def written_names(directory: Path) -> list[str]:
    """Names of the files the run produced, excluding the run report."""
    return sorted(
        path.name for path in directory.glob("leads_*") if not path.name.endswith("_report.json")
    )


def read_report(directory: Path) -> dict[str, Any]:
    reports = list(directory.glob("*_report.json"))
    assert len(reports) == 1
    return cast("dict[str, Any]", json.loads(reports[0].read_text(encoding="utf-8")))


class TestInformationalFlags:
    def test_version_exits_zero(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as excinfo:
            cli(["--version"])
        assert excinfo.value.code == EXIT_OK
        assert "lead-crawler" in capsys.readouterr().out

    def test_list_sources_marks_credentialed_sources(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert cli(["--list-sources"]) == EXIT_OK
        out = capsys.readouterr().out
        assert "mock" in out
        assert "csv" in out
        assert "apollo" in out
        assert "(requires credentials)" in out

    def test_list_formats(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert cli(["--list-formats"]) == EXIT_OK
        out = capsys.readouterr().out
        for fmt in ("csv", "json", "jsonl"):
            assert fmt in out

    def test_listing_writes_nothing(self, tmp_path: Path) -> None:
        cli(["--list-sources"])
        assert list(tmp_path.iterdir()) == []


class TestConfigurationErrors:
    def test_unknown_source_is_a_config_error(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert cli(["--source", "salesforce"]) == EXIT_CONFIG
        assert "unknown source" in capsys.readouterr().err

    def test_source_missing_its_required_setting(self, capsys: pytest.CaptureFixture[str]) -> None:
        # The csv source is registered but unusable without --csv-path.
        assert cli(["--source", "csv"]) == EXIT_CONFIG
        assert "--csv-path" in capsys.readouterr().err

    def test_unknown_export_format(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert cli(["--source", "mock", "--format", "xml"]) == EXIT_CONFIG
        assert "configuration error" in capsys.readouterr().err

    def test_invalid_dedup_strategy_is_rejected_by_the_parser(self) -> None:
        with pytest.raises(SystemExit) as excinfo:
            cli(["--dedup", "telepathy"])
        assert excinfo.value.code == EXIT_CONFIG

    def test_invalid_limit_is_rejected_by_the_parser(self) -> None:
        with pytest.raises(SystemExit) as excinfo:
            cli(["--limit", "many"])
        assert excinfo.value.code == EXIT_CONFIG


class TestRuns:
    def test_mock_run_writes_every_requested_format(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = cli(
            [
                "--source",
                "mock",
                "--limit",
                "25",
                "--output-dir",
                str(tmp_path),
                "--format",
                "csv,json",
                "--output-prefix",
                "leads",
            ]
        )
        assert code == EXIT_OK

        names = written_names(tmp_path)
        assert sum(name.endswith(".csv") for name in names) == 1
        assert sum(name.endswith(".json") for name in names) == 1
        assert len(list(tmp_path.glob("*_report.json"))) == 1
        # The summary goes to stdout; logs go to stderr.
        assert "Run summary" in capsys.readouterr().out

    def test_reported_counters_match_the_run(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cli(["--source", "mock", "--limit", "25", "--output-dir", str(tmp_path)])
        report = read_report(tmp_path)
        assert report["stats"]["raw_collected"] == 25
        assert report["run"]["limit_per_source"] == 25
        assert report["sources"]["collected"] == {"mock": 25}
        assert report["outputs"]

    def test_csv_source_end_to_end(self, csv_with_one_email_less_row: Path, tmp_path: Path) -> None:
        code = cli(
            [
                "--source",
                "csv",
                "--csv-path",
                str(csv_with_one_email_less_row),
                "--output-dir",
                str(tmp_path),
                "--format",
                "csv",
            ]
        )
        assert code == EXIT_OK
        rows = exported_leads(tmp_path)
        assert {row["full_name"] for row in rows} == {"Ada Lovelace", "Grace Hopper", "Alan Turing"}
        # Values are normalized on the way through, not merely copied.
        assert all(row["lead_id"] for row in rows)
        assert next(row for row in rows if row["last_name"] == "Lovelace")["job_title"] == "CTO"

    def test_lead_ids_are_stable_across_runs(
        self, csv_with_one_email_less_row: Path, tmp_path: Path
    ) -> None:
        # Determinism is what makes a later phase able to diff two runs.
        first, second = tmp_path / "one", tmp_path / "two"
        for directory in (first, second):
            cli(
                [
                    "--source",
                    "csv",
                    "--csv-path",
                    str(csv_with_one_email_less_row),
                    "--output-dir",
                    str(directory),
                    "--format",
                    "csv",
                ]
            )
        assert [row["lead_id"] for row in exported_leads(first)] == [
            row["lead_id"] for row in exported_leads(second)
        ]

    def test_dry_run_writes_nothing(
        self, csv_with_one_email_less_row: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = cli(
            [
                "--source",
                "csv",
                "--csv-path",
                str(csv_with_one_email_less_row),
                "--output-dir",
                str(tmp_path),
                "--dry-run",
            ]
        )
        assert code == EXIT_OK
        # Only the input file the run read from should be present.
        assert written_names(tmp_path) == []
        assert list(tmp_path.glob("*_report.json")) == []
        assert "dry run: nothing written" in capsys.readouterr().out

    def test_single_format_can_be_selected(
        self, csv_with_one_email_less_row: Path, tmp_path: Path
    ) -> None:
        cli(
            [
                "--source",
                "csv",
                "--csv-path",
                str(csv_with_one_email_less_row),
                "--output-dir",
                str(tmp_path),
                "--format",
                "jsonl",
            ]
        )
        assert len(written_names(tmp_path)) == 1
        assert written_names(tmp_path)[0].endswith(".jsonl")
        assert len(list(tmp_path.glob("*_report.json"))) == 1

    def test_max_leads_caps_the_export(
        self, csv_with_one_email_less_row: Path, tmp_path: Path
    ) -> None:
        cli(
            [
                "--source",
                "csv",
                "--csv-path",
                str(csv_with_one_email_less_row),
                "--output-dir",
                str(tmp_path),
                "--format",
                "csv",
                "--max-leads",
                "1",
            ]
        )
        assert len(exported_leads(tmp_path)) == 1

    def test_filters_can_be_applied_from_the_command_line(
        self, csv_with_one_email_less_row: Path, tmp_path: Path
    ) -> None:
        cli(
            [
                "--source",
                "csv",
                "--csv-path",
                str(csv_with_one_email_less_row),
                "--output-dir",
                str(tmp_path),
                "--format",
                "csv",
                "--require-email",
            ]
        )
        assert {row["last_name"] for row in exported_leads(tmp_path)} == {"Lovelace", "Turing"}
        assert read_report(tmp_path)["rejections"]["by_rule"] == {"require_email": 1}

    def test_explicit_flag_overrides_the_environment(
        self,
        monkeypatch: pytest.MonkeyPatch,
        csv_with_one_email_less_row: Path,
        tmp_path: Path,
    ) -> None:
        # The whole reason --flag/--no-flag exist as a pair: an omitted flag must
        # not clobber the environment, but an explicit one must win over it.
        monkeypatch.setenv("LEAD_FILTERS__REQUIRE_EMAIL", "true")
        cli(
            [
                "--source",
                "csv",
                "--csv-path",
                str(csv_with_one_email_less_row),
                "--output-dir",
                str(tmp_path),
                "--format",
                "csv",
                "--no-require-email",
            ]
        )
        assert len(exported_leads(tmp_path)) == 3

    def test_environment_alone_is_honoured(
        self,
        monkeypatch: pytest.MonkeyPatch,
        csv_with_one_email_less_row: Path,
        tmp_path: Path,
    ) -> None:
        monkeypatch.setenv("LEAD_FILTERS__REQUIRE_EMAIL", "true")
        cli(
            [
                "--source",
                "csv",
                "--csv-path",
                str(csv_with_one_email_less_row),
                "--output-dir",
                str(tmp_path),
                "--format",
                "csv",
            ]
        )
        assert len(exported_leads(tmp_path)) == 2

    def test_sources_can_be_combined(
        self, csv_with_one_email_less_row: Path, tmp_path: Path
    ) -> None:
        cli(
            [
                "-s",
                "csv",
                "-s",
                "mock",
                "--csv-path",
                str(csv_with_one_email_less_row),
                "--limit",
                "5",
                "--output-dir",
                str(tmp_path),
                "--format",
                "csv",
            ]
        )
        assert set(read_report(tmp_path)["sources"]["collected"]) == {"csv", "mock"}

    def test_comma_separated_sources_are_split(
        self, csv_with_one_email_less_row: Path, tmp_path: Path
    ) -> None:
        cli(
            [
                "--source",
                "mock,csv",
                "--csv-path",
                str(csv_with_one_email_less_row),
                "--limit",
                "3",
                "--output-dir",
                str(tmp_path),
            ]
        )
        assert set(read_report(tmp_path)["sources"]["collected"]) == {"csv", "mock"}

    def test_run_report_can_be_switched_off(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("LEAD_WRITE_RUN_REPORT", "false")
        cli(["--source", "mock", "--limit", "5", "--output-dir", str(tmp_path), "--format", "csv"])
        assert list(tmp_path.glob("*_report.json")) == []


class TestOutputTarget:
    """``--output`` names the exact file, unlike ``--output-dir``."""

    def test_output_writes_that_exact_path(self, tmp_path: Path) -> None:
        target = tmp_path / "leads.json"
        code = cli(
            [
                "--source",
                "mock",
                "--limit",
                "5",
                "--format",
                "json",
                "--output",
                str(target),
            ]
        )
        assert code == EXIT_OK
        assert target.is_file()
        # The JSON exporter writes a bare array of leads.
        assert len(json.loads(target.read_text(encoding="utf-8"))) == 4

    def test_short_form_is_accepted(self, tmp_path: Path) -> None:
        target = tmp_path / "short.csv"
        cli(["--source", "mock", "--limit", "5", "--format", "csv", "-o", str(target)])
        assert target.is_file()

    def test_no_timestamped_file_is_written(self, tmp_path: Path) -> None:
        # A caller that named a file gets that file and nothing else.
        cli(
            [
                "--source",
                "mock",
                "--limit",
                "5",
                "--format",
                "csv",
                "--output",
                str(tmp_path / "plain.csv"),
            ]
        )
        assert written_names(tmp_path) == []

    def test_missing_parent_directories_are_created(self, tmp_path: Path) -> None:
        target = tmp_path / "deep" / "nested" / "leads.csv"
        code = cli(
            [
                "--source",
                "mock",
                "--limit",
                "5",
                "--format",
                "csv",
                "--output",
                str(target),
            ]
        )
        assert code == EXIT_OK
        assert target.is_file()

    def test_run_report_sits_beside_the_output(self, tmp_path: Path) -> None:
        target = tmp_path / "leads.json"
        cli(["--source", "mock", "--limit", "5", "--format", "json", "--output", str(target)])

        report = json.loads((tmp_path / "leads_report.json").read_text(encoding="utf-8"))
        assert report["stats"]["raw_collected"] == 5
        assert report["outputs"] == [str(target)]

    def test_two_formats_into_one_file_is_a_config_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = cli(
            [
                "--source",
                "mock",
                "--format",
                "csv,json",
                "--output",
                str(tmp_path / "leads.out"),
            ]
        )
        assert code == EXIT_CONFIG
        assert "--output-dir" in capsys.readouterr().err

        # The check runs before any crawling, so a doomed run costs nothing.
        assert list(tmp_path.iterdir()) == []

    def test_output_and_output_dir_are_mutually_exclusive(self, tmp_path: Path) -> None:
        with pytest.raises(SystemExit) as excinfo:
            cli(["--source", "mock", "--output", "a.json", "--output-dir", str(tmp_path)])
        assert excinfo.value.code == EXIT_CONFIG

    def test_dry_run_ignores_the_output_target(self, tmp_path: Path) -> None:
        target = tmp_path / "never.csv"
        code = cli(
            [
                "--source",
                "mock",
                "--limit",
                "5",
                "--format",
                "csv",
                "--output",
                str(target),
                "--dry-run",
            ]
        )
        assert code == EXIT_OK
        assert not target.exists()


class TestVerbosity:
    def test_verbose_and_log_level_are_mutually_exclusive(self) -> None:
        with pytest.raises(SystemExit) as excinfo:
            cli(["--source", "mock", "--verbose", "--log-level", "INFO"])
        assert excinfo.value.code == EXIT_CONFIG

    def test_verbose_raises_the_root_logger_to_debug(self, tmp_path: Path) -> None:
        cli(["--source", "mock", "--limit", "5", "--output-dir", str(tmp_path), "--verbose"])
        assert logging.getLogger().level == logging.DEBUG

    def test_default_run_stays_at_info(self, tmp_path: Path) -> None:
        cli(["--source", "mock", "--limit", "5", "--output-dir", str(tmp_path)])
        assert logging.getLogger().level == logging.INFO

    def test_debug_records_explain_why_leads_were_dropped(
        self,
        csv_with_one_email_less_row: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # --verbose is only useful if it actually adds detail. Logs go to stderr.
        cli(
            [
                "--source",
                "csv",
                "--csv-path",
                str(csv_with_one_email_less_row),
                "--output-dir",
                str(tmp_path),
                "--require-email",
                "--verbose",
            ]
        )
        assert "lead rejected" in capsys.readouterr().err

    def test_verbose_dumps_the_effective_configuration(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cli(["--source", "mock", "--limit", "5", "--output-dir", str(tmp_path), "--verbose"])
        err = capsys.readouterr().err
        assert "effective configuration" in err
        assert "max_concurrency" in err

    def test_verbose_never_logs_a_credential(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        secret = "super-secret-apollo-key"  # noqa: S105 - a fixture value, not a credential
        monkeypatch.setenv("LEAD_APOLLO__API_KEY", secret)
        cli(["--source", "mock", "--limit", "5", "--output-dir", str(tmp_path), "--verbose"])
        assert secret not in capsys.readouterr().err


class TestExitCodes:
    def test_empty_result_is_success_by_default(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty.csv"
        empty.write_text("Email,Company\n", encoding="utf-8")
        assert cli(["--source", "csv", "--csv-path", str(empty), "--dry-run"]) == EXIT_OK

    def test_fail_on_empty_returns_its_own_code(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        empty = tmp_path / "empty.csv"
        empty.write_text("Email,Company\n", encoding="utf-8")
        code = cli(
            [
                "--source",
                "csv",
                "--csv-path",
                str(empty),
                "--output-dir",
                str(tmp_path),
                "--fail-on-empty",
            ]
        )
        assert code == EXIT_EMPTY

    def test_every_source_failing_has_its_own_code(
        self, exploding_source: str, tmp_path: Path
    ) -> None:
        # A healthy run with no matches and a run where everything broke are
        # different situations for whoever reads the exit code.
        code = cli(["--source", exploding_source, "--dry-run", "--output-dir", str(tmp_path)])
        assert code == EXIT_ALL_SOURCES_FAILED

    def test_unexpected_failure_returns_one(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from src.processors import Pipeline

        class BrokenPipeline(Pipeline):
            async def run(self, *args: Any, **kwargs: Any) -> Any:
                raise RuntimeError("something went wrong deep inside")

        monkeypatch.setattr("src.processors.Pipeline", BrokenPipeline)
        assert cli(["--source", "mock", "--dry-run"]) == EXIT_ERROR

    def test_interrupt_returns_130(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from src.processors import Pipeline

        class InterruptedPipeline(Pipeline):
            async def run(self, *args: Any, **kwargs: Any) -> Any:
                raise KeyboardInterrupt

        monkeypatch.setattr("src.processors.Pipeline", InterruptedPipeline)
        assert cli(["--source", "mock", "--dry-run"]) == 130
