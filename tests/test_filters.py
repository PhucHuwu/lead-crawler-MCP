"""Tests for the filtering stage."""

from __future__ import annotations

import pytest

from src.config import FilterSettings
from src.models.enums import SeniorityLevel
from src.processors.filters import LeadFilter
from tests.conftest import make_lead


def build(**criteria: object) -> LeadFilter:
    return LeadFilter(FilterSettings(**criteria))


class TestDefaults:
    def test_no_rules_keeps_everything(self) -> None:
        assert build().evaluate(make_lead()).passed is True

    def test_every_decision_names_its_rule(self) -> None:
        decision = build(require_email=True).evaluate(make_lead(email=None))
        assert decision.passed is False
        assert decision.rule == "require_email"
        assert decision.detail


class TestGeography:
    @pytest.mark.parametrize("excluded", ["US", "United States", "usa"])
    def test_exclude_countries_matches_aliases(self, excluded: str) -> None:
        assert build(exclude_countries=[excluded]).evaluate(make_lead(country="US")).passed is False

    def test_include_countries_accepts_matching(self) -> None:
        assert build(include_countries=["US"]).evaluate(make_lead(country="US")).passed is True

    def test_include_countries_rejects_other(self) -> None:
        decision = build(include_countries=["US"]).evaluate(make_lead(country="DE"))
        assert decision.rule == "include_countries"

    def test_include_countries_rejects_unknown_country(self) -> None:
        # An allow-list cannot be satisfied by a value we cannot verify.
        decision = build(include_countries=["US"]).evaluate(make_lead(country=None))
        assert decision.rule == "include_countries"

    def test_exclude_countries_ignores_unknown_country(self) -> None:
        # A deny-list cannot rule out something it cannot see.
        assert build(exclude_countries=["US"]).evaluate(make_lead(country=None)).passed is True


class TestFirmographics:
    def test_exclude_domains_matches_exact(self) -> None:
        decision = build(exclude_domains=["acme.com"]).evaluate(make_lead())
        assert decision.rule == "exclude_domains"

    def test_exclude_domains_matches_subdomain(self) -> None:
        lead = make_lead(company_domain="careers.acme.com")
        assert build(exclude_domains=["acme.com"]).evaluate(lead).passed is False

    def test_exclude_domains_does_not_match_unrelated(self) -> None:
        assert build(exclude_domains=["other.com"]).evaluate(make_lead()).passed is True

    def test_employee_bounds(self) -> None:
        assert build(min_employees=100).evaluate(make_lead(employee_count=250)).passed is True
        assert build(min_employees=500).evaluate(make_lead(employee_count=250)).passed is False
        assert build(max_employees=100).evaluate(make_lead(employee_count=250)).passed is False

    def test_size_rule_rejects_unknown_headcount(self) -> None:
        decision = build(min_employees=10).evaluate(make_lead(employee_count=None))
        assert decision.rule == "employee_count"

    def test_industry_substring_match(self) -> None:
        lead = make_lead(industry="Software Development")
        assert build(include_industries=["software"]).evaluate(lead).passed is True
        assert build(exclude_industries=["software"]).evaluate(lead).passed is False

    def test_unknown_industry_only_fails_an_allow_list(self) -> None:
        lead = make_lead(industry=None)
        assert build(exclude_industries=["software"]).evaluate(lead).passed is True
        assert build(include_industries=["software"]).evaluate(lead).passed is False


class TestPerson:
    def test_seniority_allow_list(self) -> None:
        lead = make_lead()
        lead.person.seniority = SeniorityLevel.VP
        assert build(include_seniority=["vp", "c_suite"]).evaluate(lead).passed is True
        assert build(include_seniority=["intern"]).evaluate(lead).passed is False

    def test_seniority_deny_list(self) -> None:
        lead = make_lead()
        lead.person.seniority = SeniorityLevel.INTERN
        assert build(exclude_seniority=["intern"]).evaluate(lead).passed is False

    def test_title_keyword_allow_list(self) -> None:
        assert build(include_title_keywords=["engineer"]).evaluate(make_lead()).passed is True
        assert build(include_title_keywords=["designer"]).evaluate(make_lead()).passed is False

    def test_title_keyword_deny_list(self) -> None:
        assert build(exclude_title_keywords=["engineering"]).evaluate(make_lead()).passed is False

    def test_missing_title_fails_only_an_allow_list(self) -> None:
        lead = make_lead(job_title=None)
        assert build(exclude_title_keywords=["intern"]).evaluate(lead).passed is True
        assert build(include_title_keywords=["engineer"]).evaluate(lead).passed is False


class TestContactability:
    def test_require_email(self) -> None:
        assert build(require_email=True).evaluate(make_lead(email=None)).passed is False

    def test_require_company_domain(self) -> None:
        assert (
            build(require_company_domain=True).evaluate(make_lead(company_domain=None)).passed
            is False
        )

    def test_require_linkedin(self) -> None:
        assert build(require_linkedin=True).evaluate(make_lead()).passed is False

    def test_exclude_free_email(self) -> None:
        lead = make_lead(email="ada@gmail.com")
        assert build(exclude_free_email=True).evaluate(lead).passed is False

    @pytest.mark.parametrize("address", ["info@acme.com", "sales@acme.com", "INFO.emea@acme.com"])
    def test_role_based_email(self, address: str) -> None:
        lead = make_lead(email=address)
        assert build(exclude_role_based_email=True).evaluate(lead).passed is False

    def test_personal_address_survives_role_filter(self) -> None:
        assert build(exclude_role_based_email=True).evaluate(make_lead()).passed is True

    def test_exclude_email_domains(self) -> None:
        lead = make_lead(email="ada@acme.com")
        assert build(exclude_email_domains=["acme.com"]).evaluate(lead).passed is False


class TestRuleOrdering:
    def test_first_matching_rule_is_reported(self) -> None:
        # Deterministic attribution: require_email is checked before the size rule.
        lead = make_lead(email=None, employee_count=1)
        decision = build(require_email=True, min_employees=100).evaluate(lead)
        assert decision.rule == "require_email"

    def test_completeness_rule_runs_first(self) -> None:
        sparse = make_lead(email=None, job_title=None, company_domain=None, country=None)
        decision = LeadFilter(FilterSettings(require_email=True), min_completeness=0.9).evaluate(
            sparse
        )
        assert decision.rule == "min_completeness"

    def test_completeness_zero_disables_the_rule(self) -> None:
        sparse = make_lead(email=None, job_title=None, company_domain=None, country=None)
        assert LeadFilter(FilterSettings(), min_completeness=0.0).evaluate(sparse).passed is True


class TestMatchingBehaviour:
    def test_matching_is_case_insensitive(self) -> None:
        lead = make_lead(company_domain="acme.com")
        assert build(exclude_domains=["ACME.COM"]).evaluate(lead).passed is False

    def test_blank_allow_entries_are_ignored(self) -> None:
        # An empty CSV element must not become a rule that matches nothing.
        assert build(include_industries=["", "  "]).evaluate(make_lead()).passed is True
