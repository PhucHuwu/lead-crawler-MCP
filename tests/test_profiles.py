"""Tests for the umbrella profile resolver.

A profile names one acquisition strategy that spans two files: *who to look for*
(search) and *which of them to keep* (filters). These tests pin the three
properties that make that safe to rely on: one name reaches both halves, a name
defined in one file is a legitimate strategy rather than a broken one, and a name
defined in neither fails loudly with the alternatives in the message.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.profiles import (
    FILTER_HALF,
    SEARCH_HALF,
    merge_halves,
    profile_names,
    resolve_profile,
)
from src.search_profiles import SearchProfile
from src.utils.errors import ConfigError

#: The files shipped in the repository. Read directly so the documented example —
#: `--profile singapore_tech` — cannot drift into being unresolvable unnoticed.
SHIPPED_SEARCH = Path(__file__).resolve().parents[1] / "config" / "search_profiles.yaml"
SHIPPED_FILTERS = Path(__file__).resolve().parents[1] / "config" / "filters.yaml"

SEARCH_BODY = "default:\n  titles: [CTO]\nsearch_only:\n  titles: [CIO]\n"
FILTER_BODY = "default:\n  allowed_titles: [CTO]\nfilter_only:\n  allowed_titles: [CIO]\n"


def write_pair(tmp_path: Path, search: str, filters: str) -> tuple[Path, Path]:
    search_path = tmp_path / "search_profiles.yaml"
    filter_path = tmp_path / "filters.yaml"
    search_path.write_text(search, encoding="utf-8")
    filter_path.write_text(filters, encoding="utf-8")
    return search_path, filter_path


class TestResolving:
    def test_a_name_in_both_files_resolves_both_halves(self, tmp_path: Path) -> None:
        search_path, filter_path = write_pair(tmp_path, SEARCH_BODY, FILTER_BODY)
        resolved = resolve_profile("default", search_path=search_path, filter_path=filter_path)
        assert resolved.search is not None
        assert resolved.search.titles == ["CTO"]
        assert resolved.filters is not None
        assert resolved.filters.allowed_titles == ["CTO"]
        assert resolved.defined_halves == (SEARCH_HALF, FILTER_HALF)
        assert not resolved.is_empty

    def test_a_name_in_one_file_resolves_that_half_only(self, tmp_path: Path) -> None:
        # A strategy that only narrows the search is still a strategy. The
        # missing half contributes nothing rather than failing the run.
        search_path, filter_path = write_pair(tmp_path, SEARCH_BODY, FILTER_BODY)
        resolved = resolve_profile("search_only", search_path=search_path, filter_path=filter_path)
        assert resolved.defined_halves == (SEARCH_HALF,)
        assert resolved.filters is None
        assert not resolved.is_empty

    def test_the_other_half_only_also_resolves(self, tmp_path: Path) -> None:
        search_path, filter_path = write_pair(tmp_path, SEARCH_BODY, FILTER_BODY)
        resolved = resolve_profile("filter_only", search_path=search_path, filter_path=filter_path)
        assert resolved.defined_halves == (FILTER_HALF,)
        assert resolved.search is None

    def test_a_name_in_neither_file_is_an_error_naming_both_paths(self, tmp_path: Path) -> None:
        search_path, filter_path = write_pair(tmp_path, SEARCH_BODY, FILTER_BODY)
        with pytest.raises(ConfigError) as excinfo:
            resolve_profile("typo", search_path=search_path, filter_path=filter_path)
        message = str(excinfo.value)
        # A typo is the usual cause and neither file is open in front of the
        # reader, so the message has to carry the alternatives and where it looked.
        assert "search_profiles.yaml" in message
        assert "filters.yaml" in message
        assert "default" in message

    def test_the_alternative_list_marks_which_halves_each_name_covers(self, tmp_path: Path) -> None:
        search_path, filter_path = write_pair(tmp_path, SEARCH_BODY, FILTER_BODY)
        with pytest.raises(ConfigError) as excinfo:
            resolve_profile("typo", search_path=search_path, filter_path=filter_path)
        message = str(excinfo.value)
        assert "default (search+filters)" in message
        assert "search_only (search)" in message
        assert "filter_only (filters)" in message

    @pytest.mark.parametrize(
        "typed", ["SEARCH_ONLY", "Search_Only", "Search-Only", "search only", " search_only "]
    )
    def test_lookup_forgives_case_separator_and_padding(self, tmp_path: Path, typed: str) -> None:
        # The name is typed at a shell and written in a file, so it is spelled
        # two ways by definition. Nobody remembers which separator the file used.
        # Only the *joining* character is forgiven, not the words themselves:
        # `de-fault` is not `default`, and must not silently become it.
        search_path, filter_path = write_pair(tmp_path, SEARCH_BODY, FILTER_BODY)
        resolved = resolve_profile(typed, search_path=search_path, filter_path=filter_path)
        assert resolved.name == "search_only"
        assert resolved.search is not None
        assert resolved.filters is None

    def test_the_resolved_name_is_the_normalized_one(self, tmp_path: Path) -> None:
        # Stored back into settings, so it must be the spelling a later lookup
        # will match — not whatever the user typed.
        search_path, filter_path = write_pair(tmp_path, SEARCH_BODY, FILTER_BODY)
        resolved = resolve_profile("Search-Only", search_path=search_path, filter_path=filter_path)
        assert resolved.name == "search_only"

    def test_a_broken_filter_half_is_reported_with_the_profile_name(self, tmp_path: Path) -> None:
        # The whole named strategy is refused rather than half-applied: a run
        # that quietly qualified against no rules would look exactly like one
        # that worked.
        search_path, filter_path = write_pair(
            tmp_path, SEARCH_BODY, "default:\n  required_fields: [company.nmae]\n"
        )
        with pytest.raises(ConfigError) as excinfo:
            resolve_profile("default", search_path=search_path, filter_path=filter_path)
        message = str(excinfo.value)
        assert "'default'" in message
        assert "company.nmae" in message


class TestListing:
    def test_names_from_both_files_are_merged(self, tmp_path: Path) -> None:
        search_path, filter_path = write_pair(tmp_path, SEARCH_BODY, FILTER_BODY)
        entries = dict(profile_names(search_path=search_path, filter_path=filter_path))
        assert set(entries) == {"default", "search_only", "filter_only"}
        assert entries["default"] == frozenset({SEARCH_HALF, FILTER_HALF})
        assert entries["search_only"] == frozenset({SEARCH_HALF})

    def test_the_listing_is_sorted(self, tmp_path: Path) -> None:
        search_path, filter_path = write_pair(tmp_path, SEARCH_BODY, FILTER_BODY)
        names = [
            name for name, _ in profile_names(search_path=search_path, filter_path=filter_path)
        ]
        assert names == sorted(names)

    def test_merging_is_order_independent(self) -> None:
        # The two files are read in a fixed order today, but the merged view must
        # not depend on it — a half's identity is a set, not a sequence position.
        halves = merge_halves({"a": SearchProfile()}, {})
        assert halves == [("a", frozenset({SEARCH_HALF}))]


class TestShippedFiles:
    """The example the README tells people to run must work as shipped."""

    def test_the_documented_umbrella_profile_resolves_both_halves(self) -> None:
        resolved = resolve_profile(
            "singapore_tech", search_path=SHIPPED_SEARCH, filter_path=SHIPPED_FILTERS
        )
        assert resolved.defined_halves == (SEARCH_HALF, FILTER_HALF)

    def test_the_documented_hyphenated_spelling_reaches_it(self) -> None:
        # The profile is named with an underscore because every other shipped
        # profile is; the hyphen is what people type.
        resolved = resolve_profile(
            "singapore-tech", search_path=SHIPPED_SEARCH, filter_path=SHIPPED_FILTERS
        )
        assert resolved.name == "singapore_tech"

    def test_every_shipped_filter_half_is_valid(self) -> None:
        # Resolution converts the filter half to catch a rule that cannot work;
        # every shipped profile must survive that, or `--profile` is a trap.
        for name, halves in profile_names(search_path=SHIPPED_SEARCH, filter_path=SHIPPED_FILTERS):
            if FILTER_HALF in halves:
                assert (
                    resolve_profile(
                        name, search_path=SHIPPED_SEARCH, filter_path=SHIPPED_FILTERS
                    ).filters
                    is not None
                )
