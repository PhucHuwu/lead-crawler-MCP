"""Tests for the normalization helpers in :mod:`src.utils`."""

from __future__ import annotations

import io
import json
import logging
import unicodedata
from collections.abc import Iterator
from pathlib import Path

import pytest

from src.utils.errors import ConfigError
from src.utils.logging import configure_logging
from src.utils.names import profile_key
from src.utils.numbers import parse_employee_count
from src.utils.redaction import (
    REDACTED,
    clear_secrets,
    redact,
    register_secrets,
    registered_secret_count,
)
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
    normalize_page_url,
    normalize_website,
    strip_tracking_params,
)
from src.utils.yaml_load import load_yaml_mapping
from tests.fixtures.records import (
    HOSTILE_URLS,
    INVALID_EMAILS,
    MALFORMED_DOMAINS,
    MALFORMED_PAGE_URLS,
    NON_FETCHABLE_URLS,
    UNICODE_HOSTS,
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

    def test_vietnamese_text_is_preserved(self) -> None:
        # The diacritics are the name. Dropping them, or escaping them, is the
        # failure mode this guards against — "Nguyễn" is not "Nguyen".
        assert clean_text("Nguyễn Thị Hương") == "Nguyễn Thị Hương"

    def test_decomposed_text_is_composed(self) -> None:
        # macOS and several APIs hand back NFD: "ễ" as "e" + a combining mark.
        # Unification matters because otherwise one person is two different
        # strings — two CSV rows that never group, two JSON values that never
        # compare equal, and a deduplication key that misses its own twin.
        decomposed = unicodedata.normalize("NFD", "Nguyễn")
        assert decomposed != "Nguyễn"  # the input really is decomposed
        assert clean_text(decomposed) == "Nguyễn"

    def test_combining_marks_are_not_stripped_as_invisible(self) -> None:
        # ``strip_ignorable`` drops Unicode "C" categories; Vietnamese
        # diacritics are "Mn" (nonspacing mark) and must survive it.
        assert clean_text("Hương") == "Hương"

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


class TestEmailCorpus:
    """The full awkward-address corpus, run against :func:`normalize_email`.

    The addresses a real export contains are not the ones a hand-written test
    thinks of: a spreadsheet cell holding two addresses, a cell holding the word
    ``unknown``, a display-name wrapper, an address with a trailing newline.
    Each has a defined outcome, and the ``None`` rows matter most — a
    half-parsed address becomes a *deduplication key*, so a corrupted value is a
    key for a person who does not exist.
    """

    @pytest.mark.parametrize(("raw", "expected"), INVALID_EMAILS)
    def test_expected_outcome(self, raw: str, expected: str | None) -> None:
        assert normalize_email(raw) == expected

    @pytest.mark.parametrize(("raw", "expected"), INVALID_EMAILS)
    def test_a_rejected_address_is_never_a_usable_key(self, raw: str, expected: str | None) -> None:
        # Restating the contract in the terms that matter downstream: anything
        # rejected must be `None`, never a stripped-down fragment. A non-None
        # answer that is not a real address is worse than no answer.
        result = normalize_email(raw)
        if expected is None:
            assert result is None
        else:
            assert result is not None
            assert "@" in result
            assert " " not in result

    def test_a_normalized_address_is_always_lowercase_and_bare(self) -> None:
        for _raw, expected in INVALID_EMAILS:
            if expected is None:
                continue
            assert expected == expected.casefold()
            assert not expected.startswith("mailto:")
            assert "<" not in expected and ">" not in expected

    def test_two_addresses_in_one_cell_are_refused_rather_than_guessed(self) -> None:
        # Picking one would silently attribute the lead to a coin flip.
        assert normalize_email("ada@acme.com,grace@navy.example.com") is None
        assert normalize_email("ada@acme.com grace@navy.example.com") is None

    def test_backslash_and_quoted_forms_do_not_crash(self) -> None:
        # A Windows CSV export escapes this way; the answer matters less than
        # the absence of an exception, since one bad cell must not stop a run.
        for raw in ('"ada@acme.com"', "ada\\@acme.com", "ada@acme.com\\"):
            assert normalize_email(raw) is None or isinstance(normalize_email(raw), str)


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


class TestDomainCorpus:
    """Malformed and non-ASCII hosts, against :func:`normalize_domain`.

    This function *extracts a host*; it is not a fetcher and has no business
    refusing an address. So a private IP comes back as a host here, and the
    refusal that matters for safety lives in :class:`TestPageUrlSafety`.
    """

    @pytest.mark.parametrize(("raw", "expected"), MALFORMED_DOMAINS)
    def test_expected_host(self, raw: str, expected: str | None) -> None:
        assert normalize_domain(raw) == expected

    @pytest.mark.parametrize(("raw", "expected"), MALFORMED_DOMAINS)
    def test_a_returned_domain_is_always_a_bare_lowercase_host(
        self, raw: str, expected: str | None
    ) -> None:
        # A domain is used as a deduplication key and a column value, so scheme,
        # port, path, userinfo and case must all be gone by now.
        result = normalize_domain(raw)
        assert result == expected
        if result is None:
            return
        assert result == result.casefold()
        for fragment in ("://", "/", ":", "?", "#", "@", " "):
            assert fragment not in result, f"{result!r} still carries {fragment!r}"
        assert not result.endswith(".")

    @pytest.mark.parametrize(("raw", "expected"), UNICODE_HOSTS)
    def test_non_ascii_hosts(self, raw: str, expected: str | None) -> None:
        # Pins a limitation rather than a feature: an IDN written in its own
        # script is rejected while the punycode form is accepted, so a company
        # published only as `münchen.de` loses its domain. If IDNA support is
        # ever added, this fails and says so.
        assert normalize_domain(raw) == expected

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


class TestTrackingParams:
    """Campaign parameters are dropped; everything else survives.

    The two failure modes are opposite and both bad: keeping ``utm_source``
    makes one page look like two, and dropping ``?page_id=`` silently points a
    lead at the wrong page.
    """

    @pytest.mark.parametrize(
        "query",
        [
            "utm_source=li&utm_medium=email&utm_campaign=q3",
            "fbclid=abc123",
            "gclid=x&msclkid=y",
            "mc_cid=1&mc_eid=2",
            "pk_campaign=newsletter",
            "_hsenc=abc&_hsmi=def",
            "hsa_cam=1&hsa_grp=2",
        ],
    )
    def test_tracking_families_are_removed(self, query: str) -> None:
        assert strip_tracking_params(query) == ""

    @pytest.mark.parametrize(
        "query",
        ["page_id=12", "department=sales&page_id=3", "lang=en", "v=abc123"],
    )
    def test_meaningful_parameters_survive(self, query: str) -> None:
        assert strip_tracking_params(query) == query

    @pytest.mark.parametrize("query", ["ref=home", "source=careers", "si=xyz"])
    def test_ambiguous_parameters_are_kept(self, query: str) -> None:
        # These are just as often real page selectors as tracking tokens, and a
        # wrong guess rewrites the URL rather than merely tidying it.
        assert strip_tracking_params(query) == query

    def test_mixed_query_keeps_only_the_meaningful_part(self) -> None:
        assert strip_tracking_params("page_id=4&utm_source=x&lang=en") == "page_id=4&lang=en"

    def test_original_order_is_preserved(self) -> None:
        assert strip_tracking_params("b=2&a=1") == "b=2&a=1"

    def test_repeated_parameters_keep_the_first(self) -> None:
        # Two values for one key is a malformed URL; picking one makes the
        # normalized form deterministic instead of order-dependent.
        assert strip_tracking_params("a=1&a=2") == "a=1"

    @pytest.mark.parametrize("query", ["", "&", "&&"])
    def test_empty_queries_yield_nothing(self, query: str) -> None:
        assert strip_tracking_params(query) == ""


class TestPageUrls:
    def test_tracking_parameters_are_stripped(self) -> None:
        assert (
            normalize_page_url("https://acme.com/contact?utm_source=li&utm_campaign=q3")
            == "https://acme.com/contact"
        )

    def test_meaningful_parameters_are_preserved(self) -> None:
        assert (
            normalize_page_url("https://acme.com/contact?department=sales")
            == "https://acme.com/contact?department=sales"
        )

    def test_fragment_is_dropped(self) -> None:
        # A fragment never reaches the server, so it cannot distinguish pages.
        assert normalize_page_url("https://acme.com/contact#team") == "https://acme.com/contact"

    def test_path_and_query_survive_together(self) -> None:
        assert (
            normalize_page_url("http://www.acme.com/about/team?page_id=2#top")
            == "https://acme.com/about/team?page_id=2"
        )

    def test_bare_host_is_unchanged(self) -> None:
        assert normalize_page_url("acme.com/") == "https://acme.com"

    @pytest.mark.parametrize("raw", ["javascript:void(0)", "mailto:a@acme.com", "tel:+1234"])
    def test_non_http_schemes_are_rejected(self, raw: str) -> None:
        assert normalize_page_url(raw) is None


class TestPageUrlSafety:
    """Whether a value may be *fetched* — the security half of URL handling.

    A "website" column is untrusted input, and whatever survives here is handed
    to an HTTP client. Two properties are therefore load-bearing: a scheme that
    executes code must never come back, and neither must a newline, because a
    CRLF that reaches a request line is header injection.
    """

    @pytest.mark.parametrize(("raw", "expected"), MALFORMED_PAGE_URLS)
    def test_expected_page_url(self, raw: str, expected: str | None) -> None:
        assert normalize_page_url(raw) == expected

    @pytest.mark.parametrize("raw", NON_FETCHABLE_URLS)
    def test_a_dangerous_scheme_is_never_fetchable(self, raw: str) -> None:
        assert normalize_page_url(raw) is None

    @pytest.mark.parametrize(
        "raw",
        ["javascript:alert(1)", "JaVaScRiPt:alert(1)", "  javascript:alert(1)  "],
    )
    def test_scheme_rejection_is_case_and_space_insensitive(self, raw: str) -> None:
        # Browsers treat the scheme case-insensitively, so a check that did not
        # would be trivially bypassable by an export that shouted it.
        assert normalize_page_url(raw) is None

    @pytest.mark.parametrize("raw", HOSTILE_URLS)
    def test_hostile_input_never_raises(self, raw: str) -> None:
        # One malformed cell in a 5000-row CSV must not stop the run.
        normalize_page_url(raw)

    @pytest.mark.parametrize(
        "raw",
        [
            "http://acme.com/\r\nHeader: injected",
            "http://acme.com/\n\nGET /admin HTTP/1.1",
            "http://acme.com/a\rb",
        ],
    )
    def test_a_newline_never_survives_into_the_url(self, raw: str) -> None:
        # The injection property, asserted on its own rather than as a side note
        # of the table above: no CR or LF may appear in a fetchable URL.
        result = normalize_page_url(raw)
        if result is not None:
            assert "\r" not in result
            assert "\n" not in result

    @pytest.mark.parametrize("raw", HOSTILE_URLS)
    def test_a_null_byte_never_survives_into_the_url(self, raw: str) -> None:
        # A NUL can truncate a C-level string in a downstream parser, turning a
        # rejected path into an accepted prefix of itself.
        result = normalize_page_url(raw)
        if result is not None:
            assert "\x00" not in result

    def test_userinfo_is_stripped(self) -> None:
        # `user:password@` in a stored URL is a credential sitting in an export
        # that gets emailed around. It is not part of the address of the page.
        assert normalize_page_url("https://user:password@acme.com/secret") == (
            "https://acme.com/secret"
        )

    def test_an_unreasonably_long_host_is_handled_without_crashing(self) -> None:
        # Pinning observed behaviour: there is no length cap, so an overlong
        # host passes through. Recorded rather than asserted as desirable.
        result = normalize_page_url("http://" + "a" * 2000 + ".com/")
        assert result is None or len(result) > 100


class TestProfileKey:
    """Names are typed at a shell and written in YAML, so they are spelled two ways."""

    @pytest.mark.parametrize(
        "spelling", ["singapore_tech", "singapore-tech", "Singapore Tech", "SINGAPORE_TECH"]
    )
    def test_every_spelling_of_a_name_reaches_the_same_key(self, spelling: str) -> None:
        assert profile_key(spelling) == "singapore_tech"

    def test_surrounding_whitespace_is_ignored(self) -> None:
        assert profile_key("  sea_fintech\n") == "sea_fintech"

    def test_repeated_separators_collapse(self) -> None:
        # `sea__fintech` is a typo, not a different profile, and a failed run is
        # a poor way to learn that.
        assert profile_key("sea__fintech") == "sea_fintech"

    def test_separators_join_words_but_do_not_invent_them(self) -> None:
        # `default` is one word: inserting a separator makes a different name and
        # must not silently resolve to the original.
        assert profile_key("de-fault") == "de_fault"


class TestYamlLoading:
    """The shared loader behind both profile files.

    Every one of these is a failure mode that would otherwise be silent: a
    duplicate key would keep the last definition, and a mistyped document would
    load as "no profiles" rather than as the mistake it is.
    """

    def _load(self, tmp_path: Path, body: str) -> dict[object, object]:
        path = tmp_path / "doc.yaml"
        path.write_text(body, encoding="utf-8")
        return load_yaml_mapping(path, label="widget profiles", env_var="LEAD_WIDGETS_PATH")

    def test_a_valid_document_loads(self, tmp_path: Path) -> None:
        assert self._load(tmp_path, "a:\n  x: 1\nb:\n  y: 2\n") == {
            "a": {"x": 1},
            "b": {"y": 2},
        }

    def test_a_duplicate_key_is_rejected(self, tmp_path: Path) -> None:
        # YAML keeps the last one, so an edited-and-pasted profile would silently
        # discard half of what its author wrote.
        with pytest.raises(ConfigError, match="duplicate key 'a'"):
            self._load(tmp_path, "a:\n  x: 1\na:\n  y: 2\n")

    def test_the_duplicate_error_carries_a_line_number(self, tmp_path: Path) -> None:
        # The file is not open in front of whoever reads the error.
        with pytest.raises(ConfigError, match=r"line 3"):
            self._load(tmp_path, "a:\n  x: 1\na:\n  y: 2\n")

    def test_an_unhashable_key_is_reported_rather_than_crashing(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="not usable as a name"):
            self._load(tmp_path, "? [a, b]\n: 1\n")

    def test_a_missing_file_names_the_setting_and_the_path(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError) as excinfo:
            load_yaml_mapping(
                tmp_path / "absent.yaml", label="widget profiles", env_var="LEAD_WIDGETS_PATH"
            )
        message = str(excinfo.value)
        assert "widget profiles file not found" in message
        assert "LEAD_WIDGETS_PATH" in message

    def test_invalid_yaml_is_reported_with_a_location(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="not valid YAML"):
            self._load(tmp_path, "a: [unclosed\n")

    def test_an_empty_document_is_reported(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="contains no widget profiles"):
            self._load(tmp_path, "")

    def test_a_comment_only_document_is_reported(self, tmp_path: Path) -> None:
        # YAML reads this as `None`, which must not be mistaken for a document
        # that happens to define nothing.
        with pytest.raises(ConfigError, match="contains no widget profiles"):
            self._load(tmp_path, "# nothing but a note\n")

    def test_a_non_mapping_document_names_what_it_got_instead(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="must be a mapping of names to settings, got list"):
            self._load(tmp_path, "- a\n- b\n")


class TestSecretRedaction:
    """The mechanism behind the "never log a credential" invariant.

    :class:`~pydantic.SecretStr` keeps a secret out of ``repr`` and out of
    serialized settings; it cannot stop an adapter from logging a whole request,
    or an error message built from a URL carrying a token. These tests cover the
    net that catches that: values registered at settings-load time are removed
    from every log record before it is rendered.
    """

    @pytest.fixture(autouse=True)
    def _clean_registry(self) -> Iterator[None]:
        """The registry is process-wide; a leaked fake key would redact later tests."""
        clear_secrets()
        yield
        clear_secrets()

    def test_a_registered_secret_is_replaced(self) -> None:
        register_secrets(["sk-live-abcdef123456"])
        assert redact("key=sk-live-abcdef123456") == f"key={REDACTED}"

    def test_a_secret_inside_a_longer_string_is_replaced(self) -> None:
        register_secrets(["sk-live-abcdef123456"])
        assert redact("sk-live-abcdef123456.extra") == f"{REDACTED}.extra"

    def test_several_secrets_are_all_replaced(self) -> None:
        register_secrets(["sk-live-abcdef123456", "tok-second-0987654321"])
        assert (
            redact("a sk-live-abcdef123456 b tok-second-0987654321") == f"a {REDACTED} b {REDACTED}"
        )

    def test_the_longest_match_wins(self) -> None:
        # Otherwise the shorter secret rewrites the longer one's prefix and
        # leaves its tail sitting in the log.
        register_secrets(["abcdefgh", "abcdefghijkl"])
        assert redact("abcdefghijkl") == REDACTED

    def test_short_values_are_ignored(self) -> None:
        # A one-character "secret" would match half the log and destroy the
        # record it is meant to protect.
        register_secrets(["x", ""])
        assert registered_secret_count() == 0
        assert redact("x marks the spot") == "x marks the spot"

    def test_blank_values_are_ignored(self) -> None:
        register_secrets([None, ""])
        assert registered_secret_count() == 0

    def test_registration_is_idempotent(self) -> None:
        register_secrets(["sk-live-abcdef123456"] * 3)
        assert registered_secret_count() == 1

    def test_non_strings_pass_through_untouched(self) -> None:
        # The filter applies this to every field of every record, including
        # counts and paths; only text can carry a secret.
        register_secrets(["sk-live-abcdef123456"])
        assert redact(42) == 42
        assert redact(None) is None
        assert redact(["sk-live-abcdef123456"]) == ["sk-live-abcdef123456"]

    def test_nothing_is_rewritten_when_nothing_is_registered(self) -> None:
        assert redact("sk-live-abcdef123456") == "sk-live-abcdef123456"

    def test_an_unregistered_string_is_untouched(self) -> None:
        register_secrets(["sk-live-abcdef123456"])
        assert redact("ada@acme.com") == "ada@acme.com"


class TestLogRedaction:
    """End-to-end: a registered secret must not survive rendering."""

    SECRET = "sk-live-abcdef123456"

    @pytest.fixture(autouse=True)
    def _clean_registry_and_logging(self) -> Iterator[None]:
        """Restore the process-wide registry and the root handlers afterwards."""
        root = logging.getLogger()
        saved_handlers, saved_level = list(root.handlers), root.level
        clear_secrets()
        yield
        clear_secrets()
        for handler in list(root.handlers):
            root.removeHandler(handler)
        for handler in saved_handlers:
            root.addHandler(handler)
        root.setLevel(saved_level)

    @staticmethod
    def _capture(fmt: str) -> io.StringIO:
        stream = io.StringIO()
        configure_logging("INFO", fmt, stream=stream)
        return stream

    def test_a_secret_in_the_message_is_scrubbed(self) -> None:
        stream = self._capture("console")
        register_secrets([self.SECRET])
        logging.getLogger("test").info("calling with key %s", self.SECRET)

        output = stream.getvalue()
        assert self.SECRET not in output
        assert REDACTED in output

    def test_a_secret_in_an_extra_field_is_scrubbed(self) -> None:
        # The route a careless `extra={"headers": ...}` would take.
        stream = self._capture("console")
        register_secrets([self.SECRET])
        logging.getLogger("test").info("request", extra={"authorization": f"Bearer {self.SECRET}"})

        assert self.SECRET not in stream.getvalue()

    def test_a_secret_in_a_traceback_is_scrubbed(self) -> None:
        stream = self._capture("console")
        register_secrets([self.SECRET])
        try:
            raise RuntimeError(f"rejected credentials {self.SECRET}")
        except RuntimeError:
            logging.getLogger("test").error("request failed", exc_info=True)

        output = stream.getvalue()
        assert self.SECRET not in output
        assert REDACTED in output
        assert "RuntimeError" in output  # the traceback still says what happened

    def test_json_output_is_scrubbed_too(self) -> None:
        # Both formatters get the filter from the handler, so neither can drift
        # into leaking what the other withholds.
        stream = self._capture("json")
        register_secrets([self.SECRET])
        try:
            raise RuntimeError(f"rejected credentials {self.SECRET}")
        except RuntimeError:
            logging.getLogger("test").error(
                "request failed", exc_info=True, extra={"key": self.SECRET}
            )

        payload = json.loads(stream.getvalue())
        assert self.SECRET not in stream.getvalue()
        assert payload["key"] == REDACTED
        assert self.SECRET not in payload["exception"]

    def test_exc_info_false_does_not_break_the_filter(self) -> None:
        # The pipeline passes a *computed boolean* for exc_info, so a falsy value
        # reaches the record as `False` rather than None. A filter that assumed
        # a tuple would raise here and take the log line with it.
        stream = self._capture("console")
        logging.getLogger("test").error("source failed", exc_info=False)

        output = stream.getvalue()
        assert "source failed" in output
        assert "Traceback" not in output

    def test_an_exception_instance_is_rendered_and_scrubbed(self) -> None:
        # `exc_info` also accepts an exception object, which logging does not
        # normalise into a tuple before the record is built.
        stream = self._capture("console")
        register_secrets([self.SECRET])
        logging.getLogger("test").error("failed", exc_info=RuntimeError(f"bad {self.SECRET}"))

        output = stream.getvalue()
        assert self.SECRET not in output
        assert REDACTED in output

    def test_a_record_with_nothing_to_redact_is_unchanged(self) -> None:
        stream = self._capture("console")
        logging.getLogger("test").info("collected leads", extra={"count": 3})

        output = stream.getvalue()
        assert "collected leads" in output
        assert "count=3" in output
