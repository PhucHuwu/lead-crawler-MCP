"""The company half of a lead."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class Company(BaseModel):
    """A normalized organization.

    ``employee_count`` is an ``int`` rather than a band because filtering needs a
    total order. Sources that publish bands (``201-500``) are resolved to their
    lower bound at normalization time; see :func:`src.utils.numbers.parse_employee_count`.
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    domain: str | None = None
    website: str | None = None
    industry: str | None = None
    employee_count: int | None = None
    country: str | None = None
    city: str | None = None
    linkedin_url: str | None = None

    #: What the company says it does, taken from its own site or profile.
    #: Populated by enrichment sources; contact databases rarely supply it.
    description: str | None = None
    #: The page to approach this company through, when one was advertised.
    contact_url: str | None = None
    #: Platform slug -> URL (``{"linkedin": ..., "github": ...}``). A bag rather
    #: than a field per platform: the set is open-ended, and only LinkedIn is
    #: used by the pipeline (identity matching), which is why that one also has
    #: its own dedicated field above.
    social_links: dict[str, str] = Field(default_factory=dict)

    @property
    def has_identity(self) -> bool:
        """True when the record names an organization we could research."""
        return bool(self.name or self.domain)
