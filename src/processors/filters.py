"""Stage 3 — filtering.

Applies the qualification rules from :class:`~src.config.FilterSettings` and
reports *which* rule rejected each lead, so a run's output is explainable rather
than just smaller.

Rules are evaluated in a fixed order and the first failure wins, which makes the
attribution in ``stats.per_filter_reason`` deterministic across runs.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from src.config import FilterSettings
from src.models.lead import StandardizedLead
from src.processors.geo import country_matches
from src.utils.urls import email_domain, is_free_email_domain


class FilterDecision(BaseModel):
    """Whether a lead passed, and if not, which rule removed it."""

    model_config = ConfigDict(extra="forbid")

    passed: bool
    rule: str | None = None
    detail: str | None = None

    @classmethod
    def keep(cls) -> FilterDecision:
        return cls(passed=True)

    @classmethod
    def drop(cls, rule: str, detail: str) -> FilterDecision:
        return cls(passed=False, rule=rule, detail=detail)


class LeadFilter:
    """Evaluates qualification criteria against a normalized lead."""

    def __init__(self, criteria: FilterSettings, *, min_completeness: float = 0.0) -> None:
        self.criteria = criteria
        self.min_completeness = min_completeness
        # Pre-compute casefolded lookup sets once per run rather than per lead.
        self._excluded_domains = {
            d.strip().casefold() for d in criteria.exclude_domains if d.strip()
        }
        self._excluded_email_domains = {
            d.strip().casefold() for d in criteria.exclude_email_domains if d.strip()
        }
        self._excluded_industries = [
            i.strip().casefold() for i in criteria.exclude_industries if i.strip()
        ]
        self._included_industries = [
            i.strip().casefold() for i in criteria.include_industries if i.strip()
        ]
        self._excluded_title_keywords = [
            k.strip().casefold() for k in criteria.exclude_title_keywords if k.strip()
        ]
        self._included_title_keywords = [
            k.strip().casefold() for k in criteria.include_title_keywords if k.strip()
        ]
        self._role_prefixes = {
            p.strip().casefold().rstrip("@")
            for p in criteria.role_based_email_prefixes
            if p.strip()
        }

    def evaluate(self, lead: StandardizedLead) -> FilterDecision:
        """Run the rule chain, returning on the first rejection."""
        for check in (
            self._check_completeness,
            self._check_excluded_domain,
            self._check_excluded_email_domain,
            self._check_required_fields,
            self._check_email_quality,
            self._check_country,
            self._check_industry,
            self._check_size,
            self._check_seniority,
            self._check_title_keywords,
        ):
            decision = check(lead)
            if decision is not None:
                return decision
        return FilterDecision.keep()

    # ------------------------------------------------------------------ #
    # Rules — each returns a drop decision, or None to continue
    # ------------------------------------------------------------------ #
    def _check_completeness(self, lead: StandardizedLead) -> FilterDecision | None:
        if self.min_completeness > 0 and lead.completeness < self.min_completeness:
            return FilterDecision.drop(
                "min_completeness",
                f"completeness {lead.completeness:.2f} below minimum {self.min_completeness:.2f}",
            )
        return None

    def _check_excluded_domain(self, lead: StandardizedLead) -> FilterDecision | None:
        domain = lead.company.domain
        if domain and _matches_domain(domain, self._excluded_domains):
            return FilterDecision.drop("exclude_domains", f"company domain {domain!r} is excluded")
        return None

    def _check_excluded_email_domain(self, lead: StandardizedLead) -> FilterDecision | None:
        domain = email_domain(lead.person.email)
        if domain and domain in self._excluded_email_domains:
            return FilterDecision.drop(
                "exclude_email_domains", f"email domain {domain!r} is excluded"
            )
        return None

    def _check_required_fields(self, lead: StandardizedLead) -> FilterDecision | None:
        criteria = self.criteria
        if criteria.require_email and not lead.person.email:
            return FilterDecision.drop("require_email", "no email address")
        if criteria.require_company_domain and not lead.company.domain:
            return FilterDecision.drop("require_company_domain", "no company domain")
        if criteria.require_linkedin and not lead.person.linkedin_url:
            return FilterDecision.drop("require_linkedin", "no LinkedIn URL")
        return None

    def _check_email_quality(self, lead: StandardizedLead) -> FilterDecision | None:
        email = lead.person.email
        if not email:
            return None

        if self.criteria.exclude_free_email and is_free_email_domain(email):
            return FilterDecision.drop(
                "exclude_free_email", f"{email!r} is on a consumer mailbox provider"
            )

        if self.criteria.exclude_role_based_email:
            local_part = email.split("@", 1)[0].casefold()
            # Strip common disambiguators so "info.emea" still reads as "info".
            base = local_part.split("+", 1)[0].split(".", 1)[0].split("-", 1)[0]
            if base in self._role_prefixes:
                return FilterDecision.drop(
                    "exclude_role_based_email", f"{email!r} is a shared/role inbox"
                )
        return None

    def _check_country(self, lead: StandardizedLead) -> FilterDecision | None:
        country = lead.company.country
        criteria = self.criteria

        if criteria.exclude_countries and country:
            for excluded in criteria.exclude_countries:
                if country_matches(country, excluded):
                    return FilterDecision.drop(
                        "exclude_countries", f"country {country!r} is excluded"
                    )

        if criteria.include_countries:
            if not country:
                return FilterDecision.drop(
                    "include_countries",
                    f"country unknown; only {', '.join(criteria.include_countries)} accepted",
                )
            if not any(country_matches(country, wanted) for wanted in criteria.include_countries):
                return FilterDecision.drop(
                    "include_countries", f"country {country!r} is not in the accepted list"
                )
        return None

    def _check_industry(self, lead: StandardizedLead) -> FilterDecision | None:
        industry = lead.company.industry
        if not industry:
            # An unknown industry only fails an allow-list; a deny-list cannot
            # rule out something it cannot see.
            if self._included_industries:
                return FilterDecision.drop("include_industries", "industry unknown")
            return None

        haystack = industry.casefold()
        if any(term in haystack for term in self._excluded_industries):
            return FilterDecision.drop("exclude_industries", f"industry {industry!r} is excluded")
        if self._included_industries and not any(
            term in haystack for term in self._included_industries
        ):
            return FilterDecision.drop(
                "include_industries", f"industry {industry!r} is not in the accepted list"
            )
        return None

    def _check_size(self, lead: StandardizedLead) -> FilterDecision | None:
        criteria = self.criteria
        if criteria.min_employees is None and criteria.max_employees is None:
            return None

        count = lead.company.employee_count
        if count is None:
            return FilterDecision.drop("employee_count", "company size unknown")

        if criteria.min_employees is not None and count < criteria.min_employees:
            return FilterDecision.drop(
                "min_employees", f"{count} employees is below the minimum {criteria.min_employees}"
            )
        if criteria.max_employees is not None and count > criteria.max_employees:
            return FilterDecision.drop(
                "max_employees", f"{count} employees exceeds the maximum {criteria.max_employees}"
            )
        return None

    def _check_seniority(self, lead: StandardizedLead) -> FilterDecision | None:
        criteria = self.criteria
        seniority = lead.person.seniority

        if criteria.exclude_seniority and seniority in criteria.exclude_seniority:
            return FilterDecision.drop(
                "exclude_seniority", f"seniority {seniority.value!r} is excluded"
            )
        if criteria.include_seniority and seniority not in criteria.include_seniority:
            return FilterDecision.drop(
                "include_seniority",
                f"seniority {seniority.value!r} is not in the accepted list",
            )
        return None

    def _check_title_keywords(self, lead: StandardizedLead) -> FilterDecision | None:
        title = lead.person.job_title
        if not title:
            if self._included_title_keywords:
                return FilterDecision.drop("include_title_keywords", "job title unknown")
            return None

        haystack = title.casefold()
        for keyword in self._excluded_title_keywords:
            if keyword in haystack:
                return FilterDecision.drop(
                    "exclude_title_keywords", f"job title {title!r} contains {keyword!r}"
                )
        if self._included_title_keywords and not any(
            keyword in haystack for keyword in self._included_title_keywords
        ):
            return FilterDecision.drop(
                "include_title_keywords", f"job title {title!r} matches no accepted keyword"
            )
        return None


def _matches_domain(domain: str, patterns: set[str]) -> bool:
    """Whether a host equals or is a subdomain of any excluded pattern."""
    if domain in patterns:
        return True
    return any(domain.endswith(f".{pattern}") for pattern in patterns)
