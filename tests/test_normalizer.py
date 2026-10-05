"""Tests for the normalization stage."""

from __future__ import annotations

import unicodedata

import pytest

from src.models.enums import SeniorityLevel
from src.processors.normalizer import MAX_PLAUSIBLE_EMPLOYEES, Normalizer
from tests.conftest import make_raw_lead
from tests.fixtures.records import (
    INTERNATIONAL_COMPANIES,
    INTERNATIONAL_NAMES,
    VIETNAMESE_CITIES,
    VIETNAMESE_NAMES,
)


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

    def test_vietnamese_names_keep_their_diacritics(self, normalizer: Normalizer) -> None:
        lead = normalizer.normalize(make_raw_lead(full_name="Nguyễn Thị Hương"))
        assert lead.person.full_name == "Nguyễn Thị Hương"

    def test_decomposed_vietnamese_is_canonicalized(self, normalizer: Normalizer) -> None:
        # Two sources spelling the same name differently — one composed, one
        # decomposed — must land on one string, or deduplication and any
        # downstream grouping see two different people.
        decomposed = unicodedata.normalize("NFD", "Nguyễn Thị Hương")
        assert decomposed != "Nguyễn Thị Hương"
        lead = normalizer.normalize(make_raw_lead(full_name=decomposed))
        assert lead.person.full_name == "Nguyễn Thị Hương"

    def test_a_shouting_vietnamese_name_is_folded_not_mangled(self, normalizer: Normalizer) -> None:
        # Title-casing runs on the composed text, so the diacritics have to
        # survive the case change as well as the whitespace cleanup.
        lead = normalizer.normalize(make_raw_lead(full_name="NGUYỄN THỊ HƯƠNG"))
        assert lead.person.full_name == "Nguyễn Thị Hương"

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


class TestVietnameseData:
    """Vietnamese records, which the shipped search profiles target.

    Vietnam-specific hazards are not hypothetical here: a company's legal form
    (``Công ty TNHH``, ``Cổ phần``) is part of its name, addresses are written
    street-last, and the same text arrives composed from an API and decomposed
    from a Mac. All of it has to survive normalization intact.
    """

    @pytest.mark.parametrize("name", VIETNAMESE_NAMES)
    def test_names_keep_every_diacritic(self, normalizer: Normalizer, name: str) -> None:
        lead = normalizer.normalize(make_raw_lead(full_name=name, first_name=None, last_name=None))
        assert lead.person.full_name == name

    @pytest.mark.parametrize("name", VIETNAMESE_NAMES)
    def test_the_decomposed_form_reaches_the_same_string(
        self, normalizer: Normalizer, name: str
    ) -> None:
        decomposed = unicodedata.normalize("NFD", name)
        composed_lead = normalizer.normalize(
            make_raw_lead(full_name=name, first_name=None, last_name=None)
        )
        decomposed_lead = normalizer.normalize(
            make_raw_lead(full_name=decomposed, first_name=None, last_name=None)
        )
        assert decomposed_lead.person.full_name == composed_lead.person.full_name

    @pytest.mark.parametrize("company", [name for name, _ in INTERNATIONAL_COMPANIES])
    def test_company_names_lose_no_letters(self, normalizer: Normalizer, company: str) -> None:
        # The invariant that matters: whatever title-casing does to the shape of
        # a name, it must not add or drop a character. A truncated
        # `Công ty TNHH Giải pháp Số` would make every Vietnamese company the
        # same company.
        lead = normalizer.normalize(make_raw_lead(company_name=company))
        assert lead.company.name is not None
        assert lead.company.name.casefold() == company.casefold()

    @pytest.mark.parametrize(
        "company",
        ["東京テクノロジー株式会社", "شركة الحلول الرقمية", "Acme & Sons, Inc.", "Công ty Cổ phần"],
    )
    def test_company_names_without_shouting_are_untouched(
        self, normalizer: Normalizer, company: str
    ) -> None:
        lead = normalizer.normalize(make_raw_lead(company_name=company))
        assert lead.company.name == company

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Công ty TNHH Giải pháp Số", "Công ty Tnhh Giải pháp Số"),
            ("ООО Технологии", "Ооо Технологии"),
            ("ACME CORP", "Acme Corp"),
        ],
    )
    def test_legal_forms_are_folded_like_shouting(
        self, normalizer: Normalizer, raw: str, expected: str
    ) -> None:
        # A known limitation, pinned so it is visible rather than discovered in
        # an export. Company names are title-cased by the same helper as person
        # names, and that helper can only ask "is this token all-caps?" — so a
        # legal form (`TNHH`, `ООО`) is indistinguishable from a shouted word
        # and is folded to `Tnhh`/`Ооо`. Separating the two needs a list of
        # legal forms per language, which is why it is recorded here instead.
        #
        # Display-only: the slug is built from casefolded text, so `Tnhh` and
        # `TNHH` produce the same `name_company` key and matching is unaffected.
        lead = normalizer.normalize(make_raw_lead(company_name=raw))
        assert lead.company.name == expected

    @pytest.mark.parametrize("city", VIETNAMESE_CITIES)
    def test_cities_survive(self, normalizer: Normalizer, city: str) -> None:
        lead = normalizer.normalize(make_raw_lead(company_city=city))
        assert lead.company.city == city

    def test_a_city_is_not_mistaken_for_a_country(self, normalizer: Normalizer) -> None:
        # `Hà Nội` must not be coerced to an ISO country code, and an unknown
        # country must pass through rather than be dropped.
        lead = normalizer.normalize(
            make_raw_lead(company_city="Hà Nội", company_country="Việt Nam")
        )
        assert lead.company.city == "Hà Nội"
        assert lead.company.country == "Việt Nam"

    def test_vietnam_is_recognized_however_it_is_spelled(self, normalizer: Normalizer) -> None:
        for spelling in ("Vietnam", "Viet Nam", "VIETNAM", "vietnam"):
            lead = normalizer.normalize(make_raw_lead(company_country=spelling))
            assert lead.company.country == "VN", spelling

    def test_a_vietnamese_title_is_not_treated_as_a_seniority_keyword(
        self, normalizer: Normalizer
    ) -> None:
        # The seniority vocabulary is English (`head`, `vp`, `owner`). A
        # Vietnamese title must fall through to UNKNOWN rather than match by
        # accident, so the record is not silently ranked on a guess.
        lead = normalizer.normalize(make_raw_lead(job_title="Giám đốc Kỹ thuật", seniority=None))
        assert lead.person.seniority is SeniorityLevel.UNKNOWN

    def test_a_vietnamese_company_without_a_domain_keeps_its_identity_key(
        self, normalizer: Normalizer
    ) -> None:
        # The end-to-end property the slug fix exists for: strip the email and
        # the only thing left to match on is the name plus the company name.
        lead = normalizer.normalize(
            make_raw_lead(
                full_name="Nguyễn Văn An",
                first_name=None,
                last_name=None,
                company_name="Công ty TNHH Giải pháp Số",
                company_domain=None,
                company_website=None,
                email=None,
            )
        )
        assert lead.identity_keys(), "a Vietnamese lead lost every identity key"


class TestInternationalScripts:
    """Every script the crawler can meet, through the whole normalization path."""

    @pytest.mark.parametrize(("name", "script"), INTERNATIONAL_NAMES)
    def test_a_name_in_any_script_round_trips(
        self, normalizer: Normalizer, name: str, script: str
    ) -> None:
        lead = normalizer.normalize(make_raw_lead(full_name=name, first_name=None, last_name=None))
        assert lead.person.full_name == name, script

    @pytest.mark.parametrize(("name", "script"), INTERNATIONAL_NAMES)
    def test_a_non_latin_lead_keeps_an_identity_key(
        self, normalizer: Normalizer, name: str, script: str
    ) -> None:
        # Without this, a record from any non-Latin script with no email is
        # invisible to deduplication: two copies of one person stay two leads.
        lead = normalizer.normalize(
            make_raw_lead(
                full_name=name,
                first_name=None,
                last_name=None,
                company_name="Acme Corp",
                email=None,
            )
        )
        assert lead.identity_keys(), f"{script} lead has no identity keys"

    def test_a_name_is_not_reduced_to_ascii(self, normalizer: Normalizer) -> None:
        # Transliteration would be a silent change of a person's name in the
        # output, which is worse than an unmerged duplicate.
        lead = normalizer.normalize(
            make_raw_lead(full_name="Ольга Иванова", first_name=None, last_name=None)
        )
        assert lead.person.full_name == "Ольга Иванова"

    def test_right_to_left_text_is_not_reordered(self, normalizer: Normalizer) -> None:
        lead = normalizer.normalize(
            make_raw_lead(full_name="محمد الأحمد", first_name=None, last_name=None)
        )
        assert lead.person.full_name == "محمد الأحمد"


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


class TestRawJobTitle:
    """Normalizing a title must not destroy what the source actually said."""

    def test_original_is_kept_when_cleaning_changed_it(self, normalizer: Normalizer) -> None:
        lead = normalizer.normalize(make_raw_lead(job_title="CTO at Acme Corp"))
        assert lead.person.job_title == "CTO"
        assert lead.person.job_title_raw == "CTO at Acme Corp"

    def test_original_is_omitted_when_nothing_changed(self, normalizer: Normalizer) -> None:
        # The common case. Storing a duplicate would add a column of noise.
        lead = normalizer.normalize(make_raw_lead(job_title="VP Engineering"))
        assert lead.person.job_title == "VP Engineering"
        assert lead.person.job_title_raw is None

    def test_whitespace_noise_alone_does_not_count_as_a_change(
        self, normalizer: Normalizer
    ) -> None:
        lead = normalizer.normalize(make_raw_lead(job_title="  Head   of  Engineering  "))
        assert lead.person.job_title == "Head of Engineering"
        assert lead.person.job_title_raw is None

    def test_missing_title_yields_no_raw_value(self, normalizer: Normalizer) -> None:
        lead = normalizer.normalize(make_raw_lead(job_title=None))
        assert (lead.person.job_title, lead.person.job_title_raw) == (None, None)

    def test_seniority_is_inferred_from_the_normalized_title(self, normalizer: Normalizer) -> None:
        lead = normalizer.normalize(make_raw_lead(job_title="CTO at Acme Corp"))
        assert lead.person.seniority is SeniorityLevel.C_SUITE


class TestNormalizationIssues:
    """Values the source sent that could not be read are reported, not erased.

    Without this, a garbled domain and an absent domain are the same thing by the
    time the validator sees the record — and only one of them is a data problem.
    """

    def test_an_unreadable_domain_is_reported(self, normalizer: Normalizer) -> None:
        lead = normalizer.normalize(make_raw_lead(company_domain="not a domain"))
        assert [(i.rule, i.field) for i in lead.normalization_issues] == [
            ("invalid_domain", "company_domain")
        ]

    def test_an_unreadable_domain_leaves_the_field_empty(self, normalizer: Normalizer) -> None:
        # No website and a consumer mailbox, so there is nothing to fall back to.
        lead = normalizer.normalize(
            make_raw_lead(company_domain="not a domain", email="ada@gmail.com")
        )
        assert lead.company.domain is None

    def test_the_offending_value_is_preserved(self, normalizer: Normalizer) -> None:
        lead = normalizer.normalize(make_raw_lead(company_domain="not a domain"))
        assert lead.normalization_issues[0].value == "not a domain"

    def test_a_malformed_website_is_reported(self, normalizer: Normalizer) -> None:
        lead = normalizer.normalize(make_raw_lead(company_website="http://"))
        assert [i.rule for i in lead.normalization_issues] == ["malformed_url"]

    def test_a_malformed_contact_url_is_reported(self, normalizer: Normalizer) -> None:
        lead = normalizer.normalize(make_raw_lead(company_contact_url="javascript:void(0)"))
        assert [i.rule for i in lead.normalization_issues] == ["malformed_url"]

    def test_a_malformed_email_is_reported(self, normalizer: Normalizer) -> None:
        lead = normalizer.normalize(make_raw_lead(email="not-an-email"))
        assert [i.rule for i in lead.normalization_issues] == ["invalid_email"]
        assert lead.person.email is None

    @pytest.mark.parametrize("marker", [None, "", "  ", "n/a", "N/A", "-"])
    def test_an_absent_value_is_not_an_issue(
        self, normalizer: Normalizer, marker: str | None
    ) -> None:
        # Absent is not malformed. Reporting these would bury the real problems.
        lead = normalizer.normalize(make_raw_lead(company_domain=marker, email=marker))
        assert lead.normalization_issues == []

    def test_a_readable_value_is_not_an_issue(self, normalizer: Normalizer) -> None:
        lead = normalizer.normalize(
            make_raw_lead(
                company_domain="https://www.acme.com/about",
                email="ada@acme.com",
                company_contact_url="https://acme.com/contact?utm_source=x",
            )
        )
        assert lead.normalization_issues == []

    def test_a_mislabelled_linkedin_url_is_not_called_malformed(
        self, normalizer: Normalizer
    ) -> None:
        # A company page in the person field is the wrong kind of URL, not a
        # broken one. Reporting it as malformed would be a false accusation.
        lead = normalizer.normalize(
            make_raw_lead(linkedin_url="https://www.linkedin.com/company/acme")
        )
        assert lead.normalization_issues == []

    def test_a_broken_linkedin_url_is_reported(self, normalizer: Normalizer) -> None:
        lead = normalizer.normalize(make_raw_lead(linkedin_url="https://acme.com/ada"))
        assert [i.rule for i in lead.normalization_issues] == ["malformed_url"]

    def test_a_fallback_does_not_hide_the_loss(self, normalizer: Normalizer) -> None:
        # The company still ends up with a usable domain from its website, so the
        # finished lead looks fine — the issue list is the only trace that the
        # domain field itself was garbage.
        lead = normalizer.normalize(
            make_raw_lead(company_domain="not a domain", company_website="acme.com")
        )
        assert lead.company.domain == "acme.com"
        assert [i.field for i in lead.normalization_issues] == ["company_domain"]

    def test_a_long_value_is_truncated(self, normalizer: Normalizer) -> None:
        lead = normalizer.normalize(make_raw_lead(company_domain="x" * 500))
        assert len(lead.normalization_issues[0].value) <= 200

    def test_issues_do_not_affect_identity_or_completeness(self, normalizer: Normalizer) -> None:
        # They describe the record; they are not part of it, and they must never
        # change how it ranks or what it matches on.
        clean = normalizer.normalize(make_raw_lead())
        dirty = normalizer.normalize(make_raw_lead(company_domain="not a domain"))
        assert clean.lead_id == dirty.lead_id
        assert clean.completeness == dirty.completeness

    def test_issues_stay_out_of_the_exported_columns(self, normalizer: Normalizer) -> None:
        lead = normalizer.normalize(make_raw_lead(company_domain="not a domain"))
        assert "normalization_issues" not in lead.flatten()
