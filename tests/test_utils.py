"""Tests for the normalization helpers in :mod:`src.utils`."""

from __future__ import annotations

import pytest

from src.utils.numbers import parse_employee_count
from src.utils.text import (
    clean_text,
    join_full_name,
    normalize_email,
    normalize_phone,
    split_full_name,
    strip_title_suffix,
    titlecase_name,
)
from src.utils.urls import (
    apex_domain,
    email_domain,
    is_free_email_domain,
    normalize_domain,
    normalize_linkedin_url,
    normalize_website,
)


class TestCleanText:
    @pytest.mark.parametrize("value", [None, "", "   ", "N/A", "n/a", "-", "null", "unknown"])
    def test_empty_markers_become_none(self, value: object) -> None:
        assert clean_text(value) is None

    def test_collapses_whitespace_and_strips(self) -> None:
        assert clean_text("  Acme   Corp \n") == "Acme Corp"

    def test_removes_zero_width_characters(self) -> None:
        assert clean_text("Ac​me") == "Acme"

    def test_applies_nfkc_normalization(self) -> None:
        # Full-width digits fold to ASCII.
        assert clean_text("１２３") == "123"

    def test_coerces_non_strings(self) -> None:
        assert clean_text(42) == "42"


class TestTitlecaseName:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("ADA", "Ada"),
            ("ada", "ada"),
            ("McDonald", "McDonald"),
            ("VAN DER BERG", "Van Der Berg"),
            ("O'NEILL", "O'Neill"),
            ("MARY-JANE", "Mary-Jane"),
            ("eBay", "eBay"),
        ],
    )
    def test_preserves_intentional_casing(self, raw: str, expected: str) -> None:
        assert titlecase_name(raw) == expected

    def test_returns_none_for_empty(self) -> None:
        assert titlecase_name("  ") is None


class TestNames:
    def test_split_multi_token(self) -> None:
        assert split_full_name("Ada King Lovelace") == ("Ada King", "Lovelace")

    def test_split_single_token(self) -> None:
        assert split_full_name("Ada") == ("Ada", None)

    def test_split_empty(self) -> None:
        assert split_full_name(None) == (None, None)

    def test_join_skips_missing_parts(self) -> None:
        assert join_full_name("Ada", None) == "Ada"
        assert join_full_name(None, "Lovelace") == "Lovelace"
        assert join_full_name(None, None) is None


class TestNormalizeEmail:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("ADA@ACME.COM", "ada@acme.com"),
            ("  ada@acme.com  ", "ada@acme.com"),
            ("mailto:ada@acme.com", "ada@acme.com"),
            ("Ada Lovelace <ada@acme.com>", "ada@acme.com"),
            ("ada@acme.com.", "ada@acme.com"),
        ],
    )
    def test_normalizes_valid_forms(self, raw: str, expected: str) -> None:
        assert normalize_email(raw) == expected

    @pytest.mark.parametrize(
        "raw",
        [None, "", "not-an-email", "ada@", "@acme.com", "ada@acme", "Ada <nope>", "n/a"],
    )
    def test_rejects_unusable(self, raw: object) -> None:
        assert normalize_email(raw) is None


class TestNormalizePhone:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("+1 (415) 555-0142", "+14155550142"),
            ("415.555.0142", "4155550142"),
            ("0039 02 1234 5678", "+390212345678"),
            ("+84 90 123 4567", "+84901234567"),
        ],
    )
    def test_strips_formatting(self, raw: str, expected: str) -> None:
        assert normalize_phone(raw) == expected

    @pytest.mark.parametrize("raw", [None, "", "N/A", "12345", "ext. 42"])
    def test_rejects_too_short(self, raw: object) -> None:
        assert normalize_phone(raw) is None


class TestStripTitleSuffix:
    def test_removes_at_company(self) -> None:
        assert strip_title_suffix("CEO at Acme Corp") == "CEO"
        assert strip_title_suffix("CTO @ Acme") == "CTO"

    def test_leaves_plain_title(self) -> None:
        assert strip_title_suffix("VP of Sales") == "VP of Sales"

    def test_none_passthrough(self) -> None:
        assert strip_title_suffix(None) is None


class TestParseEmployeeCount:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (250, 250),
            ("250", 250),
            ("1,200", 1200),
            ("1.2k", 1200),
            ("3M", 3_000_000),
            ("1000+", 1000),
            ("201-500", 201),
            ("201 - 500 employees", 201),
            ("51–200", 51),
            ("approx. 250", 250),
        ],
    )
    def test_parses_common_shapes(self, raw: object, expected: int) -> None:
        assert parse_employee_count(raw) == expected

    @pytest.mark.parametrize("raw", [None, "", "N/A", "unknown", "many", True, -5])
    def test_returns_none_for_junk(self, raw: object) -> None:
        assert parse_employee_count(raw) is None

    def test_range_returns_lower_bound(self) -> None:
        # Documented conservative choice: never overstate a company's size.
        assert parse_employee_count("5001-10000") == 5001


class TestUrls:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("https://WWW.Acme.com/path?q=1", "acme.com"),
            ("acme.com", "acme.com"),
            ("http://acme.com:8080/x", "acme.com"),
            ("ada@acme.com", "acme.com"),
            ("careers.acme.com", "careers.acme.com"),
            ("acme.com.", "acme.com"),
        ],
    )
    def test_normalize_domain(self, raw: str, expected: str) -> None:
        assert normalize_domain(raw) == expected

    @pytest.mark.parametrize("raw", [None, "", "not a domain", "localhost", "/just/a/path"])
    def test_normalize_domain_rejects(self, raw: object) -> None:
        assert normalize_domain(raw) is None

    def test_apex_domain(self) -> None:
        assert apex_domain("careers.acme.com") == "acme.com"
        assert apex_domain("acme.com") == "acme.com"

    def test_email_domain(self) -> None:
        assert email_domain("ada@acme.com") == "acme.com"
        assert email_domain("nonsense") is None

    @pytest.mark.parametrize("address", ["a@gmail.com", "b@yahoo.co.uk", "c@proton.me"])
    def test_free_email_detection(self, address: str) -> None:
        assert is_free_email_domain(address) is True

    def test_corporate_is_not_free(self) -> None:
        assert is_free_email_domain("ada@acme.com") is False

    def test_normalize_website(self) -> None:
        assert normalize_website("acme.com/about") == "https://acme.com"

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (
                "https://www.linkedin.com/in/Ada-Lovelace/",
                "https://www.linkedin.com/in/ada-lovelace",
            ),
            ("linkedin.com/in/ada", "https://www.linkedin.com/in/ada"),
            ("uk.linkedin.com/pub/ada-lovelace", "https://www.linkedin.com/in/ada-lovelace"),
        ],
    )
    def test_linkedin_person(self, raw: str, expected: str) -> None:
        assert normalize_linkedin_url(raw, kind="person") == expected

    def test_linkedin_company(self) -> None:
        assert (
            normalize_linkedin_url("https://www.linkedin.com/company/acme", kind="company")
            == "https://www.linkedin.com/company/acme"
        )

    def test_linkedin_kind_filtering(self) -> None:
        # A company URL must not be recorded as a person's profile.
        assert normalize_linkedin_url("linkedin.com/company/acme", kind="person") is None
        assert normalize_linkedin_url("linkedin.com/in/ada", kind="company") is None

    def test_linkedin_rejects_non_linkedin(self) -> None:
        assert normalize_linkedin_url("https://acme.com/ada") is None
