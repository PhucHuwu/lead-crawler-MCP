"""Stage 1 — normalization.

Turns a permissive :class:`RawLead` into a clean :class:`StandardizedLead`. This
is the only place that knows how to repair messy values, so every source gets
identical treatment no matter how dirty its data was.

The stage never raises for *quality* reasons — a lead with almost no data still
normalizes, and :mod:`src.processors.validator` decides whether it is usable.
Normalization only fails when a record cannot be interpreted at all.
"""

from __future__ import annotations

from src.models.company import Company
from src.models.enums import SeniorityLevel
from src.models.lead import LeadSource, NormalizationIssue, RawLead, StandardizedLead
from src.models.person import Person
from src.processors.geo import normalize_country
from src.processors.seniority import infer_seniority
from src.utils.logging import get_logger
from src.utils.numbers import parse_employee_count
from src.utils.text import (
    clean_text,
    join_full_name,
    normalize_email,
    normalize_phone,
    split_full_name,
    strip_title_suffix,
    titlecase_name,
    truncate,
)
from src.utils.time import ensure_utc
from src.utils.urls import (
    is_free_email_domain,
    normalize_domain,
    normalize_linkedin_url,
    normalize_page_url,
    normalize_social_links,
    normalize_website,
)

logger = get_logger(__name__)

#: Headcounts above this are data-entry errors (population-scale "companies"),
#: not real employers. Dropped rather than allowed to distort size filters.
MAX_PLAUSIBLE_EMPLOYEES = 2_000_000

#: Ceiling on a company description. Meta descriptions and JSON-LD are usually
#: one or two sentences; anything far longer is boilerplate that would bloat
#: every export row without adding a usable signal.
MAX_DESCRIPTION_CHARS = 600

#: Cap on the offending value echoed into a normalization issue. Long enough to
#: recognise the input, short enough that a pasted blob cannot bloat a report.
MAX_ISSUE_VALUE_CHARS = 200


class Normalizer:
    """Repairs and reshapes raw leads into the standardized schema."""

    def normalize(self, raw: RawLead) -> StandardizedLead:
        """Normalize one raw lead.

        Every field is best-effort: anything that cannot be cleaned becomes
        ``None`` instead of a corrupted value.

        Raises:
            NormalizationError: only if the record cannot be interpreted at all.
        """
        person = self._normalize_person(raw)
        company = self._normalize_company(raw, person)
        issues = _collect_issues(raw)

        return StandardizedLead(
            person=person,
            company=company,
            source=LeadSource(
                provider=clean_text(raw.provider) or "unknown",
                external_id=clean_text(raw.external_id),
                source_url=clean_text(raw.source_url),
                collected_at=ensure_utc(raw.collected_at) or raw.collected_at,
            ),
            normalization_issues=issues,
        )

    # ------------------------------------------------------------------ #
    # Person
    # ------------------------------------------------------------------ #
    def _normalize_person(self, raw: RawLead) -> Person:
        first = titlecase_name(raw.first_name)
        last = titlecase_name(raw.last_name)
        full = titlecase_name(raw.full_name)

        # Reconcile full name against its components. A supplied full name wins
        # (it may carry a middle name we would otherwise lose); components are
        # derived from it only where they are missing.
        if full is None:
            full = join_full_name(first, last)
        elif first is None or last is None:
            derived_first, derived_last = split_full_name(full)
            first = first or derived_first
            last = last or derived_last

        # Cleaned the same way as the normalized title, so the two differ only
        # by the normalization itself and not by whitespace noise. Kept only
        # when it says something the normalized title does not.
        raw_title = clean_text(raw.job_title)
        job_title = strip_title_suffix(raw_title)
        if raw_title == job_title:
            raw_title = None

        # An explicit seniority from the source is trusted; otherwise infer it
        # from the title rather than leaving the field empty.
        seniority = SeniorityLevel.coerce(raw.seniority)
        if seniority is SeniorityLevel.UNKNOWN:
            seniority = infer_seniority(job_title)

        return Person(
            first_name=first,
            last_name=last,
            full_name=full,
            job_title=job_title,
            job_title_raw=raw_title,
            seniority=seniority,
            email=normalize_email(raw.email),
            phone=normalize_phone(raw.phone),
            # Scoped to `person` so a company page URL never lands here.
            linkedin_url=normalize_linkedin_url(raw.linkedin_url, kind="person"),
        )

    # ------------------------------------------------------------------ #
    # Company
    # ------------------------------------------------------------------ #
    def _normalize_company(self, raw: RawLead, person: Person) -> Company:
        domain = normalize_domain(raw.company_domain) or normalize_domain(raw.company_website)

        # Fall back to the email domain: a work address is strong evidence of the
        # employer's domain. Consumer mailboxes are excluded because they say
        # nothing about the company.
        if domain is None and person.email and not is_free_email_domain(person.email):
            domain = normalize_domain(person.email)

        website = normalize_website(raw.company_website) or normalize_website(domain)

        employee_count = parse_employee_count(raw.company_employee_count)
        if employee_count is not None and employee_count > MAX_PLAUSIBLE_EMPLOYEES:
            logger.debug(
                "discarding implausible employee count",
                extra={"value": employee_count, "provider": raw.provider},
            )
            employee_count = None

        social_links = normalize_social_links(raw.company_social_links)
        linkedin_url = normalize_linkedin_url(raw.company_linkedin_url, kind="company")
        if linkedin_url is None:
            # A profile page is the same fact however the source labelled it.
            linkedin_url = social_links.get("linkedin")

        return Company(
            name=titlecase_name(raw.company_name),
            domain=domain,
            website=website,
            industry=clean_text(raw.company_industry),
            employee_count=employee_count,
            country=normalize_country(raw.company_country),
            city=titlecase_name(raw.company_city),
            linkedin_url=linkedin_url,
            description=_truncate_description(raw.company_description),
            contact_url=normalize_page_url(raw.company_contact_url),
            social_links=social_links,
        )


def _collect_issues(raw: RawLead) -> list[NormalizationIssue]:
    """Record supplied values that did not survive normalization.

    A field the source never sent is not a problem — it is simply absent. This
    reports only fields that *were* sent and could not be cleaned, which is the
    difference between "we have no domain for this company" and "the source gave
    us a domain we could not read". Each field is re-checked against its own
    normalizer rather than against the finished lead, because the company
    fallbacks mean an unusable ``company_domain`` can still be papered over by a
    usable ``company_website`` — the value would be lost without a trace.
    """
    issues: list[NormalizationIssue] = []
    _note(issues, "email", "invalid_email", raw.email, normalize_email(raw.email))
    _note(
        issues,
        "linkedin_url",
        "malformed_url",
        raw.linkedin_url,
        # Kind "any": a company URL in the person field is a mislabelled value,
        # not a malformed one, and reporting it as malformed would be a lie.
        normalize_linkedin_url(raw.linkedin_url),
    )
    _note(
        issues,
        "company_domain",
        "invalid_domain",
        raw.company_domain,
        normalize_domain(raw.company_domain),
    )
    _note(
        issues,
        "company_website",
        "malformed_url",
        raw.company_website,
        normalize_domain(raw.company_website),
    )
    _note(
        issues,
        "company_contact_url",
        "malformed_url",
        raw.company_contact_url,
        normalize_page_url(raw.company_contact_url),
    )
    return issues


def _note(
    issues: list[NormalizationIssue],
    field: str,
    rule: str,
    supplied: object,
    normalized: object,
) -> None:
    """Append an issue when a value was supplied but produced nothing."""
    if normalized is not None:
        return
    text = clean_text(supplied)
    if text is None:
        # Never supplied, or a recognised "no value" marker such as "N/A".
        # Absent is not malformed, and reporting it would drown the real issues.
        return
    issues.append(
        NormalizationIssue(rule=rule, field=field, value=truncate(text, MAX_ISSUE_VALUE_CHARS))
    )


def _truncate_description(value: object) -> str | None:
    """Clean a company description and cap its length."""
    text = clean_text(value)
    if text is None:
        return None
    if len(text) <= MAX_DESCRIPTION_CHARS:
        return text
    # Cut on a word boundary so the truncation does not read as a typo.
    return text[:MAX_DESCRIPTION_CHARS].rsplit(" ", 1)[0].rstrip(",;:") + "…"


__all__ = ["MAX_PLAUSIBLE_EMPLOYEES", "Normalizer"]
