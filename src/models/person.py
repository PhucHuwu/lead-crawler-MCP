"""The person half of a lead."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from src.models.enums import SeniorityLevel


class Person(BaseModel):
    """A normalized contact.

    Every field is optional: B2B sources routinely supply a name without an
    email, or a title without a LinkedIn URL. A lead is judged on the signals it
    *does* carry (see :mod:`src.processors.validator`), never on completeness
    alone.
    """

    model_config = ConfigDict(extra="forbid")

    first_name: str | None = None
    last_name: str | None = None
    full_name: str | None = None
    job_title: str | None = None
    #: The title exactly as the source wrote it, kept only when cleaning changed
    #: something. ``"VP Eng. at Acme"`` normalizes to ``"VP Eng."``; the clause
    #: we stripped may still be evidence about the employer, so it is preserved
    #: rather than destroyed. ``None`` means the normalized title *is* the raw
    #: one, which is the common case and keeps exports free of duplicate columns.
    job_title_raw: str | None = None
    seniority: SeniorityLevel = Field(default=SeniorityLevel.UNKNOWN)
    email: str | None = None
    phone: str | None = None
    linkedin_url: str | None = None

    @property
    def has_identity(self) -> bool:
        """True when we can address this person through at least one channel."""
        return bool(self.email or self.linkedin_url or self.full_name)
