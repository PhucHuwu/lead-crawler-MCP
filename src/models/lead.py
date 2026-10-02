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
        """
        keys: dict[str, str] = {}
        person, company = self.person, self.company

        if person.email:
            keys["email"] = person.email
        if person.linkedin_url:
            keys["linkedin"] = person.linkedin_url

        name_slug = slugify_identity(person.full_name)
        last_slug = slugify_identity(person.last_name)

        if person.phone and name_slug:
            keys["phone_name"] = f"{person.phone}|{name_slug}"
        if name_slug and company.domain:
            keys["name_domain"] = f"{name_slug}@{company.domain}"
        if name_slug and company.name:
            keys["name_company"] = f"{name_slug}@{slugify_identity(company.name)}"
        if last_slug and company.domain and person.first_name:
            keys["lastname_domain"] = f"{last_slug}@{company.domain}"
        return keys

    def stable_id(self) -> str:
        """Deterministic id derived from the strongest available identity.

        Stable across runs so that re-crawling the same lead yields the same id,
        which is what makes later-phase dedup against a CRM possible.
        """
        keys = self.identity_keys()
        basis = next(iter(keys.values()), None) or "|".join(
            filter(None, (self.person.full_name, self.company.name, self.source.external_id))
        )
        digest = hashlib.sha256(f"{self.source.provider}:{basis}".encode()).hexdigest()
        return digest[:16]

    def flatten(self) -> dict[str, Any]:
        """Flat, column-oriented view used by the CSV exporter.

        Nested models become ``snake_case`` columns with a ``company_`` prefix so
        the output is readable in a spreadsheet without further processing.
        """
        return {
            "lead_id": self.lead_id,
            "first_name": self.person.first_name,
            "last_name": self.person.last_name,
            "full_name": self.person.full_name,
            "job_title": self.person.job_title,
            "seniority": self.person.seniority.value,
            "email": self.person.email,
            "phone": self.person.phone,
            "linkedin_url": self.person.linkedin_url,
            "company_name": self.company.name,
            "company_domain": self.company.domain,
            "company_website": self.company.website,
            "company_industry": self.company.industry,
            "company_employee_count": self.company.employee_count,
            "company_country": self.company.country,
            "company_city": self.company.city,
            "company_linkedin_url": self.company.linkedin_url,
            "source_provider": self.source.provider,
            "source_external_id": self.source.external_id,
            "source_url": self.source.source_url,
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
