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

List-valued settings accept either comma-separated values (``US,CA``) or a JSON
array (``["US","CA"]``); the comma form is what humans actually type in a shell.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from src.models.enums import DedupStrategy, ExportFormat, LogFormat, SeniorityLevel
from src.utils.errors import ConfigError


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
    #: Case-insensitive substrings that must / must not appear in the job title.
    include_title_keywords: CommaSeparated = Field(default_factory=list)
    exclude_title_keywords: CommaSeparated = Field(default_factory=list)

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

    @property
    def is_active(self) -> bool:
        """True when at least one rule is set, i.e. filtering will do something."""
        return self != FilterSettings()


class ApolloSettings(BaseModel):
    """Credentials and tuning for the Apollo.io source."""

    model_config = {"extra": "forbid"}

    api_key: SecretStr | None = None
    base_url: str = "https://api.apollo.io/api/v1"
    #: Apollo caps ``per_page`` at 100; 25 keeps responses small and cheap.
    per_page: int = Field(default=25, ge=1, le=100)
    #: Free-text search terms forwarded to the people search endpoint.
    person_titles: CommaSeparated = Field(default_factory=list)
    person_locations: CommaSeparated = Field(default_factory=list)
    organization_locations: CommaSeparated = Field(default_factory=list)
    q_keywords: str | None = None

    @property
    def is_configured(self) -> bool:
        return self.api_key is not None and bool(self.api_key.get_secret_value().strip())


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

    # --- Output ----------------------------------------------------------- #
    output_dir: Path = Path("data/exports")
    output_formats: ExportFormatList = Field(
        default_factory=lambda: [ExportFormat.CSV, ExportFormat.JSON]
    )
    #: Prefix for generated filenames; the run timestamp is appended.
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

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    def ensure_output_dir(self) -> Path:
        """Create and return the output directory.

        Raises:
            ConfigError: if the directory cannot be created or written to.
        """
        try:
            self.output_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ConfigError(f"cannot create output directory {self.output_dir}: {exc}") from exc
        if not self.output_dir.is_dir():
            raise ConfigError(f"output path {self.output_dir} exists but is not a directory")
        return self.output_dir

    def active_formats(self) -> list[ExportFormat]:
        """Requested export formats, de-duplicated, order preserved."""
        seen: list[ExportFormat] = []
        for fmt in self.output_formats:
            if fmt not in seen:
                seen.append(fmt)
        return seen


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
        ConfigError: if the environment is malformed (bad type, invalid enum,
            contradictory filter bounds).
    """
    cleaned = {key: value for key, value in overrides.items() if value is not None}
    if env_file is not None:
        cleaned["_env_file"] = env_file
    try:
        return Settings(**cleaned)
    except Exception as exc:  # pydantic ValidationError, wrapped for the CLI
        raise ConfigError(f"invalid configuration: {exc}") from exc
