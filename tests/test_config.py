"""Tests for environment-driven configuration."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.config import FilterSettings, Settings, load_settings
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
