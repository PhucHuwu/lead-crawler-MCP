"""The company half of a lead."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


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

    @property
    def has_identity(self) -> bool:
        """True when the record names an organization we could research."""
        return bool(self.name or self.domain)
