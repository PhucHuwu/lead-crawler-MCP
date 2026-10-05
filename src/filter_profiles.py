"""Named, reusable qualification rules loaded from YAML.

A *filter profile* answers "which leads do we keep?" as data rather than as a
list of flags. Profiles live in ``config/filters.yaml`` (override with
``LEAD_FILTER_PROFILES_PATH``) and are selected by name::

    python -m src.main --source apollo --filter-profile sea_fintech

Why a file rather than environment variables: an ideal-customer profile is a
campaign's shared, reviewed, version-controlled asset, not a per-operator shell
incantation. Nothing here is a house opinion — the module ships the *mechanism*
for expressing an ICP, and the YAML ships examples to copy. No rule is active
unless a profile is named, so the default behaviour is unchanged.

The YAML vocabulary is deliberately plainer than the internal one::

    allowed_titles          -> include_titles
    blocked_titles          -> exclude_titles
    allowed_countries       -> include_countries
    blocked_countries       -> exclude_countries
    seniority               -> include_seniority
    minimum_employee_count  -> min_employees
    maximum_employee_count  -> max_employees

Both spellings describe the same rule; the file reads as a policy document,
while the internal names stay consistent with every other ``include_*`` /
``exclude_*`` setting.

Resolution order, weakest first::

    LEAD_FILTERS__* environment values  <  filter profile  <  CLI flags

This is one half of a strategy; :mod:`src.profiles` resolves ``--profile NAME``
across this file and the search profiles file at once.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from src.config import FilterSettings
from src.utils.errors import ConfigError
from src.utils.names import profile_key
from src.utils.yaml_load import load_yaml_mapping

#: Where profiles are read from unless ``LEAD_FILTER_PROFILES_PATH`` says otherwise.
DEFAULT_PROFILES_PATH = Path("config/filters.yaml")

#: The profile a bare ``--filter-profile`` selects. Named rather than inlined so
#: the flag and the shipped file cannot disagree about what "the default" is.
DEFAULT_PROFILE_NAME = "default"


class FilterProfile(BaseModel):
    """One named set of qualification rules.

    Every field is optional, and an unset field means "do not constrain on this"
    rather than "match nothing" — the same contract as
    :class:`~src.config.FilterSettings`, which this is converted into.
    """

    model_config = ConfigDict(extra="forbid")

    #: Optional human note. Not used by the crawler; useful in a shared file to
    #: say *why* these rules exist, which is the part that goes stale silently.
    description: str | None = None

    # --- Titles ----------------------------------------------------------- #
    #: Job titles to keep. Matched against the whole normalized title, so "CTO"
    #: does not admit "Assistant to the CTO".
    allowed_titles: list[str] = Field(default_factory=list)
    blocked_titles: list[str] = Field(default_factory=list)

    # --- Geography -------------------------------------------------------- #
    #: ISO codes or names; both are understood.
    allowed_countries: list[str] = Field(default_factory=list)
    blocked_countries: list[str] = Field(default_factory=list)

    # --- Seniority -------------------------------------------------------- #
    #: Values from the shared vocabulary (``c_suite``, ``vp``, ``director``, …).
    seniority: list[str] = Field(default_factory=list)
    blocked_seniority: list[str] = Field(default_factory=list)

    # --- Firmographics ---------------------------------------------------- #
    industries: list[str] = Field(default_factory=list)
    blocked_industries: list[str] = Field(default_factory=list)
    minimum_employee_count: int | None = None
    maximum_employee_count: int | None = None
    #: Company domains to drop — competitors, or customers you already have.
    blocked_domains: list[str] = Field(default_factory=list)

    # --- Contactability --------------------------------------------------- #
    #: Dotted paths that must be present, e.g. ``company.name``. Validated
    #: against the lead model when the profile is converted.
    required_fields: list[str] = Field(default_factory=list)
    exclude_free_email: bool = False
    exclude_role_based_email: bool = False

    @field_validator(
        "allowed_titles",
        "blocked_titles",
        "allowed_countries",
        "blocked_countries",
        "seniority",
        "blocked_seniority",
        "industries",
        "blocked_industries",
        "blocked_domains",
        "required_fields",
    )
    @classmethod
    def _clean_string_list(cls, value: list[str]) -> list[str]:
        """Trim entries and drop blanks, preserving order and de-duplicating."""
        cleaned: list[str] = []
        for entry in value:
            text = str(entry).strip()
            if text and text not in cleaned:
                cleaned.append(text)
        return cleaned

    def to_filter_settings(self) -> FilterSettings:
        """Convert to the internal rule set.

        Raises:
            ConfigError: if a value is not a real rule value — an unknown dotted
                path or seniority. Deferred to here rather than validated in the
                model so the error can name the profile that is at fault, which
                is what the person editing the file needs to know.
        """
        try:
            return FilterSettings(
                include_titles=self.allowed_titles,
                exclude_titles=self.blocked_titles,
                include_countries=self.allowed_countries,
                exclude_countries=self.blocked_countries,
                include_seniority=self.seniority,
                exclude_seniority=self.blocked_seniority,
                include_industries=self.industries,
                exclude_industries=self.blocked_industries,
                min_employees=self.minimum_employee_count,
                max_employees=self.maximum_employee_count,
                exclude_domains=self.blocked_domains,
                required_fields=self.required_fields,
                exclude_free_email=self.exclude_free_email,
                exclude_role_based_email=self.exclude_role_based_email,
            )
        except ValidationError as exc:
            # No "filter profile" prefix here: every caller adds the profile's
            # name, and repeating it reads like two different faults.
            raise ConfigError(_format(exc)) from exc


def load_filter_profiles(path: str | Path | None = None) -> dict[str, FilterProfile]:
    """Read every profile from a YAML file, keyed by name.

    Args:
        path: File to read. ``None`` uses :data:`DEFAULT_PROFILES_PATH`.

    Returns:
        Profile name -> profile. Names are normalized (:func:`~src.utils.names.profile_key`)
        so lookup forgives case and separators.

    Raises:
        ConfigError: if the file is missing, is not valid YAML, is not a mapping
            of names to profiles, contains a profile that fails validation, or
            defines two names that differ only in punctuation.
    """
    resolved = Path(path) if path is not None else DEFAULT_PROFILES_PATH
    document = load_yaml_mapping(
        resolved, label="filter profiles", env_var="LEAD_FILTER_PROFILES_PATH"
    )

    profiles: dict[str, FilterProfile] = {}
    spellings: dict[str, str] = {}
    for raw_name, raw_profile in document.items():
        name = str(raw_name).strip()
        if not name:
            raise ConfigError(f"{resolved} has a profile with an empty name")
        key = profile_key(name)
        if key in spellings:
            # `sea-fintech` and `sea_fintech` are the same key, so the second
            # would silently replace the first — the exact failure this module
            # exists to prevent in the other direction.
            raise ConfigError(
                f"{resolved} defines both {spellings[key]!r} and {name!r}, which "
                f"differ only in punctuation; rename one of them"
            )
        spellings[key] = name
        try:
            profiles[key] = FilterProfile.model_validate(raw_profile or {})
        except ValidationError as exc:
            raise ConfigError(f"profile {name!r} in {resolved} is invalid: {_format(exc)}") from exc
    return profiles


def get_filter_profile(name: str | None, *, path: str | Path | None = None) -> FilterProfile | None:
    """Resolve one profile by name.

    Args:
        name: Profile to load. ``None`` returns ``None`` — no profile requested,
            which is the default and leaves the environment's rules untouched.
        path: File to read. ``None`` uses :data:`DEFAULT_PROFILES_PATH`.

    Raises:
        ConfigError: if the file cannot be read, the name is not defined in it,
            or the profile's rules are not valid. The message lists what *is*
            defined, since the usual cause is a typo and the file is not open in
            front of the user.
    """
    if name is None:
        return None

    profiles = load_filter_profiles(path)
    key = profile_key(name)
    if key not in profiles:
        known = ", ".join(sorted(profiles)) or "<none>"
        raise ConfigError(f"unknown filter profile {name!r}; available profiles: {known}")

    # Converted here, not at load time, so that a broken rule in one profile does
    # not make every *other* profile in the file unusable.
    profile = profiles[key]
    try:
        profile.to_filter_settings()
    except ConfigError as exc:
        raise ConfigError(f"filter profile {name!r} is invalid: {exc}") from exc
    return profile


def _format(error: ValidationError) -> str:
    """Render a pydantic error as one readable line for the CLI."""
    parts = []
    for issue in error.errors():
        location = ".".join(str(item) for item in issue["loc"]) or "<root>"
        parts.append(f"{location}: {issue['msg']}")
    return "; ".join(parts)
