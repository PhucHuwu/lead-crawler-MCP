"""Named acquisition strategies, spanning both profile files.

A *profile* is the umbrella over the two halves of "who are we acquiring?":

* the **search** profile says who to look for — titles, seniority, locations;
* the **filter** profile says which of them to keep — countries, size, contactability.

Selecting one by name is the whole feature::

    python -m src.main --source apollo --profile singapore_tech

The two halves stay in their own files, because that is what keeps a search
definition readable to whoever owns the targeting and a qualification rule
readable to whoever owns the ICP. A profile is not a third document: it is the
same name looked up in both, so there is nothing to keep in sync.

A name defined in only one file is legitimate and not an error. A strategy that
only narrows the search, or only qualifies results, is still a strategy; the
half that is absent simply contributes nothing, and :func:`profile_names` shows
which halves exist at a glance.

Resolution order, weakest first::

    environment  <  the profile named by --profile  <  --search-profile/--filter-profile

An explicit half always beats the umbrella, so a one-off run can keep a
strategy's search and swap its qualification rules without editing a file.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from src.filter_profiles import (
    DEFAULT_PROFILES_PATH as DEFAULT_FILTER_PROFILES_PATH,
)
from src.filter_profiles import FilterProfile, load_filter_profiles
from src.search_profiles import (
    DEFAULT_PROFILES_PATH as DEFAULT_SEARCH_PROFILES_PATH,
)
from src.search_profiles import SearchProfile, load_search_profiles
from src.utils.errors import ConfigError
from src.utils.names import profile_key

#: The two halves a profile can define, in the order they are applied.
SEARCH_HALF = "search"
FILTER_HALF = "filters"
HALVES: tuple[str, str] = (SEARCH_HALF, FILTER_HALF)


@dataclass(frozen=True, slots=True)
class ResolvedProfile:
    """One named strategy, with whichever halves the files define."""

    name: str
    search: SearchProfile | None = None
    filters: FilterProfile | None = None

    @property
    def defined_halves(self) -> tuple[str, ...]:
        """Which of the two halves this name actually defines."""
        found = []
        if self.search is not None:
            found.append(SEARCH_HALF)
        if self.filters is not None:
            found.append(FILTER_HALF)
        return tuple(found)


def profile_names(
    *,
    search_path: str | Path | None = None,
    filter_path: str | Path | None = None,
) -> list[tuple[str, frozenset[str]]]:
    """Every profile name defined in either file, with the halves it defines.

    Returns:
        ``(name, halves)`` pairs sorted by name. A name in both files appears
        once, carrying both halves — that is the shape this module exists to make
        visible, because a name in only one file applies in only one place.
    """
    return sorted(
        merge_halves(load_search_profiles(search_path), load_filter_profiles(filter_path))
    )


def merge_halves(
    search_profiles: dict[str, SearchProfile], filter_profiles: dict[str, FilterProfile]
) -> list[tuple[str, frozenset[str]]]:
    """Combine two name -> profile maps into name -> halves that define it."""
    halves: dict[str, set[str]] = {}
    for half, names in ((SEARCH_HALF, search_profiles), (FILTER_HALF, filter_profiles)):
        for name in names:
            halves.setdefault(name, set()).add(half)
    return [(name, frozenset(found)) for name, found in halves.items()]


def resolve_profile(
    name: str,
    *,
    search_path: str | Path | None = None,
    filter_path: str | Path | None = None,
) -> ResolvedProfile:
    """Resolve one profile name across both files.

    Args:
        name: Profile to load. Matched like both loaders match — case and
            separator insensitive, so ``singapore-tech`` finds ``singapore_tech``.
        search_path: Search profiles file. ``None`` uses its default.
        filter_path: Filter profiles file. ``None`` uses its default.

    Returns:
        The strategy, with ``None`` for a half the name does not define.

    Raises:
        ConfigError: if the name is defined in neither file, or if the filter
            half's rules are not valid. The message lists every name that *is*
            available — with the half each one covers and the files searched —
            since the usual cause is a typo and neither file is open in front of
            the person reading the error.
    """
    search_profiles = load_search_profiles(search_path)
    filter_profiles = load_filter_profiles(filter_path)

    key = profile_key(name)
    search = search_profiles.get(key)
    filters = filter_profiles.get(key)
    if search is None and filters is None:
        searched = _describe_paths(
            search_path or DEFAULT_SEARCH_PROFILES_PATH,
            filter_path or DEFAULT_FILTER_PROFILES_PATH,
        )
        raise ConfigError(
            f"unknown profile {name!r}; searched {searched}. "
            f"Available profiles: {_annotate(merge_halves(search_profiles, filter_profiles))}"
        )

    if filters is not None:
        # Converted here rather than at load time, so a broken rule in one profile
        # does not make every other profile in the file unusable — and so the
        # error can name the profile at fault.
        try:
            filters.to_filter_settings()
        except ConfigError as exc:
            raise ConfigError(f"profile {name!r} is invalid: {exc}") from exc

    return ResolvedProfile(name=key, search=search, filters=filters)


def _annotate(entries: list[tuple[str, frozenset[str]]]) -> str:
    """Render ``name (search+filters)`` for every entry, or ``<none>``.

    The annotation is always shown, including when the name has both halves.
    Marking only the incomplete ones would leave the reader guessing whether an
    unmarked name is complete or simply unexamined.
    """
    if not entries:
        return "<none>"
    return ", ".join(
        f"{name} ({'+'.join(half for half in HALVES if half in halves)})"
        for name, halves in entries
    )


def _describe_paths(search_path: str | Path, filter_path: str | Path) -> str:
    """Both file locations, without repeating one that was used twice."""
    first, second = str(search_path), str(filter_path)
    return f"{first} and {second}" if first != second else first
