"""Tests for the named search-profile layer.

Profiles exist so a user can change *what* gets searched without editing Python
or exporting a wall of ``LEAD_APOLLO__*`` variables. These tests pin the two
things that makes them safe: a broken file fails loudly before any request is
made, and a profile only overrides the fields it actually sets.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.search_profiles import (
    DEFAULT_PROFILE_NAME,
    SearchProfile,
    get_search_profile,
    load_search_profiles,
)
from src.utils.errors import ConfigError

#: The example shipped in the repository. Read directly so the documented file
#: cannot drift into being unloadable without a test noticing.
SHIPPED_PROFILES = Path(__file__).resolve().parents[1] / "config" / "search_profiles.yaml"


def write_profiles(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "profiles.yaml"
    path.write_text(body, encoding="utf-8")
    return path


class TestLoading:
    def test_the_shipped_example_loads(self) -> None:
        profiles = load_search_profiles(SHIPPED_PROFILES)
        assert DEFAULT_PROFILE_NAME in profiles
        assert "sea_fintech" in profiles

    def test_the_default_profile_matches_the_documented_example(self) -> None:
        profile = get_search_profile(DEFAULT_PROFILE_NAME, path=SHIPPED_PROFILES)
        assert profile is not None
        assert profile.titles == ["CTO", "VP Engineering", "Head of Engineering", "Founder"]
        assert profile.seniorities == ["c_suite", "vp", "head", "founder"]
        assert profile.locations == ["Singapore", "Japan", "Australia"]

    def test_names_are_case_insensitive(self, tmp_path: Path) -> None:
        path = write_profiles(tmp_path, "Default:\n  titles: [CTO]\n")
        assert get_search_profile("DEFAULT", path=path) is not None

    def test_no_name_means_no_profile(self, tmp_path: Path) -> None:
        # `None` is the default and must not touch the filesystem, so a missing
        # file cannot break a run that never asked for a profile.
        assert get_search_profile(None, path=tmp_path / "absent.yaml") is None

    def test_a_missing_file_names_the_setting(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="search profiles file not found"):
            load_search_profiles(tmp_path / "absent.yaml")

    def test_invalid_yaml_is_reported(self, tmp_path: Path) -> None:
        path = write_profiles(tmp_path, "default: [unclosed\n")
        with pytest.raises(ConfigError, match="not valid YAML"):
            load_search_profiles(path)

    def test_an_empty_file_is_reported(self, tmp_path: Path) -> None:
        path = write_profiles(tmp_path, "")
        with pytest.raises(ConfigError, match="contains no search profiles"):
            load_search_profiles(path)

    def test_a_non_mapping_document_is_reported(self, tmp_path: Path) -> None:
        path = write_profiles(tmp_path, "- default\n- other\n")
        with pytest.raises(ConfigError, match="must be a mapping"):
            load_search_profiles(path)

    def test_unknown_profile_lists_what_exists(self, tmp_path: Path) -> None:
        path = write_profiles(tmp_path, "default:\n  titles: [CTO]\nsea:\n  titles: [CIO]\n")
        with pytest.raises(ConfigError) as excinfo:
            get_search_profile("typo", path=path)
        # A typo is the usual cause and the file is rarely open in front of the
        # user, so the message has to carry the alternative names.
        assert "default" in str(excinfo.value)
        assert "sea" in str(excinfo.value)


class TestValidation:
    def test_a_bad_field_names_the_profile_and_field(self, tmp_path: Path) -> None:
        path = write_profiles(tmp_path, "default:\n  titles: CTO\n")
        with pytest.raises(ConfigError) as excinfo:
            load_search_profiles(path)
        message = str(excinfo.value)
        assert "'default'" in message
        assert "titles" in message

    def test_unknown_keys_are_rejected(self, tmp_path: Path) -> None:
        # A misspelled filter silently searching for everyone is the failure
        # mode worth failing loudly on, so the message has to name the key.
        path = write_profiles(tmp_path, "default:\n  title: [CTO]\n")
        with pytest.raises(ConfigError, match="title: Extra inputs"):
            load_search_profiles(path)

    @pytest.mark.parametrize("entry", ["201-500", "201 to 500", "201 – 500"])
    def test_employee_bands_accept_common_spellings(self, tmp_path: Path, entry: str) -> None:
        path = write_profiles(tmp_path, f"default:\n  employee_ranges: ['{entry}']\n")
        profile = load_search_profiles(path)["default"]
        # Normalized to the form Apollo expects, whatever the author typed.
        assert profile.employee_ranges == ["201,500"]

    def test_an_overlapping_band_is_rejected(self, tmp_path: Path) -> None:
        path = write_profiles(tmp_path, "default:\n  employee_ranges: ['201-500', '400-900']\n")
        with pytest.raises(ConfigError, match="overlap"):
            load_search_profiles(path)

    def test_a_malformed_band_is_rejected(self, tmp_path: Path) -> None:
        path = write_profiles(tmp_path, "default:\n  employee_ranges: ['lots']\n")
        with pytest.raises(ConfigError, match="employee_range|employee range"):
            load_search_profiles(path)

    def test_keywords_may_be_written_as_a_list(self, tmp_path: Path) -> None:
        # YAML authors reach for a list even though Apollo wants one string.
        path = write_profiles(tmp_path, "default:\n  keywords: [payments, lending]\n")
        assert load_search_profiles(path)["default"].keywords == "payments lending"

    def test_blank_entries_are_dropped(self, tmp_path: Path) -> None:
        path = write_profiles(tmp_path, "default:\n  titles: ['CTO', '  ', '']\n")
        assert load_search_profiles(path)["default"].titles == ["CTO"]


class TestNameNormalization:
    def test_a_hyphenated_spelling_reaches_an_underscored_name(self, tmp_path: Path) -> None:
        # The name is written in the file and typed at a shell, so it is spelled
        # two ways by definition.
        path = write_profiles(tmp_path, "sea_fintech:\n  titles: [CTO]\n")
        assert get_search_profile("sea-fintech", path=path) is not None

    def test_names_differing_only_in_punctuation_are_rejected(self, tmp_path: Path) -> None:
        # They normalize to one key, so the second would silently replace the
        # first — a profile that quietly does not apply is the failure this
        # module exists to prevent.
        path = write_profiles(
            tmp_path, "sea_fintech:\n  titles: [CTO]\nsea-fintech:\n  titles: [CIO]\n"
        )
        with pytest.raises(ConfigError) as excinfo:
            load_search_profiles(path)
        message = str(excinfo.value)
        assert "sea_fintech" in message
        assert "sea-fintech" in message


class TestEmptiness:
    def test_an_unset_profile_is_empty(self) -> None:
        assert SearchProfile().is_empty

    def test_a_profile_with_any_filter_is_not_empty(self) -> None:
        assert not SearchProfile(titles=["CTO"]).is_empty

    def test_a_description_alone_is_still_empty(self) -> None:
        # A note to the reader constrains nothing, so it must not count as a
        # configured search.
        assert SearchProfile(description="our ICP").is_empty
