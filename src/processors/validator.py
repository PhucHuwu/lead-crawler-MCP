"""Stage 2 — validation.

Answers one question: *is this a coherent, usable record?* Not "do we want it" —
that is the filter stage's job. Keeping the two apart means business rules can be
retuned per campaign without ever weakening structural guarantees.

Validation is purely structural and needs no configuration.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field

from src.models.lead import StandardizedLead
from src.utils.logging import get_logger
from src.utils.urls import email_domain, is_free_email_domain

logger = get_logger(__name__)

#: Deliberately permissive: the normalizer already rejected anything obviously
#: malformed, so this only catches values that slipped through.
_EMAIL_SHAPE_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

#: A person's name this short is a placeholder ("-", "x"), not a person.
MIN_NAME_LENGTH = 2

#: Names that are really a department or a placeholder.
_NON_PERSON_NAMES = frozenset(
    {
        "admin",
        "administrator",
        "contact",
        "customer service",
        "help desk",
        "info",
        "information",
        "office",
        "owner",
        "sales",
        "support",
        "team",
        "test",
        "unknown",
        "user",
    }
)


class ValidationIssue(BaseModel):
    """One reason a lead was judged incoherent."""

    model_config = ConfigDict(extra="forbid")

    rule: str
    message: str


class ValidationOutcome(BaseModel):
    """The result of validating a single lead."""

    model_config = ConfigDict(extra="forbid")

    issues: list[ValidationIssue] = Field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        return not self.issues

    def summary(self) -> str:
        """All issue messages joined, for the rejection record."""
        return "; ".join(issue.message for issue in self.issues)

    @property
    def first_rule(self) -> str | None:
        return self.issues[0].rule if self.issues else None


class LeadValidator:
    """Structural checks applied to every normalized lead."""

    def validate(self, lead: StandardizedLead) -> ValidationOutcome:
        """Run every rule and collect all failures, not just the first.

        Collecting all issues gives a far more useful rejection record than
        short-circuiting on the first problem.
        """
        issues: list[ValidationIssue] = []
        person, company = lead.person, lead.company

        # --- There must be something we can act on. ------------------------ #
        # Either half will do. A contact record is addressed through a person;
        # an enrichment record (see the `website` source) is addressed through
        # the company. Requiring both would reject every company-level lead.
        if not (person.has_identity or company.has_identity):
            issues.append(
                ValidationIssue(
                    rule="no_identity",
                    message="record has no person or company identity",
                )
            )
        elif not person.has_identity:
            # Worth knowing about, but not disqualifying: this is a company we
            # can research, not yet a person we can write to.
            logger.debug(
                "company-only lead",
                extra={"company": company.name or company.domain, "provider": lead.source.provider},
            )

        # --- ...and a company to attribute them to. ------------------------ #
        if not (company.name or company.domain or person.email):
            issues.append(
                ValidationIssue(
                    rule="no_company_signal",
                    message="record has no company name, domain or email to attribute a company",
                )
            )

        # --- Names that are not names. ------------------------------------- #
        if person.full_name:
            name = person.full_name.strip()
            if len(name) < MIN_NAME_LENGTH or name.isdigit():
                issues.append(
                    ValidationIssue(
                        rule="implausible_person_name",
                        message=f"person name {name!r} is not a usable name",
                    )
                )
            elif name.casefold() in _NON_PERSON_NAMES:
                issues.append(
                    ValidationIssue(
                        rule="non_person_name",
                        message=f"person name {name!r} is a department or placeholder",
                    )
                )
            elif company.name and name.casefold() == company.name.casefold():
                issues.append(
                    ValidationIssue(
                        rule="name_equals_company",
                        message="person name duplicates the company name",
                    )
                )

        # --- Defensive email re-check. ------------------------------------- #
        if person.email and not _EMAIL_SHAPE_RE.match(person.email):
            issues.append(
                ValidationIssue(
                    rule="invalid_email",
                    message=f"email {person.email!r} is not a valid address",
                )
            )

        # --- Contact details that cannot belong to this company. ----------- #
        # A corporate address whose domain contradicts the company's own domain
        # usually means two companies were merged into one record upstream.
        # Consumer mailboxes are exempt (a personal address is normal and says
        # nothing about the employer), as are subdomains in either direction
        # (mail.acme.com vs acme.com).
        if person.email and company.domain:
            domain = email_domain(person.email)
            if (
                domain
                and not is_free_email_domain(person.email)
                and not _domains_align(domain, company.domain)
            ):
                issues.append(
                    ValidationIssue(
                        rule="email_company_mismatch",
                        message=(
                            f"email domain {domain!r} does not match "
                            f"company domain {company.domain!r}"
                        ),
                    )
                )

        return ValidationOutcome(issues=issues)


def _domains_align(email_host: str, company_host: str) -> bool:
    """Whether two hosts plausibly belong to the same organization.

    True when they are equal or one is a subdomain of the other.
    """
    return (
        email_host == company_host
        or email_host.endswith(f".{company_host}")
        or company_host.endswith(f".{email_host}")
    )
