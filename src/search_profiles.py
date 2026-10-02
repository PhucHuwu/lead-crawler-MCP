"""Named, reusable search definitions loaded from YAML.

A *search profile* answers "who are we looking for?" as data rather than as a
list of flags. Profiles live in ``config/search_profiles.yaml`` (override with
``LEAD_SEARCH_PROFILES_PATH``) and are selected by name::

    python -m src.main --source apollo --search-profile sea_fintech

This module deliberately knows nothing about Apollo. It validates the *shape* of
a profile — strings in, strings out — and leaves the question of which of those
strings Apollo's API accepts to the adapter that speaks to Apollo. That is what
keeps a source-specific vocabulary from leaking into shared configuration.

Resolution order, weakest first::

    LEAD_APOLLO__* environment values  <  search profile  <  CLI flags
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from src.utils.errors import ConfigError
from src.utils.numbers import parse_employee_range

#: Profile used by a bare ``--search-profile`` with no name.
DEFAULT_PROFILE_NAME = "default"

#: Where profiles are read from unless ``LEAD_SEARCH_PROFILES_PATH`` says otherwise.
DEFAULT_PROFILES_PATH = Path("config/search_profiles.yaml")


class SearchProfile(BaseModel):
    """One named search definition.

    Every field is optional, and an unset field means "do not constrain on this"
    rather than "match nothing" — the distinction matters because Apollo treats
    an explicitly empty list differently from an absent key.
    """

    model_config = ConfigDict(extra="forbid")

    #: Optional human note, ignored by the crawler; useful in a shared file.
    description: str | None = None

    titles: list[str] = Field(default_factory=list)
    #: Source-vocabulary seniority values, passed through verbatim.
    seniorities: list[str] = Field(default_factory=list)
    #: Where the company is headquartered.
    locations: list[str] = Field(default_factory=list)
    #: Where the person is based. Rarely as useful as the company's.
    person_locations: list[str] = Field(default_factory=list)
    industries: list[str] = Field(default_factory=list)
    #: ``"min,max"`` headcount bands, canonicalized on load.
    employee_ranges: list[str] = Field(default_factory=list)
    #: Free-text terms ANDed into the search. Written as a string or a list in
    #: YAML; a list is joined with spaces, which is what the sources expect.
    keywords: str | None = None
    #: Let the source widen each title to near-equivalents. ``None`` defers to
    #: the adapter's own default.
    similar_titles: bool | None = None

    @field_validator("titles", "seniorities", "locations", "person_locations", "industries")
    @classmethod
    def _clean_string_list(cls, value: list[str]) -> list[str]:
        """Trim entries and drop blanks, preserving order and de-duplicating."""
        cleaned: list[str] = []
        for entry in value:
            text = str(entry).strip()
            if text and text not in cleaned:
                cleaned.append(text)
        return cleaned

    @field_validator("employee_ranges")
    @classmethod
    def _validate_employee_ranges(cls, value: list[str]) -> list[str]:
        """Canonicalize headcount bands and reject nonsense.

        Bands must be ordered, non-negative and non-overlapping. Overlap is
        almost always a typo (``"1-50"`` and ``"50-200"``), and letting it
        through would silently broaden the search rather than fail.

        YAML has no list-separator ambiguity, so ``201-500`` and ``201,500``
        are both accepted here — unlike the environment form, where a comma
        already means "next list item".
        """
        bands: list[tuple[int, int]] = []
        for entry in value:
            parsed = parse_employee_range(entry)
            if parsed is None:
                raise ValueError(
                    f'employee range {entry!r} must be a band such as "201-500" '
                    f"with 0 <= min <= max"
                )
            bands.append(parsed)

        ordered = sorted(bands)
        for (_, previous_high), (next_low, _) in zip(ordered, ordered[1:], strict=False):
            if next_low <= previous_high:
                raise ValueError(
                    f"employee ranges overlap around {next_low}; "
                    f'use disjoint bands such as "1-10" and "11-50"'
                )
        return [f"{low},{high}" for low, high in ordered]

    @field_validator("keywords", mode="before")
    @classmethod
    def _join_keywords(cls, value: object) -> object:
        """Accept ``payments`` or ``[payments, fintech]``; store one phrase.

        A list is the natural way to write this in YAML, and the sources take a
        single space-separated string, so the join happens here rather than
        being a rule every caller has to remember.
        """
        if isinstance(value, list):
            return " ".join(str(item).strip() for item in value if str(item).strip())
        return value

    @field_validator("keywords")
    @classmethod
    def _clean_keywords(cls, value: str | None) -> str | None:
        text = value.strip() if value else ""
        return text or None

    @property
    def is_empty(self) -> bool:
        """True when the profile constrains nothing and would search everyone."""
        return not any(
            (
                self.titles,
                self.seniorities,
                self.locations,
                self.person_locations,
                self.industries,
                self.employee_ranges,
                self.keywords,
            )
        )


def load_search_profiles(path: str | Path | None = None) -> dict[str, SearchProfile]:
    """Read every profile from a YAML file, keyed by name.

    Args:
        path: File to read. ``None`` uses :data:`DEFAULT_PROFILES_PATH`.

    Returns:
        Profile name -> profile. Names are casefolded so lookup is forgiving.

    Raises:
        ConfigError: if the file is missing, is not valid YAML, is not a mapping
            of names to profiles, or contains a profile that fails validation.
    """
    resolved = Path(path) if path is not None else DEFAULT_PROFILES_PATH

    try:
        text = resolved.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ConfigError(
            f"search profiles file not found: {resolved} "
            f"(set LEAD_SEARCH_PROFILES_PATH or create the file)"
        ) from exc
    except OSError as exc:
        raise ConfigError(f"cannot read search profiles file {resolved}: {exc}") from exc

    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{resolved} is not valid YAML: {exc}") from exc

    if document is None:
        raise ConfigError(f"{resolved} contains no profiles")
    if not isinstance(document, dict):
        raise ConfigError(
            f"{resolved} must be a mapping of profile names to settings, "
            f"got {type(document).__name__}"
        )

    profiles: dict[str, SearchProfile] = {}
    for raw_name, raw_profile in document.items():
        name = str(raw_name).strip()
        if not name:
            raise ConfigError(f"{resolved} has a profile with an empty name")
        try:
            profiles[name.casefold()] = SearchProfile.model_validate(raw_profile or {})
        except ValidationError as exc:
            raise ConfigError(f"profile {name!r} in {resolved} is invalid: {_format(exc)}") from exc
    return profiles


def get_search_profile(name: str | None, *, path: str | Path | None = None) -> SearchProfile | None:
    """Resolve one profile by name.

    Args:
        name: Profile to load. ``None`` returns ``None`` — no profile requested,
            which is the default and leaves existing settings untouched.
        path: File to read. ``None`` uses :data:`DEFAULT_PROFILES_PATH`.

    Raises:
        ConfigError: if the file cannot be read or the name is not defined in
            it. The message lists what *is* defined, since the usual cause is a
            typo and the file is not open in front of the user.
    """
    if name is None:
        return None

    profiles = load_search_profiles(path)
    key = name.strip().casefold()
    if key not in profiles:
        known = ", ".join(sorted(profiles)) or "<none>"
        raise ConfigError(f"unknown search profile {name!r}; available profiles: {known}")
    return profiles[key]


def _format(error: ValidationError) -> str:
    """Render a pydantic error as ``field: message`` pairs for a CLI."""
    return "; ".join(
        f"{'.'.join(str(part) for part in item['loc']) or '<profile>'}: {item['msg']}"
        for item in error.errors()
    )
