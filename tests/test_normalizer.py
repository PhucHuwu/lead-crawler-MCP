"""Tests for the normalization stage."""

from __future__ import annotations

import pytest

from src.models.enums import SeniorityLevel
from src.processors.normalizer import MAX_PLAUSIBLE_EMPLOYEES, Normalizer
from tests.conftest import make_raw_lead


@pytest.fixture
def normalizer() -> Normalizer:
    return Normalizer()


class TestPersonNormalization:
    def test_shouting_names_are_folded(self, normalizer: Normalizer) -> None:
        lead = normalizer.normalize(make_raw_lead(first_name="ADA", last_name="LOVELACE"))
        assert (lead.person.first_name, lead.person.last_name) == ("Ada", "Lovelace")
        assert lead.person.full_name == "Ada Lovelace"

    def test_full_name_derived_from_components(self, normalizer: Normalizer) -> None:
        lead = normalizer.normalize(
            make_raw_lead(first_name="Ada", last_name="Lovelace", full_name=None)
        )
        assert lead.person.full_name == "Ada Lovelace"

    def test_components_derived_from_full_name(self, normalizer: Normalizer) -> None:
        lead = normalizer.normalize(
            make_raw_lead(first_name=None, last_name=None, full_name="Ada King Lovelace")
        )
        assert lead.person.first_name == "Ada King"
        assert lead.person.last_name == "Lovelace"

    def test_partial_components_are_completed(self, normalizer: Normalizer) -> None:
        lead = normalizer.normalize(
            make_raw_lead(first_name="Ada", last_name=None, full_name="Ada Lovelace")
        )
        assert lead.person.last_name == "Lovelace"

    def test_email_is_lowercased_and_trimmed(self, normalizer: Normalizer) -> None:
        lead = normalizer.normalize(make_raw_lead(email="  ADA@ACME.COM "))
        assert lead.person.email == "ada@acme.com"

    def test_invalid_email_becomes_none(self, normalizer: Normalizer) -> None:
        assert normalizer.normalize(make_raw_lead(email="not-an-email")).person.email is None

    def test_phone_formatting_is_stripped(self, normalizer: Normalizer) -> None:
        lead = normalizer.normalize(make_raw_lead(phone="+1 (415) 555-0142"))
        assert lead.person.phone == "+14155550142"

    def test_company_linkedin_is_not_stored_as_a_person_profile(
        self, normalizer: Normalizer
    ) -> None:
        lead = normalizer.normalize(make_raw_lead(linkedin_url="linkedin.com/company/acme"))
        assert lead.person.linkedin_url is None

    def test_title_company_suffix_is_stripped(self, normalizer: Normalizer) -> None:
        lead = normalizer.normalize(make_raw_lead(job_title="CEO at Acme Corp"))
        assert lead.person.job_title == "CEO"


class TestSeniorityInference:
    @pytest.mark.parametrize(
        ("title", "expected"),
        [
            ("Chief Executive Officer", SeniorityLevel.C_SUITE),
            ("CTO", SeniorityLevel.C_SUITE),
            ("Co-Founder", SeniorityLevel.FOUNDER),
            ("VP of Sales", SeniorityLevel.VP),
            ("Vice President, Marketing", SeniorityLevel.VP),
            ("Director of Engineering", SeniorityLevel.DIRECTOR),
            ("Head of Growth", SeniorityLevel.DIRECTOR),
            ("Sales Manager", SeniorityLevel.MANAGER),
            ("Senior Software Engineer", SeniorityLevel.SENIOR),
            ("Marketing Intern", SeniorityLevel.INTERN),
            ("Junior Analyst", SeniorityLevel.ENTRY),
            ("Barista", SeniorityLevel.UNKNOWN),
        ],
    )
    def test_inferred_from_title(
        self, normalizer: Normalizer, title: str, expected: SeniorityLevel
    ) -> None:
        lead = normalizer.normalize(make_raw_lead(job_title=title))
        assert lead.person.seniority is expected

    def test_explicit_seniority_wins_over_inference(self, normalizer: Normalizer) -> None:
        lead = normalizer.normalize(
            make_raw_lead(job_title="Intern", seniority=SeniorityLevel.C_SUITE)
        )
        assert lead.person.seniority is SeniorityLevel.C_SUITE

    def test_developer_is_not_matched_as_vp(self, normalizer: Normalizer) -> None:
        # "vp" must not match inside another word.
        lead = normalizer.normalize(make_raw_lead(job_title="Developer"))
        assert lead.person.seniority is SeniorityLevel.UNKNOWN


class TestCompanyNormalization:
    def test_employee_band_takes_lower_bound(self, normalizer: Normalizer) -> None:
        lead = normalizer.normalize(make_raw_lead(company_employee_count="201-500"))
        assert lead.company.employee_count == 201

    def test_placeholder_headcount_becomes_none(self, normalizer: Normalizer) -> None:
        assert (
            normalizer.normalize(make_raw_lead(company_employee_count="N/A")).company.employee_count
            is None
        )

    def test_implausible_headcount_is_discarded(self, normalizer: Normalizer) -> None:
        lead = normalizer.normalize(
            make_raw_lead(company_employee_count=MAX_PLAUSIBLE_EMPLOYEES + 1)
        )
        assert lead.company.employee_count is None

    def test_domain_derived_from_website(self, normalizer: Normalizer) -> None:
        lead = normalizer.normalize(
            make_raw_lead(company_domain=None, company_website="https://www.acme.com/about")
        )
        assert lead.company.domain == "acme.com"

    def test_website_derived_from_domain(self, normalizer: Normalizer) -> None:
        lead = normalizer.normalize(make_raw_lead(company_website=None))
        assert lead.company.website == "https://acme.com"

    def test_domain_inferred_from_corporate_email(self, normalizer: Normalizer) -> None:
        lead = normalizer.normalize(
            make_raw_lead(company_domain=None, company_website=None, email="ada@acme.com")
        )
        assert lead.company.domain == "acme.com"

    def test_domain_not_inferred_from_consumer_email(self, normalizer: Normalizer) -> None:
        # A gmail address says nothing about the employer.
        lead = normalizer.normalize(
            make_raw_lead(company_domain=None, company_website=None, email="ada@gmail.com")
        )
        assert lead.company.domain is None

    def test_country_normalized_to_iso_code(self, normalizer: Normalizer) -> None:
        assert normalizer.normalize(make_raw_lead(company_country="usa")).company.country == "US"
        assert (
            normalizer.normalize(make_raw_lead(company_country="Deutschland")).company.country
            == "DE"
        )

    def test_unrecognized_country_passes_through(self, normalizer: Normalizer) -> None:
        lead = normalizer.normalize(make_raw_lead(company_country="Ruritania"))
        assert lead.company.country == "Ruritania"

    def test_company_linkedin_is_canonical(self, normalizer: Normalizer) -> None:
        lead = normalizer.normalize(
            make_raw_lead(company_linkedin_url="https://uk.linkedin.com/company/Acme/")
        )
        assert lead.company.linkedin_url == "https://www.linkedin.com/company/acme"


class TestProvenance:
    def test_source_fields_are_preserved(self, normalizer: Normalizer) -> None:
        raw = make_raw_lead(provider="apollo", external_id="p-1", source_url="https://x/y")
        lead = normalizer.normalize(raw)
        assert lead.source.provider == "apollo"
        assert lead.source.external_id == "p-1"
        assert lead.source.source_url == "https://x/y"

    def test_provider_is_required_by_the_model(self) -> None:
        result = Normalizer().normalize(make_raw_lead(provider="  "))
        # A blank provider is not a crash, but must not silently become empty.
        assert result.source.provider == "unknown"


class TestIdempotence:
    def test_normalizing_twice_is_stable(self, normalizer: Normalizer) -> None:
        once = normalizer.normalize(make_raw_lead())
        twice = normalizer.normalize(make_raw_lead())
        assert once.model_dump(exclude={"source"}) == twice.model_dump(exclude={"source"})
        assert once.lead_id == twice.lead_id
