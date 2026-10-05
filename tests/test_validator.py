"""Tests for the validation stage."""

from __future__ import annotations

import pytest

from src.models.enums import ValidationSeverity
from src.models.lead import NormalizationIssue
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


class TestSeverity:
    """Validation must be able to mark a record, not only reject it.

    A lead whose company domain was garbled upstream is still addressable.
    Dropping it would discard good data to punish one bad field.
    """

    def test_every_rule_is_fatal_by_default(self, validator: LeadValidator) -> None:
        lead = make_lead(full_name="Info")
        assert all(
            issue.severity is ValidationSeverity.ERROR for issue in validator.validate(lead).issues
        )

    def test_a_warning_does_not_invalidate_the_record(self, validator: LeadValidator) -> None:
        lead = make_lead()
        lead.normalization_issues = [
            NormalizationIssue(rule="invalid_domain", field="company_domain", value="junk")
        ]
        outcome = validator.validate(lead)
        assert outcome.is_valid
        assert outcome.has_warnings
        assert not outcome.errors

    def test_a_warning_is_still_reported(self, validator: LeadValidator) -> None:
        lead = make_lead()
        lead.normalization_issues = [
            NormalizationIssue(rule="invalid_domain", field="company_domain", value="junk")
        ]
        outcome = validator.validate(lead)
        assert [issue.rule for issue in outcome.warnings] == ["invalid_domain"]
        assert "junk" in outcome.summary()

    def test_an_error_outranks_a_warning(self, validator: LeadValidator) -> None:
        # Both at once: the record is dropped, but the warning is still visible.
        lead = make_lead(full_name="Info", company_name=None, company_domain=None, email=None)
        lead.normalization_issues = [
            NormalizationIssue(rule="invalid_domain", field="company_domain", value="junk")
        ]
        outcome = validator.validate(lead)
        assert not outcome.is_valid
        assert outcome.has_warnings
        assert outcome.first_rule != "invalid_domain"

    @pytest.mark.parametrize(
        ("rule", "expected_fragment"),
        [
            ("invalid_domain", "not a usable domain"),
            ("malformed_url", "not a usable URL"),
            ("invalid_email", "not a usable email address"),
        ],
    )
    def test_each_rule_gets_its_own_phrasing(
        self, validator: LeadValidator, rule: str, expected_fragment: str
    ) -> None:
        lead = make_lead()
        lead.normalization_issues = [
            NormalizationIssue(rule=rule, field="company_domain", value="junk")
        ]
        assert expected_fragment in validator.validate(lead).summary()

    def test_an_unrecognised_rule_is_still_reported(self, validator: LeadValidator) -> None:
        # A rule the normalizer adds later must not vanish just because no
        # message template exists for it yet.
        lead = make_lead()
        lead.normalization_issues = [
            NormalizationIssue(rule="brand_new_rule", field="company_city", value="junk")
        ]
        outcome = validator.validate(lead)
        assert [issue.rule for issue in outcome.warnings] == ["brand_new_rule"]
        assert "could not be normalized" in outcome.summary()


class TestOutcomeRules:
    def test_rules_are_deduplicated(self, validator: LeadValidator) -> None:
        # Statistics count records affected by a rule, so two unreadable URLs is
        # one lead with a URL problem, not two.
        lead = make_lead()
        lead.normalization_issues = [
            NormalizationIssue(rule="malformed_url", field="company_website", value="a"),
            NormalizationIssue(rule="malformed_url", field="company_contact_url", value="b"),
        ]
        assert validator.validate(lead).rules == ("malformed_url",)
        assert len(validator.validate(lead).warnings) == 2

    def test_rules_follow_first_seen_order(self, validator: LeadValidator) -> None:
        lead = make_lead(full_name="Info")
        lead.normalization_issues = [
            NormalizationIssue(rule="invalid_domain", field="company_domain", value="junk")
        ]
        assert validator.validate(lead).rules == ("non_person_name", "invalid_domain")

    def test_a_clean_lead_has_no_rules(self, validator: LeadValidator) -> None:
        outcome = validator.validate(make_lead())
        assert outcome.rules == ()
        assert not outcome.has_warnings
