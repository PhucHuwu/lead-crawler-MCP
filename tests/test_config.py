"""Tests for environment-driven configuration."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.config import FilterSettings, Settings, load_settings, misnamed_env_vars
from src.models.enums import DedupStrategy, ExportFormat, LogFormat, SeniorityLevel
from src.utils.errors import ConfigError


class TestDefaults:
    def test_runs_with_no_configuration_at_all(self) -> None:
        settings = load_settings()
        assert settings.default_sources == ["mock"]
        assert settings.log_format is LogFormat.CONSOLE
        assert settings.dedup_strategy is DedupStrategy.IDENTITY

    def test_filters_start_inactive(self) -> None:
        # Filtering must be opt-in: a default run should not silently drop leads.
        assert load_settings().filters.is_active is False

    def test_output_formats_default_to_csv_and_json(self) -> None:
        assert load_settings().active_formats() == [ExportFormat.CSV, ExportFormat.JSON]


class TestEnvironmentParsing:
    def test_plain_scalar(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LEAD_LOG_LEVEL", "DEBUG")
        monkeypatch.setenv("LEAD_DEFAULT_LIMIT", "250")
        settings = load_settings()
        assert settings.log_level == "DEBUG"
        assert settings.default_limit == 250

    def test_comma_separated_list(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LEAD_FILTERS__EXCLUDE_COUNTRIES", "IN, CN ,RU")
        assert load_settings().filters.exclude_countries == ["IN", "CN", "RU"]

    def test_json_array_list(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LEAD_FILTERS__INCLUDE_COUNTRIES", '["US","CA"]')
        assert load_settings().filters.include_countries == ["US", "CA"]

    def test_empty_list(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LEAD_FILTERS__EXCLUDE_COUNTRIES", "")
        assert load_settings().filters.exclude_countries == []

    def test_enum_list(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LEAD_OUTPUT_FORMATS", "csv,jsonl")
        assert load_settings().active_formats() == [ExportFormat.CSV, ExportFormat.JSONL]

    def test_enum_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LEAD_DEDUP_STRATEGY", "aggressive")
        assert load_settings().dedup_strategy is DedupStrategy.AGGRESSIVE

    def test_seniority_list(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LEAD_FILTERS__INCLUDE_SENIORITY", "c_suite,vp")
        assert load_settings().filters.include_seniority == [
            SeniorityLevel.C_SUITE,
            SeniorityLevel.VP,
        ]

    def test_nested_credentials_are_masked(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LEAD_APOLLO__API_KEY", "super-secret")
        settings = load_settings()
        assert settings.apollo.is_configured is True
        # The secret must never appear in a repr, a log line or a report.
        assert "super-secret" not in repr(settings)
        assert settings.apollo.api_key is not None
        assert settings.apollo.api_key.get_secret_value() == "super-secret"

    def test_unknown_variables_are_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LEAD_SOMETHING_UNKNOWN", "1")
        assert load_settings().log_level == "INFO"

    def test_active_formats_deduplicates(self) -> None:
        settings = load_settings(output_formats=["csv", "csv", "json"])
        assert settings.active_formats() == [ExportFormat.CSV, ExportFormat.JSON]


class TestOverrides:
    def test_override_beats_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LEAD_LOG_LEVEL", "DEBUG")
        assert load_settings(log_level="ERROR").log_level == "ERROR"

    def test_none_override_is_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # An unset CLI flag must not clobber an environment value.
        monkeypatch.setenv("LEAD_LOG_LEVEL", "DEBUG")
        assert load_settings(log_level=None).log_level == "DEBUG"

    def test_nested_override_preserves_other_env_fields(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LEAD_FILTERS__MIN_EMPLOYEES", "50")
        settings = load_settings(filters={"exclude_countries": ["IN"]})
        assert settings.filters.exclude_countries == ["IN"]
        assert settings.filters.min_employees == 50


class TestValidation:
    def test_contradictory_size_bounds_are_rejected(self) -> None:
        with pytest.raises(ConfigError, match="valid configuration"):
            load_settings(filters={"min_employees": 100, "max_employees": 10})

    def test_unknown_enum_is_rejected(self) -> None:
        with pytest.raises(ConfigError):
            load_settings(dedup_strategy="telepathic")

    def test_malformed_json_list_is_rejected(self) -> None:
        with pytest.raises(ConfigError):
            load_settings(filters={"exclude_countries": '["unclosed'})

    def test_out_of_range_completeness_is_rejected(self) -> None:
        with pytest.raises(ConfigError):
            load_settings(min_completeness=1.5)

    def test_negative_limit_is_rejected(self) -> None:
        with pytest.raises(ConfigError):
            load_settings(default_limit=0)


class TestOutputDirectory:
    def test_creates_missing_directory(self, tmp_path: Path) -> None:
        target = tmp_path / "nested" / "exports"
        settings = load_settings(output_dir=target)
        assert settings.ensure_output_dir() == target
        assert target.is_dir()

    def test_is_idempotent(self, tmp_path: Path) -> None:
        settings = load_settings(output_dir=tmp_path / "out")
        settings.ensure_output_dir()
        assert settings.ensure_output_dir().is_dir()

    def test_file_in_the_way_is_a_config_error(self, tmp_path: Path) -> None:
        blocker = tmp_path / "not-a-dir"
        blocker.write_text("x")
        with pytest.raises(ConfigError, match="cannot create output directory"):
            load_settings(output_dir=blocker).ensure_output_dir()


class TestEnvFile:
    def test_reads_from_an_explicit_dotenv_file(self, tmp_path: Path) -> None:
        env_file = tmp_path / "custom.env"
        env_file.write_text("LEAD_LOG_LEVEL=WARNING\nLEAD_DEFAULT_LIMIT=42\n")
        settings = load_settings(env_file=env_file)
        assert settings.log_level == "WARNING"
        assert settings.default_limit == 42


class TestMisnamedEnvironmentVariables:
    """A setting that is silently not read is worse than one that is missing.

    ``env_prefix="LEAD_"`` means a bare ``APOLLO_API_KEY`` is accepted and then
    ignored, so the run proceeds unauthenticated — and for a credential the
    difference between "ignored" and "absent" is invisible until it costs
    something. These tests pin the loud refusal.
    """

    def test_a_bare_credential_stops_the_run(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("APOLLO_API_KEY", "sk-live-xxx")
        with pytest.raises(ConfigError) as excinfo:
            load_settings()
        message = str(excinfo.value)
        # Both halves matter: what is wrong, and what to write instead.
        assert "APOLLO_API_KEY" in message
        assert "LEAD_APOLLO__API_KEY" in message

    def test_a_bare_tuning_value_stops_the_run(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LOG_LEVEL", "DEBUG")
        with pytest.raises(ConfigError, match="LEAD_LOG_LEVEL"):
            load_settings()

    def test_the_single_underscore_nested_form_is_caught(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The likeliest typo of all: one underscore instead of the two that mark
        # a nested field.
        monkeypatch.setenv("LEAD_FILTERS_MIN_EMPLOYEES", "50")
        with pytest.raises(ConfigError, match="LEAD_FILTERS__MIN_EMPLOYEES"):
            load_settings()

    def test_every_offender_is_named_at_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Learning them one per run is a miserable way to be told.
        monkeypatch.setenv("LOG_LEVEL", "DEBUG")
        monkeypatch.setenv("APOLLO_API_KEY", "sk-live-xxx")
        with pytest.raises(ConfigError) as excinfo:
            load_settings()
        message = str(excinfo.value)
        assert "LOG_LEVEL" in message
        assert "APOLLO_API_KEY" in message

    def test_the_canonical_name_alone_is_read_normally(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LEAD_APOLLO__API_KEY", "sk-live-xxx")
        assert load_settings().apollo.is_configured is True

    def test_the_wrong_name_is_tolerated_when_the_right_one_is_also_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The tool is reading the right variable, so a stray one is somebody
        # else's business — failing here would break runs that work correctly.
        monkeypatch.setenv("APOLLO_API_KEY", "sk-live-wrong")
        monkeypatch.setenv("LEAD_APOLLO__API_KEY", "sk-live-right")
        assert load_settings().apollo.is_configured is True

    def test_a_name_belonging_to_another_tool_is_left_alone(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The guard has to be worth its false positives. `AWS_PROFILE` and
        # friends are common in a shell and are not our business.
        monkeypatch.setenv("AWS_PROFILE", "prod")
        monkeypatch.setenv("HTTP_PROXY", "http://proxy.internal:3128")
        assert load_settings().log_level == "INFO"

    def test_a_misnamed_variable_in_the_dotenv_file_is_caught(self, tmp_path: Path) -> None:
        # The .env file is scanned too, because that is where a credential is
        # most likely to have been copied in from a provider's docs.
        env_file = tmp_path / "custom.env"
        env_file.write_text("APOLLO_API_KEY=sk-live-xxx\n")
        with pytest.raises(ConfigError, match="LEAD_APOLLO__API_KEY"):
            load_settings(env_file=env_file)

    def test_the_message_says_the_offender_came_from_the_dotenv_file(self, tmp_path: Path) -> None:
        env_file = tmp_path / "custom.env"
        env_file.write_text("APOLLO_API_KEY=sk-live-xxx\n")
        with pytest.raises(ConfigError, match="custom.env"):
            load_settings(env_file=env_file)

    def test_a_canonical_dotenv_value_excuses_the_bare_environment_one(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("APOLLO_API_KEY", "sk-live-wrong")
        env_file = tmp_path / "custom.env"
        env_file.write_text("LEAD_APOLLO__API_KEY=sk-live-right\n")
        assert load_settings(env_file=env_file).apollo.is_configured is True

    def test_the_umbrella_profile_name_is_covered(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PROFILE", "singapore_tech")
        assert load_settings().profile is None  # not read...
        monkeypatch.setenv("LEAD_PROFILE", "singapore_tech")
        assert load_settings().profile == "singapore_tech"  # ...but this is

    def test_the_guard_table_is_derived_from_the_settings_models(self) -> None:
        # Deriving it is what keeps a newly added nested setting covered; an
        # empty or hand-written-only table would rot without anything noticing.
        names = misnamed_env_vars()
        assert names["LEAD_APOLLO_API_KEY"] == "LEAD_APOLLO__API_KEY"
        assert names["LEAD_FILTERS_MIN_EMPLOYEES"] == "LEAD_FILTERS__MIN_EMPLOYEES"
        assert names["APOLLO_API_KEY"] == "LEAD_APOLLO__API_KEY"


class TestProfileSelection:
    def test_no_profile_is_selected_by_default(self) -> None:
        # The whole profile layer must be inert until asked for: with no name,
        # neither file is even opened.
        settings = load_settings()
        assert settings.profile is None
        assert settings.search_profile is None
        assert settings.filter_profile is None

    def test_the_umbrella_profile_comes_from_the_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LEAD_PROFILE", "singapore_tech")
        assert load_settings().profile == "singapore_tech"

    def test_the_profile_paths_have_shipped_defaults(self) -> None:
        settings = load_settings()
        assert settings.search_profiles_path == Path("config/search_profiles.yaml")
        assert settings.filter_profiles_path == Path("config/filters.yaml")

    def test_the_paths_are_configurable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LEAD_FILTER_PROFILES_PATH", "/etc/leads/filters.yaml")
        assert load_settings().filter_profiles_path == Path("/etc/leads/filters.yaml")


class TestFilterSettings:
    def test_is_active_detects_any_rule(self) -> None:
        assert FilterSettings().is_active is False
        assert FilterSettings(require_email=True).is_active is True
        assert FilterSettings(min_employees=10).is_active is True

    def test_role_prefixes_have_a_sensible_default(self) -> None:
        prefixes = FilterSettings().role_based_email_prefixes
        assert "info" in prefixes
        assert "sales" in prefixes


class TestSecrets:
    def test_settings_are_hashable_by_pydantic_equality_but_hide_secrets(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LEAD_APOLLO__API_KEY", "abc123")
        settings: Settings = load_settings()
        assert "abc123" not in str(settings.model_dump())
