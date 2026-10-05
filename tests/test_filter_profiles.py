"""Tests for the named filter-profile layer.

Profiles exist so an ICP is a reviewed, version-controlled file rather than a
list of flags retyped per run. These tests pin the three things that make that
safe: a broken profile fails loudly before any crawling, a profile only overrides
the rules it actually sets, and nothing is loaded unless a profile is named.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.filter_profiles import (
    DEFAULT_PROFILE_NAME,
    DEFAULT_PROFILES_PATH,
    FilterProfile,
    get_filter_profile,
    load_filter_profiles,
)
from src.models.enums import SeniorityLevel
from src.utils.errors import ConfigError

#: The example shipped in the repository. Read directly so the documented file
#: cannot drift into being unloadable without a test noticing.
SHIPPED_PROFILES = Path(__file__).resolve().parents[1] / "config" / "filters.yaml"


def write_profiles(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "filters.yaml"
    path.write_text(body, encoding="utf-8")
    return path


class TestLoading:
    def test_the_shipped_example_loads(self) -> None:
        profiles = load_filter_profiles(SHIPPED_PROFILES)
        assert DEFAULT_PROFILE_NAME in profiles
        assert "enterprise_na" in profiles

    def test_the_default_profile_matches_the_documented_example(self) -> None:
        profile = get_filter_profile(DEFAULT_PROFILE_NAME, path=SHIPPED_PROFILES)
        assert profile is not None
        assert profile.allowed_titles == [
            "CTO",
            "VP Engineering",
            "Head of Engineering",
            "Founder",
        ]
        assert profile.minimum_employee_count == 10
        assert profile.maximum_employee_count == 1000
        assert profile.required_fields == ["company.name"]

    def test_the_default_path_is_the_shipped_file(self) -> None:
        assert Path("config/filters.yaml") == DEFAULT_PROFILES_PATH

    def test_names_are_case_insensitive(self, tmp_path: Path) -> None:
        path = write_profiles(tmp_path, "Default:\n  allowed_titles: [CTO]\n")
        assert get_filter_profile("DEFAULT", path=path) is not None

    def test_no_name_means_no_profile(self, tmp_path: Path) -> None:
        # `None` is the default and must not touch the filesystem, so a missing
        # file cannot break a run that never asked for a profile. This is what
        # keeps the whole feature inert by default.
        assert get_filter_profile(None, path=tmp_path / "absent.yaml") is None

    def test_a_missing_file_names_the_setting(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="filter profiles file not found"):
            load_filter_profiles(tmp_path / "absent.yaml")

    def test_invalid_yaml_is_reported(self, tmp_path: Path) -> None:
        path = write_profiles(tmp_path, "default: [unclosed\n")
        with pytest.raises(ConfigError, match="not valid YAML"):
            load_filter_profiles(path)

    def test_an_empty_file_is_reported(self, tmp_path: Path) -> None:
        path = write_profiles(tmp_path, "")
        with pytest.raises(ConfigError, match="contains no filter profiles"):
            load_filter_profiles(path)

    def test_a_non_mapping_document_is_reported(self, tmp_path: Path) -> None:
        path = write_profiles(tmp_path, "- default\n- other\n")
        with pytest.raises(ConfigError, match="must be a mapping"):
            load_filter_profiles(path)

    def test_an_empty_named_profile_is_allowed(self, tmp_path: Path) -> None:
        # A profile with no rules is legal and means "keep everything" — useful
        # as a placeholder while an ICP is still being agreed.
        path = write_profiles(tmp_path, "default:\n")
        assert load_filter_profiles(path)[DEFAULT_PROFILE_NAME] == FilterProfile()

    def test_unknown_profile_lists_what_exists(self, tmp_path: Path) -> None:
        path = write_profiles(
            tmp_path, "default:\n  industries: [software]\nsea:\n  seniority: [vp]\n"
        )
        with pytest.raises(ConfigError) as excinfo:
            get_filter_profile("typo", path=path)
        # A typo is the usual cause and the file is rarely open in front of the
        # user, so the message has to carry the alternative names.
        assert "default" in str(excinfo.value)
        assert "sea" in str(excinfo.value)


class TestValidation:
    def test_a_bad_field_names_the_profile_and_field(self, tmp_path: Path) -> None:
        path = write_profiles(tmp_path, "default:\n  allowed_titles: CTO\n")
        with pytest.raises(ConfigError) as excinfo:
            load_filter_profiles(path)
        message = str(excinfo.value)
        assert "'default'" in message
        assert "allowed_titles" in message

    def test_unknown_keys_are_rejected(self, tmp_path: Path) -> None:
        # A misspelled rule silently keeping everyone is the failure mode worth
        # failing loudly on, so the message has to name the key.
        path = write_profiles(tmp_path, "default:\n  allowed_title: [CTO]\n")
        with pytest.raises(ConfigError, match="allowed_title: Extra inputs"):
            load_filter_profiles(path)

    def test_blank_entries_are_dropped(self, tmp_path: Path) -> None:
        path = write_profiles(tmp_path, "default:\n  allowed_titles: ['CTO', '  ', '']\n")
        assert load_filter_profiles(path)[DEFAULT_PROFILE_NAME].allowed_titles == ["CTO"]

    def test_duplicate_entries_are_collapsed(self, tmp_path: Path) -> None:
        path = write_profiles(tmp_path, "default:\n  allowed_titles: [CTO, cto, CTO]\n")
        assert load_filter_profiles(path)[DEFAULT_PROFILE_NAME].allowed_titles == ["CTO", "cto"]

    def test_entries_are_trimmed(self, tmp_path: Path) -> None:
        path = write_profiles(tmp_path, "default:\n  allowed_countries: ['  SG  ']\n")
        assert load_filter_profiles(path)[DEFAULT_PROFILE_NAME].allowed_countries == ["SG"]

    def test_a_bad_required_path_is_reported_with_the_profile_name(self, tmp_path: Path) -> None:
        # Deferred to conversion, so that one broken profile does not make every
        # other profile in the file unusable — but still loud when selected.
        path = write_profiles(tmp_path, "broken:\n  required_fields: [company.nmae]\n")
        with pytest.raises(ConfigError) as excinfo:
            get_filter_profile("broken", path=path)
        message = str(excinfo.value)
        assert "'broken'" in message
        assert "company.nmae" in message

    def test_a_bad_seniority_is_reported(self, tmp_path: Path) -> None:
        path = write_profiles(tmp_path, "default:\n  seniority: [chief]\n")
        with pytest.raises(ConfigError, match="seniority"):
            get_filter_profile("default", path=path)

    def test_a_minimum_above_the_maximum_is_reported(self, tmp_path: Path) -> None:
        path = write_profiles(
            tmp_path,
            "default:\n  minimum_employee_count: 500\n  maximum_employee_count: 10\n",
        )
        with pytest.raises(ConfigError, match="greater than"):
            get_filter_profile("default", path=path)

    def test_listing_tolerates_a_profile_whose_rules_are_broken(self, tmp_path: Path) -> None:
        # `--list-filter-profiles` must still show the file's shape, so a bad
        # rule in one profile cannot make the whole file unlistable.
        path = write_profiles(
            tmp_path,
            "good:\n  allowed_titles: [CTO]\nbroken:\n  required_fields: [company.nmae]\n",
        )
        assert set(load_filter_profiles(path)) == {"good", "broken"}


class TestNameNormalization:
    def test_a_hyphenated_spelling_reaches_an_underscored_name(self, tmp_path: Path) -> None:
        # The name is written in the file and typed at a shell, so it is spelled
        # two ways by definition.
        path = write_profiles(tmp_path, "sea_fintech:\n  allowed_titles: [CTO]\n")
        assert get_filter_profile("sea-fintech", path=path) is not None

    def test_names_differing_only_in_punctuation_are_rejected(self, tmp_path: Path) -> None:
        # They normalize to one key, so the second would silently replace the
        # first — a profile that quietly does not apply is the failure this
        # module exists to prevent.
        path = write_profiles(
            tmp_path,
            "sea_fintech:\n  allowed_titles: [CTO]\nsea-fintech:\n  allowed_titles: [CIO]\n",
        )
        with pytest.raises(ConfigError) as excinfo:
            load_filter_profiles(path)
        message = str(excinfo.value)
        assert "sea_fintech" in message
        assert "sea-fintech" in message


class TestConversion:
    def test_the_yaml_vocabulary_maps_onto_the_internal_one(self) -> None:
        profile = FilterProfile(
            allowed_titles=["CTO"],
            blocked_titles=["Intern"],
            allowed_countries=["SG"],
            blocked_countries=["CN"],
            seniority=["vp"],
            blocked_seniority=["intern"],
            industries=["software"],
            blocked_industries=["gambling"],
            minimum_employee_count=10,
            maximum_employee_count=1000,
            blocked_domains=["competitor.com"],
            required_fields=["company.name"],
            exclude_free_email=True,
            exclude_role_based_email=True,
        )
        rules = profile.to_filter_settings()
        assert rules.include_titles == ["CTO"]
        assert rules.exclude_titles == ["Intern"]
        assert rules.include_countries == ["SG"]
        assert rules.exclude_countries == ["CN"]
        assert rules.include_seniority == [SeniorityLevel.VP]
        assert rules.exclude_seniority == [SeniorityLevel.INTERN]
        assert rules.include_industries == ["software"]
        assert rules.exclude_industries == ["gambling"]
        assert rules.min_employees == 10
        assert rules.max_employees == 1000
        assert rules.exclude_domains == ["competitor.com"]
        assert rules.required_fields == ["company.name"]
        assert rules.exclude_free_email is True
        assert rules.exclude_role_based_email is True

    def test_an_empty_profile_constrains_nothing(self) -> None:
        assert FilterProfile().to_filter_settings().is_active is False

    def test_a_description_alone_constrains_nothing(self) -> None:
        # A note to the reader is not a rule, so it must not make a run filter.
        assert FilterProfile(description="our ICP").to_filter_settings().is_active is False

    def test_a_profile_with_any_rule_is_active(self) -> None:
        assert FilterProfile(allowed_titles=["CTO"]).to_filter_settings().is_active is True


class TestShippedExampleIsInert:
    """The shipped file must not change behaviour unless a profile is named."""

    def test_selecting_nothing_loads_nothing(self) -> None:
        assert get_filter_profile(None, path=SHIPPED_PROFILES) is None

    def test_the_shipped_profiles_all_convert(self) -> None:
        # Every profile in the repository file must be usable as shipped.
        for name, profile in load_filter_profiles(SHIPPED_PROFILES).items():
            assert profile.to_filter_settings() is not None, name
