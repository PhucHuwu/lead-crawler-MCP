"""Stage 2 — validation.

Answers one question: *is this a coherent, usable record?* Not "do we want it" —
that is the filter stage's job. Keeping the two apart means business rules can be
retuned per campaign without ever weakening structural guarantees.

Validation is purely structural and needs no configuration.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field

from src.models.enums import ValidationSeverity
from src.models.lead import StandardizedLead
from src.utils.logging import get_logger
from src.utils.urls import email_domain, is_free_email_domain

logger = get_logger(__name__)

#: How to phrase a discarded value, per rule the normalizer can raise. A rule
#: with no entry still gets reported, using the fallback below — losing the
#: finding would be worse than phrasing it blandly.
_ISSUE_MESSAGES: dict[str, str] = {
    "invalid_domain": "{field} {value!r} is not a usable domain",
    "malformed_url": "{field} {value!r} is not a usable URL",
    "invalid_email": "{field} {value!r} is not a usable email address",
}
_DEFAULT_ISSUE_MESSAGE = "{field} {value!r} could not be normalized"

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
    #: ``error`` drops the record; ``warning`` keeps it and reports the finding.
    severity: ValidationSeverity = ValidationSeverity.ERROR


class ValidationOutcome(BaseModel):
    """The result of validating a single lead."""

    model_config = ConfigDict(extra="forbid")

    issues: list[ValidationIssue] = Field(default_factory=list)

    @property
    def errors(self) -> list[ValidationIssue]:
        return [issue for issue in self.issues if issue.severity is ValidationSeverity.ERROR]

    @property
    def warnings(self) -> list[ValidationIssue]:
        return [issue for issue in self.issues if issue.severity is ValidationSeverity.WARNING]

    @property
    def is_valid(self) -> bool:
        """Whether the record may continue down the pipeline.

        Only errors disqualify it. Warnings are findings *about* a usable record.
        """
        return not self.errors

    @property
    def has_warnings(self) -> bool:
        return bool(self.warnings)

    @property
    def rules(self) -> tuple[str, ...]:
        """Distinct rule names, in first-seen order.

        Deduplicated because statistics count *records* affected by a rule: a
        lead with two unreadable URLs is one lead with a URL problem, not two.
        """
        seen: dict[str, None] = {}
        for issue in self.issues:
            seen.setdefault(issue.rule, None)
        return tuple(seen)

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

        # --- Values the source sent that could not be read. ------------- #
        # Reported, never disqualifying: the record is still usable, and these
        # are the only trace that the source sent something we threw away.
        issues.extend(_issue_warnings(lead))

        return ValidationOutcome(issues=issues)


def _issue_warnings(lead: StandardizedLead) -> list[ValidationIssue]:
    """Turn the normalizer's discarded values into reportable findings."""
    return [
        ValidationIssue(
            rule=issue.rule,
            severity=ValidationSeverity.WARNING,
            message=_ISSUE_MESSAGES.get(issue.rule, _DEFAULT_ISSUE_MESSAGE).format(
                field=issue.field, value=issue.value
            ),
        )
        for issue in lead.normalization_issues
    ]


def _domains_align(email_host: str, company_host: str) -> bool:
    """Whether two hosts plausibly belong to the same organization.

    True when they are equal or one is a subdomain of the other.
    """
    return (
        email_host == company_host
        or email_host.endswith(f".{company_host}")
        or company_host.endswith(f".{email_host}")
    )
