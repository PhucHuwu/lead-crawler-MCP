"""Tests for the validation stage."""

from __future__ import annotations

import pytest

from src.processors.validator import LeadValidator
from tests.conftest import make_lead


@pytest.fixture
def validator() -> LeadValidator:
    return LeadValidator()


def rules(outcome: object) -> set[str]:
    return {issue.rule for issue in outcome.issues}  # type: ignore[attr-defined]


class TestPersonIdentity:
    def test_complete_lead_is_valid(self, validator: LeadValidator) -> None:
        outcome = validator.validate(make_lead())
        assert outcome.is_valid, outcome.summary()

    def test_email_alone_is_enough(self, validator: LeadValidator) -> None:
        lead = make_lead(full_name=None, first_name=None, last_name=None, linkedin_url=None)
        assert validator.validate(lead).is_valid

    def test_linkedin_alone_is_enough(self, validator: LeadValidator) -> None:
        lead = make_lead(email=None, full_name=None, first_name=None, last_name=None)
        lead.person.linkedin_url = "https://www.linkedin.com/in/ada"
        assert validator.validate(lead).is_valid

    def test_no_identity_at_all_is_rejected(self, validator: LeadValidator) -> None:
        lead = make_lead(
            email=None,
            full_name=None,
            first_name=None,
            last_name=None,
            company_name=None,
            company_domain=None,
        )
        assert "no_identity" in rules(validator.validate(lead))

    def test_company_alone_is_enough(self, validator: LeadValidator) -> None:
        # An enrichment source yields accounts, not contacts. Requiring a person
        # would silently discard every lead the `website` source produces.
        lead = make_lead(
            email=None,
            full_name=None,
            first_name=None,
            last_name=None,
            company_name="Acme Corp",
            company_domain="acme.com",
        )
        assert validator.validate(lead).is_valid

    def test_a_website_alone_is_not_a_company_identity(self, validator: LeadValidator) -> None:
        # We cannot name or address an organization from a URL alone, so this
        # still counts as having no identity at all.
        lead = make_lead(
            email=None,
            full_name=None,
            first_name=None,
            last_name=None,
            company_name=None,
            company_domain=None,
        )
        lead.company.website = "https://acme.com"
        assert not validator.validate(lead).is_valid

    def test_company_signal_required(self, validator: LeadValidator) -> None:
        lead = make_lead(company_name=None, company_domain=None, email=None)
        lead.person.linkedin_url = "https://www.linkedin.com/in/ada"
        assert "no_company_signal" in rules(validator.validate(lead))

    def test_email_can_supply_the_company_signal(self, validator: LeadValidator) -> None:
        lead = make_lead(company_name=None, company_domain=None)
        assert validator.validate(lead).is_valid


class TestNamePlausibility:
    @pytest.mark.parametrize("name", ["-", "x", "12345"])
    def test_implausible_names_rejected(self, validator: LeadValidator, name: str) -> None:
        outcome = validator.validate(make_lead(full_name=name))
        assert "implausible_person_name" in rules(outcome)

    @pytest.mark.parametrize("name", ["Info", "Sales", "Support", "Unknown"])
    def test_department_names_rejected(self, validator: LeadValidator, name: str) -> None:
        outcome = validator.validate(make_lead(full_name=name))
        assert "non_person_name" in rules(outcome)

    def test_name_matching_company_rejected(self, validator: LeadValidator) -> None:
        lead = make_lead(full_name="Acme Corp", company_name="Acme Corp")
        assert "name_equals_company" in rules(validator.validate(lead))


class TestEmailChecks:
    def test_malformed_email_rejected(self, validator: LeadValidator) -> None:
        lead = make_lead()
        # Bypass the normalizer to simulate a value that slipped through.
        lead.person.email = "broken@@acme.com"
        assert "invalid_email" in rules(validator.validate(lead))

    @pytest.mark.parametrize("address", ["ada@gmail.com", "ada@yahoo.co.uk", "ada@proton.me"])
    def test_consumer_mailboxes_are_not_a_mismatch(
        self, validator: LeadValidator, address: str
    ) -> None:
        # A personal address is normal and does not contradict the employer.
        lead = make_lead(email=address)
        assert validator.validate(lead).is_valid

    def test_subdomain_is_not_a_mismatch(self, validator: LeadValidator) -> None:
        lead = make_lead(email="ada@mail.acme.com", company_domain="acme.com")
        assert validator.validate(lead).is_valid

    def test_unrelated_domains_are_a_mismatch(self, validator: LeadValidator) -> None:
        lead = make_lead(email="ada@other-corp.com", company_domain="acme.com")
        assert "email_company_mismatch" in rules(validator.validate(lead))


class TestOutcome:
    def test_collects_every_issue_not_just_the_first(self, validator: LeadValidator) -> None:
        lead = make_lead(full_name="Info", email=None)
        lead.person.email = "broken@@x.com"
        outcome = validator.validate(lead)
        assert len(outcome.issues) >= 2

    def test_summary_joins_messages(self, validator: LeadValidator) -> None:
        # Needs two issues for the separator to appear at all.
        lead = make_lead(full_name="Info", email=None)
        lead.person.email = "broken@@x.com"
        summary = validator.validate(lead).summary()
        assert ";" in summary
        assert summary.count(";") == len(validator.validate(lead).issues) - 1

    def test_first_rule_is_reported(self, validator: LeadValidator) -> None:
        lead = make_lead(full_name="Info")
        assert validator.validate(lead).first_rule == "non_person_name"

    def test_valid_lead_has_no_first_rule(self, validator: LeadValidator) -> None:
        assert validator.validate(make_lead()).first_rule is None
