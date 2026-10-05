"""Environment-driven configuration.

Every tunable lives here and is read from the environment (optionally seeded by a
``.env`` file), so no behaviour is hardcoded and no credential ever appears in
source. CLI flags override individual values for a single run.

Environment variable names are the field names upper-cased and prefixed with
``LEAD_``; nested settings use a double underscore::

    LEAD_LOG_LEVEL=DEBUG
    LEAD_DEFAULT_LIMIT=250
    LEAD_FILTERS__MIN_EMPLOYEES=50
    LEAD_FILTERS__EXCLUDE_COUNTRIES=IN,CN
    LEAD_APOLLO__API_KEY=sk-...

List-valued settings accept either comma-separated values (``US,CA``) or a JSON
array (``["US","CA"]``); the comma form is what humans actually type in a shell.

Secrets belong here and nowhere else — YAML holds crawler *behaviour* (see
:mod:`src.profiles`), never credentials.

The prefix is enforced in both directions: :func:`check_env_names` refuses to
start when a variable looks like one of ours but is spelled in a way this tool
never reads, because ``APOLLO_API_KEY=sk-live-…`` being ignored is
indistinguishable from it being absent.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Annotated, Any

from dotenv import dotenv_values
from pydantic import (
    BaseModel,
    BeforeValidator,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from src.models.enums import DedupStrategy, ExportFormat, LogFormat, SeniorityLevel
from src.models.lead import is_lead_field_path
from src.utils.errors import ConfigError
from src.utils.io import ensure_directory
from src.utils.numbers import parse_employee_range
from src.utils.redaction import iter_secret_values, register_secrets


def _split_list(value: Any) -> Any:
    """Decode a list-valued setting from either comma-separated or JSON text."""
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            decoded = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"expected a JSON array or comma-separated list, got {text!r}"
            ) from exc
        if not isinstance(decoded, list):
            raise ValueError(f"expected a JSON array, got {type(decoded).__name__}")
        return decoded
    return [part.strip() for part in text.split(",") if part.strip()]


#: ``list[str]`` read from the environment as CSV or JSON rather than JSON only.
CommaSeparated = Annotated[list[str], NoDecode, BeforeValidator(_split_list)]
#: Same, narrowed to the seniority vocabulary.
SeniorityList = Annotated[list[SeniorityLevel], NoDecode, BeforeValidator(_split_list)]
#: Same, narrowed to the export formats we can actually write.
ExportFormatList = Annotated[list[ExportFormat], NoDecode, BeforeValidator(_split_list)]


#: Unprefixed spellings of this tool's own settings, which pydantic-settings
#: never reads because it looks for the ``LEAD_`` form only. Curated rather than
#: derived: the bare form of a *section* field (``MOCK``, ``WEBSITE``) or of a
#: generic top-level name (``PROFILE``) is something other tools legitimately
#: export, and here a false alarm is worse than a missed one — it stops a run
#: that was configured correctly. ``APOLLO_API_KEY`` is the one that earns the
#: list its keep: a secret that is silently ignored looks exactly like no secret
#: at all, and the expensive failure is discovering that after the run.
_BARE_ENV_ALIASES: dict[str, str] = {
    "APOLLO_API_KEY": "LEAD_APOLLO__API_KEY",
    "APOLLO_BASE_URL": "LEAD_APOLLO__BASE_URL",
    "HTTP_TIMEOUT": "LEAD_HTTP_TIMEOUT",
    "HTTP_MAX_ATTEMPTS": "LEAD_HTTP_MAX_ATTEMPTS",
    "LOG_LEVEL": "LEAD_LOG_LEVEL",
    "LOG_FORMAT": "LEAD_LOG_FORMAT",
    "DEDUP_STRATEGY": "LEAD_DEDUP_STRATEGY",
    "MIN_COMPLETENESS": "LEAD_MIN_COMPLETENESS",
    "MAX_CONCURRENCY": "LEAD_MAX_CONCURRENCY",
    "DEFAULT_SOURCES": "LEAD_DEFAULT_SOURCES",
    "DEFAULT_LIMIT": "LEAD_DEFAULT_LIMIT",
    "OUTPUT_DIR": "LEAD_OUTPUT_DIR",
    "OUTPUT_FORMATS": "LEAD_OUTPUT_FORMATS",
    "OUTPUT_PREFIX": "LEAD_OUTPUT_PREFIX",
    "WRITE_RUN_REPORT": "LEAD_WRITE_RUN_REPORT",
    "SEARCH_PROFILE": "LEAD_SEARCH_PROFILE",
    "FILTER_PROFILE": "LEAD_FILTER_PROFILE",
    "SEARCH_PROFILES_PATH": "LEAD_SEARCH_PROFILES_PATH",
    "FILTER_PROFILES_PATH": "LEAD_FILTER_PROFILES_PATH",
}


class FilterSettings(BaseModel):
    """Qualification rules applied after validation.

    Every rule is opt-in. With defaults, the pipeline keeps every lead that
    survives validation — filtering is a deliberate narrowing, never a surprise.
    """

    model_config = {"extra": "forbid"}

    # --- Geography -------------------------------------------------------- #
    #: When non-empty, keep only leads whose country matches one of these.
    include_countries: CommaSeparated = Field(default_factory=list)
    exclude_countries: CommaSeparated = Field(default_factory=list)

    # --- Firmographics ---------------------------------------------------- #
    include_industries: CommaSeparated = Field(default_factory=list)
    exclude_industries: CommaSeparated = Field(default_factory=list)
    min_employees: int | None = None
    max_employees: int | None = None
    #: Company domains to drop, e.g. competitors or existing customers.
    exclude_domains: CommaSeparated = Field(default_factory=list)

    # --- Person ----------------------------------------------------------- #
    include_seniority: SeniorityList = Field(default_factory=list)
    exclude_seniority: SeniorityList = Field(default_factory=list)
    #: Job titles to keep, matched on the **whole** normalized title rather than
    #: as a substring: ``CTO`` accepts ``CTO`` and ``cto`` but not ``Assistant to
    #: the CTO``. That is what makes these a title *list* rather than the keyword
    #: rules below, and the two are independent — an entry here can be a full
    #: title that the keyword rules would have matched only by accident.
    include_titles: CommaSeparated = Field(default_factory=list)
    #: Job titles to drop, matched the same exact way.
    exclude_titles: CommaSeparated = Field(default_factory=list)
    #: Case-insensitive substrings that must / must not appear in the job title.
    include_title_keywords: CommaSeparated = Field(default_factory=list)
    exclude_title_keywords: CommaSeparated = Field(default_factory=list)
    #: Dotted paths that must carry a value, e.g. ``company.name`` or
    #: ``person.linkedin_url``. Resolved against the lead model, so any field is
    #: addressable without a code change; an unknown path is a configuration
    #: error rather than a rule that silently never fires.
    required_fields: CommaSeparated = Field(default_factory=list)

    # --- Contactability --------------------------------------------------- #
    require_email: bool = False
    require_company_domain: bool = False
    require_linkedin: bool = False
    #: Drop leads whose address is on a consumer mailbox provider (gmail, ...).
    exclude_free_email: bool = False
    #: Drop shared inboxes (info@, sales@) that rarely reach a decision maker.
    exclude_role_based_email: bool = False
    role_based_email_prefixes: CommaSeparated = Field(
        default_factory=lambda: [
            "abuse",
            "admin",
            "billing",
            "careers",
            "contact",
            "enquiries",
            "enquiry",
            "feedback",
            "hello",
            "help",
            "hr",
            "info",
            "jobs",
            "legal",
            "marketing",
            "media",
            "noreply",
            "no-reply",
            "office",
            "postmaster",
            "press",
            "privacy",
            "recruiting",
            "recruitment",
            "sales",
            "security",
            "support",
            "team",
            "webmaster",
        ]
    )
    exclude_email_domains: CommaSeparated = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_ranges(self) -> FilterSettings:
        if (
            self.min_employees is not None
            and self.max_employees is not None
            and self.min_employees > self.max_employees
        ):
            raise ValueError(
                f"min_employees ({self.min_employees}) is greater than "
                f"max_employees ({self.max_employees})"
            )
        return self

    @field_validator("required_fields")
    @classmethod
    def _check_required_paths(cls, value: list[str]) -> list[str]:
        """Reject a required-field path the lead model does not have.

        A typo here would otherwise produce a rule that never fires, and a
        filter that silently keeps everything is far worse than one that refuses
        to start — the operator would see a clean run and trust it.
        """
        resolved: list[str] = []
        for path in value:
            normalized = path.strip()
            if not normalized:
                continue
            if not is_lead_field_path(normalized):
                raise ValueError(
                    f"required_fields entry {normalized!r} is not a field on the lead; "
                    f"use a dotted path such as 'company.name' or 'person.email'"
                )
            if normalized not in resolved:
                resolved.append(normalized)
        return resolved

    @property
    def is_active(self) -> bool:
        """True when at least one rule is set, i.e. filtering will do something."""
        return self != FilterSettings()


class ApolloSettings(BaseModel):
    """Credentials and tuning for the Apollo.io source.

    The search fields here are the *defaults* for a run. A named search profile
    (see :mod:`src.search_profiles`) overrides them, and an explicit CLI flag
    overrides both — see ``ApolloCrawler.resolve_search``.
    """

    model_config = {"extra": "forbid"}

    api_key: SecretStr | None = None
    base_url: str = "https://api.apollo.io/api/v1"
    #: Apollo caps ``per_page`` at 100; 25 keeps responses small and cheap.
    per_page: int = Field(default=25, ge=1, le=100)
    #: Safety valve on pagination: a mis-set ``--limit`` cannot spin forever.
    max_pages: int = Field(default=20, ge=1, le=500)
    #: Free-text search terms forwarded to the people search endpoint.
    person_titles: CommaSeparated = Field(default_factory=list)
    #: Apollo's own seniority vocabulary (``owner``, ``founder``, ``c_suite``,
    #: ``partner``, ``vp``, ``head``, ``director``, ``manager``, ``senior``,
    #: ``entry``, ``intern``). Deliberately plain strings rather than this
    #: project's :class:`~src.models.enums.SeniorityLevel`: Apollo separates
    #: ``head`` from ``director`` and collapsing them would quietly change the
    #: search. Validated inside the adapter, which owns that vocabulary.
    person_seniorities: CommaSeparated = Field(default_factory=list)
    #: Where the person is based.
    person_locations: CommaSeparated = Field(default_factory=list)
    #: Where the company is headquartered.
    organization_locations: CommaSeparated = Field(default_factory=list)
    organization_industries: CommaSeparated = Field(default_factory=list)
    #: Headcount bands as ``min,max`` pairs, e.g. ``"201,500"``.
    employee_count_ranges: CommaSeparated = Field(default_factory=list)
    #: Let Apollo widen each title to near-equivalents rather than exact matches.
    include_similar_titles: bool = True
    q_keywords: str | None = None

    @field_validator("employee_count_ranges")
    @classmethod
    def _validate_employee_ranges(cls, value: list[str]) -> list[str]:
        """Normalize ``min,max`` bands.

        Write them with a hyphen (``201-500``): a comma inside a
        comma-separated environment value would be read as the next list item.
        The comma form only survives inside a JSON array, where each element's
        commas are unambiguous.
        """
        bands: list[str] = []
        for entry in value:
            parsed = parse_employee_range(entry)
            if parsed is None:
                raise ValueError(
                    f'employee range {entry!r} must look like "201-500" '
                    f"(use a hyphen, not a comma, in a comma-separated list)"
                )
            low, high = parsed
            if low < 0:
                raise ValueError(f"employee range {entry!r} must not be negative")
            bands.append(f"{low},{high}")
        return bands

    @property
    def is_configured(self) -> bool:
        return self.api_key is not None and bool(self.api_key.get_secret_value().strip())


class WebsiteSettings(BaseModel):
    """Tuning for the ``website`` enrichment source.

    The defaults are deliberately conservative: this source reads public pages
    that belong to someone else, so it identifies itself, obeys ``robots.txt``,
    waits between requests and reads as few pages per site as it can get away
    with.
    """

    model_config = {"extra": "forbid"}

    #: Company sites to enrich. Accepts bare domains (``acme.com``) as well as
    #: full URLs; each entry produces at most one lead.
    urls: CommaSeparated = Field(default_factory=list)
    #: Pages to fetch per site, homepage included. Three covers the common
    #: homepage + about + contact shape without turning into a site crawl.
    max_pages_per_site: int = Field(default=3, ge=1, le=10)
    #: Minimum seconds between two requests to the same host.
    request_delay: float = Field(default=1.0, ge=0.0, le=60.0)
    #: Honour ``robots.txt``. Off only for a site you own.
    respect_robots: bool = True
    #: Sent verbatim as ``User-Agent``. It should say who is calling and how to
    #: ask them to stop; an anonymous crawler is a rude one.
    user_agent: str = (
        "TinasoftLeadCrawler/0.1 (+https://tinasoft.example/lead-crawler; "
        "contact: data@tinasoft.example)"
    )
    #: Follow only links on the seed host. Off would turn this into a general
    #: web crawler, which is exactly what it is not.
    same_host_only: bool = True
    #: Stop collecting public email addresses after this many per site.
    max_emails: int = Field(default=3, ge=0, le=20)


class CsvSourceSettings(BaseModel):
    """Where the ``csv`` source reads from and how the file is shaped."""

    model_config = {"extra": "forbid"}

    path: Path | None = None
    #: Field separator, or ``"auto"`` to sniff it from the file.
    delimiter: str = ","
    encoding: str = "utf-8-sig"
    #: Overrides for the built-in header aliasing, as ``target=source_column``
    #: pairs (e.g. ``job_title=position``). Applied on top of, not instead of,
    #: the built-in aliases.
    column_map: CommaSeparated = Field(default_factory=list)


class MockSettings(BaseModel):
    """Deterministic synthetic source, used for demos, smoke tests and CI."""

    model_config = {"extra": "forbid"}

    #: Fix the random seed for byte-identical output across runs. ``None``
    #: randomizes per run.
    seed: int | None = 1337
    #: Fraction of generated records deliberately left messy (shouting-case
    #: names, unformatted phones, ``N/A`` placeholders) so that a demo run
    #: exercises the normalizer and validator rather than a clean happy path.
    messy_ratio: float = Field(default=0.25, ge=0.0, le=1.0)
    #: Fraction of records given a duplicate identity, to exercise deduplication.
    duplicate_ratio: float = Field(default=0.15, ge=0.0, le=1.0)


class BrowserSettings(BaseModel):
    """Real-browser configuration, shared by every browser-backed source.

    The crawler drives a real Chromium through CloakBrowser rather than calling
    an API, so what used to be "an HTTP client's timeout" is now a process, a
    profile directory on disk and a concurrency limit that a license enforces.
    Each of those needs a knob, and none of them may be hardcoded.
    """

    model_config = {"extra": "forbid"}

    #: Which browser backend to use. ``cloakbrowser`` is the only registered
    #: provider today; the value exists so the MCP provider the architecture is
    #: being prepared for can be selected without touching crawler code.
    provider: str = "cloakbrowser"

    #: Run Chromium without a visible window. ``--login`` overrides this to
    #: ``False`` for its own bootstrap, since signing in needs a window.
    headless: bool = True

    #: Concurrent browser sessions. CloakBrowser's free tier permits **one**, and
    #: each persistent context is its own Chromium process, so with the default
    #: two browser-backed sources in one run serialize: the second waits for the
    #: first to close. Raise this only with a Pro license key. The pipeline's
    #: ``max_concurrency`` does not and cannot exceed it.
    max_sessions: int = Field(default=1, ge=1, le=16)

    #: Pro license key. ``SecretStr`` so it stays out of ``repr``, out of
    #: serialized settings, and is collected by :func:`iter_secret_values` for
    #: log redaction automatically.
    license_key: SecretStr | None = None

    #: Root under which each source gets its own persistent profile directory
    #: (``<profile_root>/<source>``). Holds live session cookies, so it is
    #: gitignored and never copied into diagnostics.
    profile_root: Path = Path("data/browser_profiles")

    #: Explicit profile directory, overriding ``profile_root`` entirely. This is
    #: how an operator points at a profile they prepared elsewhere — for example
    #: a copy of their own Chrome profile — instead of using ``--login``.
    user_data_dir: Path | None = None

    #: Default bound for a single interaction that is not otherwise bounded.
    default_timeout: float = Field(default=30.0, gt=0)
    #: Bound for a navigation. Larger than ``default_timeout`` because a
    #: JS-heavy application page legitimately takes longer than a form submit.
    nav_timeout: float = Field(default=45.0, gt=0)

    #: Where ``--debug-artifacts`` writes. Off by default: these files quote page
    #: content from a signed-in session.
    debug_dir: Path = Path("debug")
    debug_artifacts: bool = False
    #: Total bytes and file count for one run, and how many past runs to keep.
    debug_max_bytes: int = Field(default=50 * 1024 * 1024, ge=0)
    debug_max_files: int = Field(default=200, ge=1)
    debug_keep_runs: int = Field(default=5, ge=0)


class Settings(BaseSettings):
    """Root application settings."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="LEAD_",
        env_nested_delimiter="__",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Runtime ---------------------------------------------------------- #
    log_level: str = "INFO"
    log_format: LogFormat = LogFormat.CONSOLE
    #: Bounded parallelism when several sources are crawled at once.
    max_concurrency: int = Field(default=4, ge=1, le=32)
    #: Cap on retained rejection records; counters are always exact.
    max_recorded_rejections: int = Field(default=1000, ge=0)
    #: Provider slugs to crawl when ``--source`` is not given.
    default_sources: CommaSeparated = Field(default_factory=lambda: ["mock"])
    default_limit: int = Field(default=100, ge=1)
    #: Umbrella profile: one name looked up in *both* profile files (see
    #: :mod:`src.profiles`). A name defined in only one file applies in only one
    #: place, which is not an error. The two settings below name a half
    #: explicitly and therefore win over this one.
    profile: str | None = None
    #: Named search profile to apply (see :mod:`src.search_profiles`). ``None``
    #: loads no file at all, so this feature is inert until asked for.
    search_profile: str | None = None
    #: Where search profiles are read from.
    search_profiles_path: Path = Path("config/search_profiles.yaml")
    #: Named qualification profile to apply (see :mod:`src.filter_profiles`).
    #: ``None`` — the default — loads no file and leaves ``filters`` exactly as
    #: the environment configured it.
    filter_profile: str | None = None
    #: Where filter profiles are read from.
    filter_profiles_path: Path = Path("config/filters.yaml")

    # --- Output ----------------------------------------------------------- #
    output_dir: Path = Path("data/exports")
    output_formats: ExportFormatList = Field(
        default_factory=lambda: [ExportFormat.CSV, ExportFormat.JSON]
    )
    #: Leading component of generated filenames; the run's sources and a UTC
    #: timestamp follow it, so the full shape is
    #: ``<prefix>_<source>_<YYYY-MM-DD_HH-mm-ss>.<ext>``.
    output_prefix: str = "leads"
    #: Also write the run report (stats + rejection sample) next to the leads.
    write_run_report: bool = True
    #: Field separator for the CSV exporter. Some locales expect ``;``.
    output_csv_delimiter: str = ","
    #: Defaults to UTF-8 with BOM so Excel detects the encoding.
    output_encoding: str = "utf-8-sig"

    # --- Processing ------------------------------------------------------- #
    dedup_strategy: DedupStrategy = DedupStrategy.IDENTITY
    #: When collapsing duplicates, fill missing fields on the kept lead from the
    #: discarded one instead of throwing that detail away.
    dedup_merge_fields: bool = True
    #: Require a minimum completeness score. 0.0 disables the rule.
    min_completeness: float = Field(default=0.0, ge=0.0, le=1.0)
    #: Emit leads best-first (most complete first) rather than in collection
    #: order. Keeps the most actionable records when ``--max-leads`` truncates.
    sort_by_completeness: bool = True

    # --- HTTP ------------------------------------------------------------- #
    http_timeout: float = Field(default=30.0, gt=0)
    http_max_attempts: int = Field(default=3, ge=1, le=10)
    http_initial_backoff: float = Field(default=0.5, gt=0)
    http_max_backoff: float = Field(default=20.0, gt=0)

    filters: FilterSettings = Field(default_factory=FilterSettings)
    apollo: ApolloSettings = Field(default_factory=ApolloSettings)
    csv_source: CsvSourceSettings = Field(default_factory=CsvSourceSettings)
    mock: MockSettings = Field(default_factory=MockSettings)
    website: WebsiteSettings = Field(default_factory=WebsiteSettings)
    browser: BrowserSettings = Field(default_factory=BrowserSettings)

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    def ensure_output_dir(self) -> Path:
        """Create and return the output directory.

        Raises:
            ConfigError: if the directory cannot be created or written to.
        """
        return ensure_directory(self.output_dir, what="output directory")

    def active_formats(self) -> list[ExportFormat]:
        """Requested export formats, de-duplicated, order preserved."""
        seen: list[ExportFormat] = []
        for fmt in self.output_formats:
            if fmt not in seen:
                seen.append(fmt)
        return seen

    def secret_values(self) -> list[str]:
        """Every plaintext credential this configuration holds.

        Derived by walking the model for ``SecretStr`` fields rather than listed
        by hand, so a credential added to any section is covered the moment it is
        declared. Fed to :func:`~src.utils.redaction.register_secrets`, which is
        what keeps them out of the logs.
        """
        return list(iter_secret_values(self))


def _nested_sections() -> dict[str, type[BaseModel]]:
    """Root fields that are themselves a settings section, keyed by field name."""
    sections: dict[str, type[BaseModel]] = {}
    for name, field in Settings.model_fields.items():
        annotation = field.annotation
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            sections[name] = annotation
    return sections


def misnamed_env_vars() -> dict[str, str]:
    """Names this tool recognises but never reads -> the name that works.

    Public because it is a fact about the interface, not an implementation
    detail: it is exactly the set of variables that will stop a run, so anything
    standing up an environment for this tool — a test fixture, a container —
    needs to know it.

    The bare forms are curated (:data:`_BARE_ENV_ALIASES`); the
    single-underscore forms are derived from ``Settings``, so adding a nested
    setting extends the guard for free. Deriving is safe for those because every
    name it produces starts with ``LEAD_`` — whatever it matches is ours.
    """
    names = dict(_BARE_ENV_ALIASES)
    for section, model in _nested_sections().items():
        prefix = f"LEAD_{section.upper()}"
        for field_name in model.model_fields:
            names[f"{prefix}_{field_name.upper()}"] = f"{prefix}__{field_name.upper()}"
    return names


def _dotenv_keys(env_file: str | Path | None) -> set[str]:
    """Names defined in the dotenv file, or nothing if there is no usable file.

    A ``.env`` this cannot parse is left alone: reporting it is pydantic's job,
    and one confusing error is better than two.
    """
    path = Path(env_file) if env_file is not None else Path(".env")
    if not path.is_file():
        return set()
    try:
        return {str(key) for key in dotenv_values(path) if key}
    except Exception:  # a broken .env surfaces from the settings loader instead
        return set()


def check_env_names(env_file: str | Path | None = None) -> None:
    """Reject environment variables that look like settings but are not read.

    ``env_prefix="LEAD_"`` plus ``extra="ignore"`` means ``APOLLO_API_KEY=…`` is
    accepted without complaint and then ignored — the run proceeds with no key
    at all, which for a credential is the worst available outcome. A name this
    tool recognises in any other spelling is therefore an error, not a shrug.

    Only fires when the canonical name is absent. If both are set the tool is
    reading the right one, and the stray name is somebody else's business.

    Args:
        env_file: Dotenv file to inspect alongside the process environment.
            ``None`` looks at the default ``.env``.

    Raises:
        ConfigError: naming every offending variable at once, with the spelling
            to use instead. All of them, because discovering them one per run is
            a miserable way to be told.
    """
    from_file = _dotenv_keys(env_file)
    found: list[tuple[str, str, str]] = []
    for wrong, right in sorted(misnamed_env_vars().items()):
        if wrong not in os.environ and wrong not in from_file:
            continue
        if right in os.environ or right in from_file:
            continue
        where = "" if wrong in os.environ else f" in {Path(env_file or '.env')}"
        found.append((wrong, right, where))

    if not found:
        return
    details = "; ".join(
        f"{wrong}{where} is set but this tool reads {right}" for wrong, right, where in found
    )
    raise ConfigError(
        f"misnamed environment variable(s): {details}. "
        f"Rename to the name given, or unset it if it belongs to another tool."
    )


def load_settings(*, env_file: str | Path | None = None, **overrides: Any) -> Settings:
    """Build settings from the environment, applying explicit overrides last.

    Used by the CLI so that flags win over ``.env`` values while everything else
    keeps its configured default.

    Args:
        env_file: Path to a dotenv file. ``None`` uses the default ``.env``.
        **overrides: Field values (including nested models as dicts) that take
            precedence over the environment. ``None`` values are ignored so an
            unset CLI flag never clobbers an environment value.

    Raises:
        ConfigError: if a variable is spelled in a way this tool never reads, or
            if the environment is malformed (bad type, invalid enum,
            contradictory filter bounds).
    """
    check_env_names(env_file)

    cleaned = {key: value for key, value in overrides.items() if value is not None}
    if env_file is not None:
        cleaned["_env_file"] = env_file
    try:
        settings = Settings(**cleaned)
    except Exception as exc:  # pydantic ValidationError, wrapped for the CLI
        raise ConfigError(f"invalid configuration: {exc}") from exc

    # Register the credentials before anything else can log. From here on a
    # secret cannot reach a log record even by a route nobody anticipated.
    register_secrets(settings.secret_values())
    return settings
