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
from typing import TYPE_CHECKING, Any, cast

import pytest

from src.config import FilterSettings
from src.crawlers import registry as registry_module
from src.crawlers.base import BaseCrawler
from src.crawlers.registry import register_crawler
from src.filter_profiles import DEFAULT_PROFILE_NAME
from src.main import (
    EXIT_ALL_SOURCES_FAILED,
    EXIT_CONFIG,
    EXIT_EMPTY,
    EXIT_ERROR,
    EXIT_OK,
    build_parser,
    build_settings,
    cli,
)
from src.models.lead import RawLead
from src.utils.errors import CrawlerError
from src.utils.logging import configure_logging

if TYPE_CHECKING:
    from src.config import Settings


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


class TestSourceFlags:
    """Per-source flags resolve into settings without a crawl being run."""

    @staticmethod
    def settings_for(argv: list[str]) -> Any:
        parser = build_parser()
        return build_settings(parser.parse_args(argv))

    def test_website_urls_accept_repeats_and_commas(self) -> None:
        settings = self.settings_for(
            ["--website-url", "acme.com", "--website-url", "beta.com,gamma.com"]
        )
        assert settings.website.urls == ["acme.com", "beta.com", "gamma.com"]

    def test_website_urls_fall_back_to_the_environment(self) -> None:
        # An omitted flag must not clobber LEAD_WEBSITE__URLS with an empty list.
        settings = self.settings_for([])
        assert settings.website.urls == []

    def test_bare_search_profile_means_default(self) -> None:
        assert self.settings_for(["--search-profile"]).search_profile == "default"

    def test_search_profile_takes_a_name(self) -> None:
        assert self.settings_for(["--search-profile", "sea"]).search_profile == "sea"

    def test_no_search_profile_leaves_it_unset(self) -> None:
        assert self.settings_for([]).search_profile is None

    def test_an_unknown_profile_is_a_config_error(self, capsys: pytest.CaptureFixture[str]) -> None:
        code = cli(
            [
                "--source",
                "apollo",
                "--search-profile",
                "nope",
                "--search-profiles-path",
                str(Path(__file__).resolve().parents[1] / "config" / "search_profiles.yaml"),
                "--dry-run",
            ]
        )
        assert code == EXIT_CONFIG
        assert "unknown search profile" in capsys.readouterr().err

    def test_the_website_source_is_unusable_without_urls(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert cli(["--source", "website"]) == EXIT_CONFIG
        assert "--website-url" in capsys.readouterr().err


class TestFilterProfiles:
    """The named-profile layer: inert until named, then a complete rule set."""

    #: The example shipped in the repository, by absolute path — the isolation
    #: fixture chdirs into a tmp_path, so the relative default would not resolve.
    SHIPPED = Path(__file__).resolve().parents[1] / "config" / "filters.yaml"

    @staticmethod
    def settings_for(argv: list[str]) -> Settings:
        return build_settings(build_parser().parse_args(argv))

    @staticmethod
    def write_profile(tmp_path: Path, body: str) -> Path:
        path = tmp_path / "filters.yaml"
        path.write_text(body, encoding="utf-8")
        return path

    def test_bare_filter_profile_means_default(self) -> None:
        settings = self.settings_for(
            ["--filter-profile", "--filter-profiles-path", str(self.SHIPPED)]
        )
        assert settings.filter_profile == DEFAULT_PROFILE_NAME
        # Naming a profile resolves it eagerly, so a typo fails before a crawl.
        assert settings.filters.include_titles == [
            "CTO",
            "VP Engineering",
            "Head of Engineering",
            "Founder",
        ]

    def test_filter_profile_takes_a_name(self) -> None:
        settings = self.settings_for(
            ["--filter-profile", "enterprise_na", "--filter-profiles-path", str(self.SHIPPED)]
        )
        assert settings.filter_profile == "enterprise_na"
        assert settings.filters.include_countries == ["US", "CA"]

    def test_no_filter_profile_leaves_it_unset(self) -> None:
        assert self.settings_for([]).filter_profile is None

    def test_no_profile_reads_no_file(self) -> None:
        # Inert by default: a path pointing nowhere must not break a run that
        # never asked for a profile. This is what makes the feature safe to ship.
        settings = self.settings_for(["--filter-profiles-path", "does/not/exist.yaml"])
        assert settings.filter_profiles_path == Path("does/not/exist.yaml")
        assert settings.filters == FilterSettings()

    def test_no_profile_keeps_the_environment_rules(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LEAD_FILTERS__MIN_EMPLOYEES", "42")
        assert self.settings_for([]).filters.min_employees == 42

    def test_a_profile_replaces_the_environment_rules(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # A profile is a complete statement of which leads we keep, so it must
        # mean the same thing on every machine rather than merging with whatever
        # happened to be exported in the shell.
        monkeypatch.setenv("LEAD_FILTERS__MIN_EMPLOYEES", "42")
        path = self.write_profile(tmp_path, "default:\n  minimum_employee_count: 10\n")
        settings = self.settings_for(["--filter-profile", "--filter-profiles-path", str(path)])
        assert settings.filters.min_employees == 10

    def test_a_flag_overrides_the_profile(self, tmp_path: Path) -> None:
        path = self.write_profile(
            tmp_path,
            "default:\n  minimum_employee_count: 10\n  maximum_employee_count: 1000\n",
        )
        settings = self.settings_for(
            ["--filter-profile", "--filter-profiles-path", str(path), "--min-employees", "5"]
        )
        assert settings.filters.min_employees == 5
        # Overriding one rule must not discard the rest of the profile.
        assert settings.filters.max_employees == 1000

    def test_an_unknown_profile_is_a_config_error(self, capsys: pytest.CaptureFixture[str]) -> None:
        code = cli(
            [
                "--source",
                "mock",
                "--filter-profile",
                "nope",
                "--filter-profiles-path",
                str(self.SHIPPED),
                "--dry-run",
            ]
        )
        assert code == EXIT_CONFIG
        assert "unknown filter profile" in capsys.readouterr().err

    def test_a_broken_profile_is_a_config_error(self, tmp_path: Path) -> None:
        path = self.write_profile(tmp_path, "default:\n  required_fields: [company.nmae]\n")
        assert (
            cli(["--source", "mock", "--filter-profile", "--filter-profiles-path", str(path)])
            == EXIT_CONFIG
        )

    def test_list_filter_profiles(self, capsys: pytest.CaptureFixture[str]) -> None:
        shipped = Path(__file__).resolve().parents[1] / "config" / "filters.yaml"
        assert cli(["--list-filter-profiles", "--filter-profiles-path", str(shipped)]) == EXIT_OK
        out = capsys.readouterr().out
        assert DEFAULT_PROFILE_NAME in out
        assert "enterprise_na" in out

    def test_listing_uses_the_environment_path(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The listing must name the file a run would actually read, not a default
        # the user has already overridden.
        path = self.write_profile(tmp_path, "custom:\n  allowed_titles: [CTO]\n")
        monkeypatch.setenv("LEAD_FILTER_PROFILES_PATH", str(path))
        assert cli(["--list-filter-profiles"]) == EXIT_OK
        assert "custom" in capsys.readouterr().out


class TestProfileDrivenRun:
    """A profile must reach the pipeline, not just the settings object."""

    def test_a_profile_written_to_disk_filters_the_run(
        self, csv_with_one_email_less_row: Path, tmp_path: Path
    ) -> None:
        path = tmp_path / "filters.yaml"
        path.write_text("default:\n  required_fields: [person.email]\n", encoding="utf-8")
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
                "--filter-profile",
                "--filter-profiles-path",
                str(path),
            ]
        )
        assert code == EXIT_OK
        assert len(exported_leads(tmp_path)) == 2
        # Named per path, so a multi-rule profile says which requirement bit.
        assert read_report(tmp_path)["rejections"]["by_rule"] == {"require_person_email": 1}


class TestUmbrellaProfile:
    """``--profile NAME``: one name, both halves — search and qualification."""

    #: The files shipped in the repository, by absolute path: the isolation
    #: fixture chdirs into a tmp_path, so the relative defaults would not resolve.
    SEARCH = Path(__file__).resolve().parents[1] / "config" / "search_profiles.yaml"
    FILTERS = Path(__file__).resolve().parents[1] / "config" / "filters.yaml"

    @classmethod
    def paths(cls) -> list[str]:
        return [
            "--search-profiles-path",
            str(cls.SEARCH),
            "--filter-profiles-path",
            str(cls.FILTERS),
        ]

    @staticmethod
    def settings_for(argv: list[str]) -> Settings:
        return build_settings(build_parser().parse_args(argv))

    def test_one_name_fills_both_halves(self) -> None:
        settings = self.settings_for(["--profile", "singapore_tech", *self.paths()])
        assert settings.profile == "singapore_tech"
        assert settings.search_profile == "singapore_tech"
        assert settings.filter_profile == "singapore_tech"
        assert settings.filters.is_active is True

    def test_the_hyphenated_spelling_works(self) -> None:
        # How the name is most likely to be typed, and what the prompt example
        # uses — the shipped profile is named with an underscore for consistency.
        settings = self.settings_for(["--profile", "singapore-tech", *self.paths()])
        assert settings.search_profile == "singapore_tech"

    def test_a_search_only_name_leaves_the_filter_half_alone(self) -> None:
        # A strategy that only narrows the search is still a strategy; the half it
        # does not define must not be emptied or invented.
        settings = self.settings_for(["--profile", "sea_fintech", *self.paths()])
        assert settings.search_profile == "sea_fintech"
        assert settings.filter_profile is None
        assert settings.filters == FilterSettings()

    def test_a_filter_only_name_leaves_the_search_half_alone(self) -> None:
        settings = self.settings_for(["--profile", "enterprise_na", *self.paths()])
        assert settings.filter_profile == "enterprise_na"
        assert settings.search_profile is None

    def test_a_missing_half_keeps_the_environment_value(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The umbrella said nothing about search, so whatever else selected that
        # half still stands — it must not be cleared just because the umbrella
        # did not cover it.
        monkeypatch.setenv("LEAD_SEARCH_PROFILE", "sea_fintech")
        settings = self.settings_for(["--profile", "enterprise_na", *self.paths()])
        assert settings.search_profile == "sea_fintech"
        assert settings.filter_profile == "enterprise_na"

    def test_the_umbrella_beats_the_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A flag on the command line is the more specific request.
        monkeypatch.setenv("LEAD_FILTER_PROFILE", "enterprise_na")
        monkeypatch.setenv("LEAD_SEARCH_PROFILE", "sea_fintech")
        settings = self.settings_for(["--profile", "singapore_tech", *self.paths()])
        assert settings.filter_profile == "singapore_tech"
        assert settings.search_profile == "singapore_tech"

    def test_an_explicit_half_beats_the_umbrella(self) -> None:
        # A one-off run can keep a strategy's search and swap its qualification
        # rules without editing a file.
        settings = self.settings_for(
            ["--profile", "singapore_tech", "--filter-profile", "enterprise_na", *self.paths()]
        )
        assert settings.search_profile == "singapore_tech"
        assert settings.filter_profile == "enterprise_na"
        assert settings.filters.include_countries == ["US", "CA"]

    def test_the_umbrella_comes_from_the_environment_too(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LEAD_PROFILE", "singapore_tech")
        settings = self.settings_for(self.paths())
        assert settings.search_profile == "singapore_tech"

    def test_no_profile_reads_neither_file(self) -> None:
        # Still inert by default: paths pointing nowhere must not break a run
        # that never named a profile.
        settings = self.settings_for(
            ["--search-profiles-path", "gone.yaml", "--filter-profiles-path", "gone.yaml"]
        )
        assert settings.search_profile is None and settings.filter_profile is None

    def test_an_unknown_name_is_a_config_error_before_any_crawl(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = cli(["--source", "mock", "--profile", "nope", *self.paths(), "--dry-run"])
        assert code == EXIT_CONFIG
        err = capsys.readouterr().err
        assert "unknown profile" in err
        # The alternatives, because a typo is the usual cause and neither file is
        # open in front of whoever is reading the error.
        assert "singapore_tech" in err
        assert "sea_fintech" in err

    def test_a_broken_filter_half_is_a_config_error(self, tmp_path: Path) -> None:
        # Refused whole rather than half-applied: a run that quietly qualified
        # against no rules would look exactly like one that worked.
        filters = tmp_path / "filters.yaml"
        filters.write_text("broken:\n  required_fields: [company.nmae]\n", encoding="utf-8")
        search = tmp_path / "search.yaml"
        search.write_text("broken:\n  titles: [CTO]\n", encoding="utf-8")
        code = cli(
            [
                "--source",
                "mock",
                "--profile",
                "broken",
                "--search-profiles-path",
                str(search),
                "--filter-profiles-path",
                str(filters),
                "--dry-run",
            ]
        )
        assert code == EXIT_CONFIG

    def test_the_listing_marks_which_halves_each_name_covers(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert cli(["--list-profiles", *self.paths()]) == EXIT_OK
        out = capsys.readouterr().out
        # The whole point of the listing: seeing at a glance that `sea_fintech`
        # is search-only and `enterprise_na` filter-only.
        assert "singapore_tech" in out and "search+filters" in out
        assert "sea_fintech" in out
        assert "enterprise_na" in out

    def test_the_listing_uses_the_configured_paths(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        search = tmp_path / "search.yaml"
        search.write_text("custom:\n  titles: [CTO]\n", encoding="utf-8")
        filters = tmp_path / "filters.yaml"
        filters.write_text("custom:\n  allowed_titles: [CTO]\n", encoding="utf-8")
        monkeypatch.setenv("LEAD_SEARCH_PROFILES_PATH", str(search))
        monkeypatch.setenv("LEAD_FILTER_PROFILES_PATH", str(filters))
        assert cli(["--list-profiles"]) == EXIT_OK
        assert "custom" in capsys.readouterr().out

    def test_the_umbrella_drives_the_run(
        self, csv_with_one_email_less_row: Path, tmp_path: Path
    ) -> None:
        # The settings object is not the deliverable; the filtered run is.
        search = tmp_path / "search.yaml"
        search.write_text("strict:\n  titles: [CTO]\n", encoding="utf-8")
        filters = tmp_path / "filters.yaml"
        filters.write_text("strict:\n  required_fields: [person.email]\n", encoding="utf-8")
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
                "--profile",
                "strict",
                "--search-profiles-path",
                str(search),
                "--filter-profiles-path",
                str(filters),
            ]
        )
        assert code == EXIT_OK
        assert len(exported_leads(tmp_path)) == 2
        assert read_report(tmp_path)["rejections"]["by_rule"] == {"require_person_email": 1}


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
        assert {row["person_full_name"] for row in rows} == {
            "Ada Lovelace",
            "Grace Hopper",
            "Alan Turing",
        }
        # Values are normalized on the way through, not merely copied.
        assert all(row["lead_id"] for row in rows)
        assert (
            next(row for row in rows if row["person_last_name"] == "Lovelace")["person_job_title"]
            == "CTO"
        )

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
        assert {row["person_last_name"] for row in exported_leads(tmp_path)} == {
            "Lovelace",
            "Turing",
        }
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


class TestOutputPreflight:
    """An output request that cannot succeed fails before the crawl, not after it.

    The crawl is the expensive half of a run and, for a metered source, the half
    that costs money. Discovering afterwards that there was nowhere to write
    means paying for results that are then thrown away.
    """

    def test_an_unwritable_directory_is_a_config_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        blocked = tmp_path / "locked"

        def refuse(self: Path, *args: object, **kwargs: object) -> None:
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr(Path, "mkdir", refuse)
        code = cli(
            [
                "--source",
                "mock",
                "--limit",
                "5",
                "--format",
                "csv",
                "--output",
                str(blocked / "leads.csv"),
            ]
        )

        assert code == EXIT_CONFIG
        err = capsys.readouterr().err
        assert "cannot create output directory" in err
        assert str(blocked) in err

    def test_the_check_runs_before_any_crawling(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        def refuse(self: Path, *args: object, **kwargs: object) -> None:
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr(Path, "mkdir", refuse)
        cli(
            [
                "--source",
                "mock",
                "--limit",
                "5",
                "--format",
                "csv",
                "--output",
                str(tmp_path / "x" / "leads.csv"),
            ]
        )

        # The pipeline logs this as it starts each source; its absence is what
        # proves the run never began.
        assert "crawling source" not in capsys.readouterr().err

    def test_a_dry_run_creates_no_directory(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # A dry run that made directories would not be dry.
        target = tmp_path / "never" / "created" / "leads.csv"
        code = cli(
            [
                "--source",
                "mock",
                "--limit",
                "5",
                "--format",
                "csv",
                "--dry-run",
                "--output",
                str(target),
            ]
        )
        assert code == EXIT_OK
        assert not target.parent.exists()

    def test_a_parent_that_is_a_file_is_reported(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # `--output some_file.txt/leads.csv` is a typo, and the error should say
        # which path was the problem rather than surfacing a bare errno.
        blocker = tmp_path / "blocker"
        blocker.write_text("not a directory", encoding="utf-8")
        code = cli(["--source", "mock", "--format", "csv", "--output", str(blocker / "leads.csv")])

        assert code == EXIT_CONFIG
        assert "cannot create output directory" in capsys.readouterr().err

    def test_a_path_that_is_not_a_directory_is_reported(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Defensive branch: mkdir reported success but the path is not usable as
        # a directory. Exercised directly because the OSError path above would
        # otherwise hide it.
        def pretend_success(self: Path, *args: object, **kwargs: object) -> None:
            return None

        monkeypatch.setattr(Path, "mkdir", pretend_success)
        code = cli(
            ["--source", "mock", "--format", "csv", "--output", str(tmp_path / "sub" / "leads.csv")]
        )
        assert code == EXIT_CONFIG
        assert "is not a directory" in capsys.readouterr().err


class TestExportResilience:
    """One format that cannot be written must not cost the others, or the report."""

    @pytest.fixture
    def broken_csv_exporter(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Replace the CSV writer with one that always fails.

        Assigned into a copied registry rather than registered through
        :func:`register_exporter`, which (correctly) refuses to let two classes
        claim one format — overwriting the real writer here is exactly the
        intent.
        """
        from src.exporters import base as exporter_base
        from src.models.enums import ExportFormat
        from src.utils.errors import ExportError as ExportFailure

        class BrokenCsvExporter(exporter_base.BaseExporter):
            format = ExportFormat.CSV
            extension = ".csv"
            description = "Always fails; used to exercise the failure path."

            async def export(self, leads: Any, path: Path) -> Any:
                raise ExportFailure(f"could not write CSV to {path}")

        monkeypatch.setattr(
            exporter_base,
            "_REGISTRY",
            {**exporter_base._REGISTRY, ExportFormat.CSV: BrokenCsvExporter},
        )

    def test_the_other_format_is_still_written(
        self,
        broken_csv_exporter: None,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        code = cli(
            [
                "--source",
                "mock",
                "--limit",
                "5",
                "--format",
                "csv,json",
                "--output-dir",
                str(tmp_path),
            ]
        )

        assert code == EXIT_ERROR
        err = capsys.readouterr().err
        assert "export failed" in err
        assert "csv" in err
        # The JSON export succeeded, so it must be on disk.
        assert [Path(name).suffix for name in written_names(tmp_path)] == [".json"]

    def test_the_run_report_survives_an_export_failure(
        self,
        broken_csv_exporter: None,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # The report is what explains the crawl; losing it over one unwritable
        # file would leave the failure undiagnosable.
        cli(
            [
                "--source",
                "mock",
                "--limit",
                "5",
                "--format",
                "csv,json",
                "--output-dir",
                str(tmp_path),
            ]
        )

        report = read_report(tmp_path)
        assert report["stats"]["raw_collected"] == 5

    def test_a_clean_run_is_unaffected(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The containment must not turn every run into a partial failure.
        code = cli(
            [
                "--source",
                "mock",
                "--limit",
                "5",
                "--format",
                "csv,json",
                "--output-dir",
                str(tmp_path),
            ]
        )
        assert code == EXIT_OK
        assert "export failed" not in capsys.readouterr().err
        assert len(written_names(tmp_path)) == 2


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
