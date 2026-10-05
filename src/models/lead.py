"""The two lead representations that flow through the pipeline.

``RawLead`` is what a crawler adapter emits: a flat, permissive, *uncleaned*
record that mirrors the standardized schema but makes no promises about content.
``StandardizedLead`` is what comes out the far end of normalization: nested,
cleaned, scored and addressable by a stable id.

Keeping these as two distinct types is what decouples the pipeline from the
sources. A new adapter only has to produce ``RawLead``; normalizing, validating,
filtering, deduplicating and exporting never learn that it exists.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

from src.models.company import Company
from src.models.enums import SeniorityLevel
from src.models.person import Person
from src.utils.time import to_iso, utcnow

_SLUG_RE = re.compile(r"[^a-z0-9]+")

#: Relative weight of each signal when scoring how actionable a lead is. Email
#: and company domain dominate because outreach and qualification both depend on
#: them; country is a weak signal but still worth a little.
_COMPLETENESS_WEIGHTS: dict[str, float] = {
    "email": 0.25,
    "full_name": 0.15,
    "company_name": 0.15,
    "company_domain": 0.15,
    "job_title": 0.10,
    "linkedin_url": 0.10,
    "company_country": 0.05,
    "phone": 0.05,
}


def slugify_identity(value: str | None) -> str:
    """Reduce a name to a comparable slug (``Ada O'Neill`` -> ``adaoneill``)."""
    if not value:
        return ""
    return _SLUG_RE.sub("", value.casefold())


def format_social_links(links: dict[str, str]) -> str | None:
    """Render social links as ``github=https://…; linkedin=https://…``.

    A flat string because the destination is a spreadsheet column: JSON would
    be more faithful but unreadable in Excel, and the set of platforms is open
    so there is no fixed number of columns to give them. Sorted by platform so
    two exports of the same lead are byte-identical.
    """
    if not links:
        return None
    return "; ".join(f"{platform}={url}" for platform, url in sorted(links.items()))


def is_empty_value(value: object) -> bool:
    """Whether a field carries no information.

    Used for two different jobs that need the same answer: deciding if a
    duplicate may fill a field (see :func:`~src.processors.deduplicator.merge_leads`)
    and deciding if a required field was supplied at all. Both are asking "is
    there anything here?", so both must agree on what counts as nothing.

    Note that ``0`` and ``False`` are *present* — a company with zero employees
    is a fact, not a gap — while ``UNKNOWN`` seniority is not, because that is
    the vocabulary's way of writing "we do not know".
    """
    # Enums must be checked before `str`: SeniorityLevel is a StrEnum, so its
    # members satisfy `isinstance(value, str)` and would otherwise be judged on
    # their text ("unknown" is non-empty, and so looks like real data).
    if isinstance(value, SeniorityLevel):
        return value is SeniorityLevel.UNKNOWN
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple, set, dict)):
        return not value
    return False


def is_lead_field_path(path: str) -> bool:
    """Whether ``path`` names a real field on :class:`StandardizedLead`.

    Accepts dotted paths (``company.name``) and walks the nested models, so a
    field added to the schema is addressable from configuration the moment it
    exists rather than needing to be registered anywhere.
    """
    parts = [part for part in path.split(".") if part]
    if not parts:
        return False

    model: type[BaseModel] = StandardizedLead
    for index, part in enumerate(parts):
        field = model.model_fields.get(part)
        if field is None:
            return False
        if index == len(parts) - 1:
            return True
        annotation = field.annotation
        if not (isinstance(annotation, type) and issubclass(annotation, BaseModel)):
            return False
        model = annotation
    return False


def resolve_field_path(lead: StandardizedLead, path: str) -> object:
    """Read a dotted path off a lead, returning ``None`` if any step is absent."""
    value: object = lead
    for part in path.split("."):
        value = getattr(value, part, None)
        if value is None:
            return None
    return value


class LeadSource(BaseModel):
    """Provenance for a lead — where it came from and when we saw it.

    Retained on every exported record so downstream phases can attribute,
    re-crawl or expire leads without guessing.
    """

    model_config = ConfigDict(extra="forbid")

    provider: str
    external_id: str | None = None
    source_url: str | None = None
    collected_at: datetime = Field(default_factory=utcnow)
    #: Every provider that contributed to this record, primary first. Usually one
    #: entry; after deduplication merges two sources describing the same person
    #: it holds both, so the export can say *which* sources agreed rather than
    #: silently crediting whichever happened to be crawled first. Populated by
    #: :func:`~src.processors.deduplicator.merge_leads`; ``provider`` above stays
    #: the primary and is always ``sources[0]``.
    sources: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _seed_sources(self) -> Self:
        """Ensure ``sources`` always names at least the primary provider.

        Kept as a validator rather than a default so adapters cannot forget it —
        a lead that named a provider but listed no sources would export an empty
        provenance column for no reason.
        """
        if self.provider and self.provider not in self.sources:
            self.sources.insert(0, self.provider)
        return self


class NormalizationIssue(BaseModel):
    """A supplied value that could not be cleaned.

    Normalization is lossy by design: an unusable domain becomes ``None``. That
    is the right *repair*, but on its own it is indistinguishable from a field
    the source never sent — and "the source sent garbage" is exactly the thing
    an operator needs to see. Recording the failure here lets
    :mod:`src.processors.validator` report it as a rule violation instead of
    letting the value vanish.
    """

    model_config = ConfigDict(extra="forbid")

    #: Rule name the validator will raise, e.g. ``invalid_domain``.
    rule: str
    #: The :class:`RawLead` field the value came from.
    field: str
    #: The offending input, truncated. Kept verbatim so the operator can judge
    #: whether the normalizer was wrong or the source was.
    value: str


class RawLead(BaseModel):
    """Unprocessed output of a crawler adapter.

    Deliberately flat and forgiving. ``company_employee_count`` accepts
    ``int | str`` because sources really do send ``500``, ``"500"`` and
    ``"201-500"``. ``raw`` preserves the untouched upstream payload so a
    mapping bug is always recoverable from the crawl output.
    """

    model_config = ConfigDict(extra="forbid")

    provider: str
    external_id: str | None = None
    source_url: str | None = None
    collected_at: datetime = Field(default_factory=utcnow)

    # Person
    first_name: str | None = None
    last_name: str | None = None
    full_name: str | None = None
    job_title: str | None = None
    seniority: SeniorityLevel | None = None
    email: str | None = None
    phone: str | None = None
    linkedin_url: str | None = None

    # Company
    company_name: str | None = None
    company_domain: str | None = None
    company_website: str | None = None
    company_industry: str | None = None
    company_employee_count: int | str | None = None
    company_country: str | None = None
    company_city: str | None = None
    company_linkedin_url: str | None = None
    #: Enrichment-only fields, supplied by sources that read company sites.
    company_description: str | None = None
    company_contact_url: str | None = None
    #: Platform slug -> URL, e.g. ``{"linkedin": "https://...", "github": ...}``.
    company_social_links: dict[str, str] = Field(default_factory=dict)

    #: Untouched upstream record, preserved for debugging and re-processing.
    raw: dict[str, Any] = Field(default_factory=dict)

    def label(self) -> str:
        """Short human-readable identifier used in logs and rejection records."""
        for candidate in (self.email, self.full_name, self.linkedin_url, self.external_id):
            if candidate:
                return str(candidate)
        return "<unidentified>"


class StandardizedLead(BaseModel):
    """A cleaned, validated, scored lead — the pipeline's unit of output."""

    model_config = ConfigDict(extra="forbid")

    #: Stable hash of the lead's strongest identity. Left empty at construction
    #: and filled in by ``_ensure_lead_id`` below.
    lead_id: str = ""
    person: Person = Field(default_factory=Person)
    company: Company = Field(default_factory=Company)
    source: LeadSource
    #: Values the normalizer had to discard, surfaced so the validator can report
    #: them. Deliberately excluded from ``flatten()``, from ``completeness`` and
    #: from ``identity_keys()`` — it is a quality note about the record, not part
    #: of the record, and it must never affect matching or ranking.
    normalization_issues: list[NormalizationIssue] = Field(default_factory=list)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def completeness(self) -> float:
        """Fraction of weighted key signals present, in ``[0.0, 1.0]``.

        Derived rather than stored so it can never drift from the data it
        describes. Used to rank leads and to break ties when merging duplicates.
        """
        present = {
            "email": self.person.email,
            "full_name": self.person.full_name,
            "job_title": self.person.job_title,
            "linkedin_url": self.person.linkedin_url,
            "phone": self.person.phone,
            "company_name": self.company.name,
            "company_domain": self.company.domain,
            "company_country": self.company.country,
        }
        score = sum(weight for key, weight in _COMPLETENESS_WEIGHTS.items() if present.get(key))
        return round(min(score, 1.0), 4)

    def identity_keys(self) -> dict[str, str]:
        """Ordered identity ladder, strongest key first.

        The deduplicator walks this in order and collapses on the first key the
        active strategy permits. Keys are only emitted when the underlying data
        is actually present, so a sparse lead simply offers fewer ways to match.

        The ladder is deliberately *not* ordered by how much it narrows a person
        down but by how certain a match is:

        1. ``source_id`` — the source itself says these are one record.
        2. ``email`` — a normalized address is unique to a person.
        3. ``linkedin`` — a profile URL is unique to a person.
        4. ``phone_name`` — a phone number is near-unique, plus a name to confirm.
        5. ``name_domain`` — a name at a company domain.
        6. ``name_company`` — a name at a company *name*, with no domain to anchor it.

        Nothing below ``linkedin`` is a claim of uniqueness, and no key here is a
        bare name: two different people who share a name (or, worse, a surname at
        one employer) must never collapse into one lead.
        """
        keys: dict[str, str] = {}
        person, company = self.person, self.company

        # Scoped by provider: two sources can and do use overlapping external ids.
        if self.source.external_id:
            keys["source_id"] = f"{self.source.provider}:{self.source.external_id}"
        if person.email:
            keys["email"] = person.email
        if person.linkedin_url:
            keys["linkedin"] = person.linkedin_url

        name_slug = slugify_identity(person.full_name)

        if person.phone and name_slug:
            keys["phone_name"] = f"{person.phone}|{name_slug}"
        if name_slug and company.domain:
            keys["name_domain"] = f"{name_slug}@{company.domain}"
        if name_slug and company.name:
            keys["name_company"] = f"{name_slug}@{slugify_identity(company.name)}"
        return keys

    def stable_id(self) -> str:
        """Deterministic id derived from the strongest available identity.

        Stable across runs so that re-crawling the same lead yields the same id,
        which is what makes later-phase dedup against a CRM possible.

        ``source_id`` is deliberately *excluded* from the basis even though it
        heads :meth:`identity_keys`. The two answer different questions: a
        source id identifies a *record at a source*, whereas this id identifies a
        *person*, and the same person found through two sources has two different
        source ids. Keying on it would make one person's id depend on which
        source happened to be crawled first.
        """
        keys = {name: value for name, value in self.identity_keys().items() if name != "source_id"}
        basis = next(iter(keys.values()), None) or "|".join(
            filter(None, (self.person.full_name, self.company.name, self.source.external_id))
        )
        digest = hashlib.sha256(f"{self.source.provider}:{basis}".encode()).hexdigest()
        return digest[:16]

    def flatten(self) -> dict[str, Any]:
        """Flat, column-oriented view used by the CSV exporter.

        Every field of a nested model carries that model's name as its prefix
        (``person_``, ``company_``, ``source_``) so a column says where its value
        came from without the reader having to consult the schema — the columns
        that belong to the source are the ones that would otherwise collide
        (``name``, ``url``, ``linkedin_url``). ``lead_id`` and ``completeness``
        are the two fields of the lead itself, so they stay unprefixed.
        """
        return {
            "lead_id": self.lead_id,
            "person_first_name": self.person.first_name,
            "person_last_name": self.person.last_name,
            "person_full_name": self.person.full_name,
            "person_job_title": self.person.job_title,
            "person_job_title_raw": self.person.job_title_raw,
            "person_seniority": self.person.seniority.value,
            "person_email": self.person.email,
            "person_phone": self.person.phone,
            "person_linkedin_url": self.person.linkedin_url,
            "company_name": self.company.name,
            "company_domain": self.company.domain,
            "company_website": self.company.website,
            "company_industry": self.company.industry,
            "company_employee_count": self.company.employee_count,
            "company_country": self.company.country,
            "company_city": self.company.city,
            "company_linkedin_url": self.company.linkedin_url,
            "company_description": self.company.description,
            "company_contact_url": self.company.contact_url,
            "company_social_links": format_social_links(self.company.social_links),
            "source_provider": self.source.provider,
            "source_external_id": self.source.external_id,
            "source_url": self.source.source_url,
            # Semicolon-joined for the same reason social links are: a cell holds
            # one string, and a merged lead legitimately has more than one source.
            "sources": "; ".join(self.source.sources) if self.source.sources else None,
            "collected_at": to_iso(self.source.collected_at),
            "completeness": self.completeness,
        }

    @model_validator(mode="after")
    def _ensure_lead_id(self) -> Self:
        """Derive ``lead_id`` whenever the caller did not supply one.

        Keeping this on the model means a lead can never exist without a stable
        id, regardless of which code path constructed it.
        """
        if not self.lead_id:
            object.__setattr__(self, "lead_id", self.stable_id())
        return self
