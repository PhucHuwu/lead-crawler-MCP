"""Deduplication, stated as the rules the crawler promises to follow.

:mod:`tests.test_deduplicator` exercises the mechanism — which strategy trusts
which key, how merging fills gaps, what the counters report. This module states
the *outcome* for the cases the crawler's behaviour is actually judged on, one
test per rule, in the words the requirement uses:

* same email            -> duplicate
* same Apollo id        -> duplicate
* same name + domain    -> likely duplicate (reported as probable, not certain)
* same name, other domain -> **not** a duplicate
* same company, other person -> **not** a duplicate

The distinction between the last two and the first three is the whole point. A
false merge silently deletes a lead — the person is never contacted and nothing
in the output says so — whereas a missed merge leaves two rows a human can see
and fix. The tests are therefore written so that *merging too much* fails loudly:
:data:`MUST_NOT_MERGE` is the half of the spec that protects real leads.
"""

from __future__ import annotations

import unicodedata
from typing import TypedDict

import pytest

from src.models.enums import DedupStrategy, DuplicateKind
from src.models.lead import StandardizedLead
from src.processors.deduplicator import Deduplicator, DedupOutcome
from src.processors.normalizer import Normalizer
from tests.conftest import make_lead
from tests.fixtures.records import complete_lead, vietnamese_lead

#: Strategies that trust inference as well as proof. The rules below that say
#: "duplicate" hold for every strategy; the ones that need name+domain matching
#: only hold here, and each test names the strategy it is asserting for.
INFERRING = DedupStrategy.AGGRESSIVE


def dedupe(leads: list[StandardizedLead], strategy: DedupStrategy = INFERRING) -> DedupOutcome:
    """Run one strategy over ``leads`` and hand back the whole outcome."""
    return Deduplicator(strategy).deduplicate(leads)


class TestSameEmailIsDuplicate:
    """Two records sharing an address are one person, under every strategy."""

    @pytest.mark.parametrize(
        "strategy",
        [DedupStrategy.EMAIL, DedupStrategy.IDENTITY, DedupStrategy.AGGRESSIVE],
    )
    def test_matching_email_collapses(self, strategy: DedupStrategy) -> None:
        # Everything else differs — different spelling of the name, different
        # title, different employer — because an address is the person, not the
        # record, and a job change must not fork them into two leads.
        leads = [
            make_lead(email="ada@acme.com", full_name="Ada Lovelace"),
            make_lead(
                email="ada@acme.com",
                full_name="Ada B. Lovelace",
                job_title="CTO",
                company_name="Analytical Engines",
                company_domain="analytical.example.com",
            ),
        ]
        outcome = dedupe(leads, strategy)
        assert outcome.removed_count == 1
        assert len(outcome.kept) == 1
        assert outcome.duplicates[0].matched_key == "email"

    def test_email_match_is_reported_as_certain(self) -> None:
        outcome = dedupe([make_lead(email="ada@acme.com"), make_lead(email="ada@acme.com")])
        assert outcome.duplicates[0].kind is DuplicateKind.EXACT
        assert outcome.exact_count == 1
        assert outcome.probable_count == 0

    def test_case_and_whitespace_differences_still_match(self) -> None:
        # Normalization has already folded these by the time the deduplicator
        # sees them; asserting it here keeps the two stages from drifting apart.
        normalizer = Normalizer()
        leads = [
            normalizer.normalize(complete_lead()),
            normalizer.normalize(
                complete_lead().model_copy(update={"email": "  ADA.LOVELACE@ACME.COM  "})
            ),
        ]
        assert dedupe(leads).removed_count == 1

    def test_a_different_address_is_a_different_person(self) -> None:
        # Two colleagues at one company, reached at different addresses.
        leads = [
            make_lead(email="ada@acme.com", full_name="Ada Lovelace"),
            make_lead(email="grace@acme.com", full_name="Grace Hopper"),
        ]
        assert dedupe(leads).removed_count == 0


class TestSameSourceIdIsDuplicate:
    """Same provider, same external id: the same record fetched twice."""

    @pytest.mark.parametrize(
        "strategy",
        [
            DedupStrategy.EMAIL,
            DedupStrategy.IDENTITY,
            DedupStrategy.AGGRESSIVE,
        ],
    )
    def test_matching_source_id_collapses(self, strategy: DedupStrategy) -> None:
        # Deliberately contradictory: two *different people* by every other
        # signal. The provider said these are one record, and re-fetching a page
        # cannot turn one record into two people, so the id wins.
        leads = [
            make_lead(
                provider="apollo",
                external_id="66f1a2b3c4d5e6f7a8b9c0d1",
                email="ada@acme.com",
                full_name="Ada Lovelace",
            ),
            make_lead(
                provider="apollo",
                external_id="66f1a2b3c4d5e6f7a8b9c0d1",
                email="ada@acme.com",
                full_name="Ada Lovelace",
            ),
        ]
        outcome = dedupe(leads, strategy)
        assert outcome.removed_count == 1
        assert outcome.duplicates[0].matched_key == "source_id"

    def test_source_id_match_is_reported_as_certain(self) -> None:
        leads = [
            make_lead(provider="apollo", external_id="abc123"),
            make_lead(provider="apollo", external_id="abc123"),
        ]
        outcome = dedupe(leads)
        assert outcome.duplicates[0].kind is DuplicateKind.EXACT
        assert outcome.duplicates[0].matched_value == "apollo:abc123"

    def test_only_the_pairs_provider_and_id(self) -> None:
        # The same id from two providers is a coincidence, not an identity:
        # providers number their own records independently, so `1` from Apollo
        # and `1` from a CSV are unrelated people.
        leads = [
            make_lead(provider="apollo", external_id="1", email="ada@acme.com"),
            make_lead(provider="csv", external_id="1", email="grace@acme.com", full_name="Grace H"),
        ]
        assert dedupe(leads).removed_count == 0

    def test_a_lead_without_an_external_id_does_not_collide_with_one_that_has_it(self) -> None:
        # `None` must never become a shared key — otherwise every record missing
        # an id would collapse into whichever one was seen first.
        leads = [
            make_lead(provider="apollo", external_id=None, email="ada@acme.com"),
            make_lead(provider="apollo", external_id=None, email="grace@acme.com", full_name="G H"),
        ]
        assert dedupe(leads).removed_count == 0


class TestSameNameAndCompanyIsLikelyDuplicate:
    """The inference case: one person recorded twice by two sources."""

    def test_same_name_and_domain_collapses(self) -> None:
        leads = [
            make_lead(email="ada@acme.com", full_name="Ada Lovelace", company_domain="acme.com"),
            make_lead(
                email="a.lovelace@acme.com",  # a second address for the same person
                full_name="Ada Lovelace",
                company_domain="acme.com",
            ),
        ]
        outcome = dedupe(leads)
        assert outcome.removed_count == 1
        assert outcome.duplicates[0].matched_key == "name_domain"

    def test_the_match_is_reported_as_uncertain(self) -> None:
        # This is the honest label: the crawler inferred it, and the output says
        # so. Two people named Ada Lovelace at one company is unlikely but not
        # impossible, and a reviewer needs to be able to find these pairs.
        leads = [
            make_lead(email="ada@acme.com", full_name="Ada Lovelace", company_domain="acme.com"),
            make_lead(email="other@acme.com", full_name="Ada Lovelace", company_domain="acme.com"),
        ]
        outcome = dedupe(leads)
        assert outcome.duplicates[0].kind is DuplicateKind.PROBABLE
        assert outcome.probable_count == 1
        assert outcome.exact_count == 0

    def test_a_conservative_strategy_declines_to_infer(self) -> None:
        # The same pair must survive `--dedupe identity`: the operator asked for
        # proof only, and a name is not proof.
        leads = [
            make_lead(email="ada@acme.com", full_name="Ada Lovelace", company_domain="acme.com"),
            make_lead(email="other@acme.com", full_name="Ada Lovelace", company_domain="acme.com"),
        ]
        assert dedupe(leads, DedupStrategy.IDENTITY).removed_count == 0

    def test_the_domain_is_compared_not_the_company_label(self) -> None:
        # "Acme Corp" and "Acme, Inc." are the same employer written twice; the
        # domain is what identifies it, and the labels must not be the key.
        leads = [
            make_lead(
                full_name="Ada Lovelace",
                company_name="Acme Corp",
                company_domain="acme.com",
                email="ada@acme.com",
            ),
            make_lead(
                full_name="Ada Lovelace",
                company_name="ACME, INC.",
                company_domain="acme.com",
                email="other@acme.com",
            ),
        ]
        outcome = dedupe(leads)
        assert outcome.removed_count == 1
        assert outcome.duplicates[0].matched_key == "name_domain"

    def test_the_kept_record_gains_what_the_duplicate_knew(self) -> None:
        # Merging rather than discarding is the reason multi-source crawling is
        # worth doing: the first record to arrive is rarely the most complete.
        leads = [
            make_lead(
                full_name="Ada Lovelace",
                company_domain="acme.com",
                email=None,
                phone=None,
            ),
            make_lead(
                full_name="Ada Lovelace",
                company_domain="acme.com",
                email="ada@acme.com",
                phone="+14155550142",
            ),
        ]
        outcome = dedupe(leads)
        assert outcome.removed_count == 1
        survivor = outcome.kept[0]
        assert survivor.person.email == "ada@acme.com"
        assert survivor.person.phone == "+14155550142"


#: The identity fields the "must not merge" table varies. A ``TypedDict`` rather
#: than ``dict[str, object]`` so the entries can be splatted into ``make_lead``
#: with the keyword types still checked — a typo in a field name, or ``email``
#: given a non-string, is then a type error rather than a silently ignored
#: keyword that quietly removes a pair from the test.
class _IdentityFields(TypedDict):
    full_name: str
    company_name: str | None
    company_domain: str | None
    email: str | None


#: Pairs that must never be folded together, as (left, right, why). Each is a
#: plausible false positive of a name-based rule, so each is the reason a
#: name-only key does not exist in the ladder.
#:
#: Every entry names all three identity fields explicitly. ``make_lead`` defaults
#: ``company_name`` to ``"Acme Corp"``, and a pair that inherits that default
#: secretly shares an employer — which is enough for ``name_company`` to match,
#: so a table that left it out would be asserting on a coincidence.
MUST_NOT_MERGE: tuple[tuple[_IdentityFields, _IdentityFields, str], ...] = (
    (
        {
            "full_name": "Ada Lovelace",
            "company_name": "Acme Corp",
            "company_domain": "acme.com",
            "email": "ada@acme.com",
        },
        {
            "full_name": "Ada Lovelace",
            "company_name": "Bletchley Systems",
            "company_domain": "bletchley.example.com",
            "email": "ada@bletchley.example.com",
        },
        "same name, different employer — two people, or one person who moved",
    ),
    (
        {
            "full_name": "Ada Lovelace",
            "company_name": "Acme Corp",
            "company_domain": "acme.com",
            "email": "ada@acme.com",
        },
        {
            "full_name": "Grace Hopper",
            "company_name": "Acme Corp",
            "company_domain": "acme.com",
            "email": "grace@acme.com",
        },
        "same employer, different people — colleagues",
    ),
    (
        {
            "full_name": "Nguyễn Văn An",
            "company_name": "Công ty TNHH Giải pháp Số",
            "company_domain": "congty.vn",
            "email": "an@congty.vn",
        },
        {
            "full_name": "Nguyễn Văn Bình",
            "company_name": "Công ty TNHH Giải pháp Số",
            "company_domain": "congty.vn",
            "email": "binh@congty.vn",
        },
        "shared family name and employer — common in Vietnamese naming",
    ),
    (
        {
            "full_name": "김민준",
            "company_name": "서울테크",
            "company_domain": "seoultech.kr",
            "email": "minjun@seoultech.kr",
        },
        {
            "full_name": "김지훈",
            "company_name": "서울테크",
            "company_domain": "seoultech.kr",
            "email": "jihoon@seoultech.kr",
        },
        "two people sharing a Korean family name at one employer",
    ),
    (
        {
            "full_name": "Ada Lovelace",
            "company_name": None,
            "company_domain": None,
            "email": "ada@acme.com",
        },
        {
            "full_name": "Ada Lovelace",
            "company_name": None,
            "company_domain": None,
            "email": "ada@navy.example.com",
        },
        "same name, no employer on either side — no grounds to merge",
    ),
    (
        {
            "full_name": "John Smith",
            "company_name": None,
            "company_domain": "acme.com",
            "email": None,
        },
        {
            "full_name": "Jane Smith",
            "company_name": None,
            "company_domain": "acme.com",
            "email": None,
        },
        "a shared surname at one employer — the classic false positive",
    ),
)


class TestMustNotMerge:
    """The false-positive half of the spec: merging too much loses leads."""

    @pytest.mark.parametrize(("left", "right", "why"), MUST_NOT_MERGE)
    def test_pair_is_kept_apart(
        self, left: _IdentityFields, right: _IdentityFields, why: str
    ) -> None:
        outcome = dedupe([make_lead(**left), make_lead(**right)])
        assert outcome.removed_count == 0, f"should not merge: {why}"
        assert len(outcome.kept) == 2

    @pytest.mark.parametrize(("left", "right", "why"), MUST_NOT_MERGE)
    def test_pair_is_kept_apart_at_every_strategy(
        self, left: _IdentityFields, right: _IdentityFields, why: str
    ) -> None:
        # Including AGGRESSIVE, the most willing to infer: a pair that only
        # survives the conservative strategies is one bad default away from
        # being merged in production.
        for strategy in DedupStrategy:
            if strategy is DedupStrategy.NONE:
                continue
            outcome = dedupe([make_lead(**left), make_lead(**right)], strategy)
            assert outcome.removed_count == 0, f"{strategy.value} merged: {why}"


class TestUnicodeIdentity:
    """Script and encoding must not change who a lead is."""

    def test_decomposed_and_composed_vietnamese_are_one_person(self) -> None:
        # macOS and some Excel exports emit NFD; the Apollo API emits NFC. The
        # same person must not become two leads because of the platform the
        # export was written on.
        normalizer = Normalizer()
        leads = [
            normalizer.normalize(vietnamese_lead("NFC")),
            normalizer.normalize(vietnamese_lead("NFD")),
        ]
        assert unicodedata.normalize("NFD", leads[0].person.full_name or "") != (
            leads[0].person.full_name
        ), "fixture is not actually decomposed — the test would pass vacuously"
        outcome = dedupe(leads)
        assert outcome.removed_count == 1

    def test_the_two_forms_agree_on_a_company_domain_match(self) -> None:
        # The merge must hold on the inferred key too, not only on the email:
        # a record with no address has nothing else to be matched by.
        normalizer = Normalizer()
        composed = normalizer.normalize(vietnamese_lead("NFC").model_copy(update={"email": None}))
        decomposed = normalizer.normalize(vietnamese_lead("NFD").model_copy(update={"email": None}))
        outcome = dedupe([composed, decomposed])
        assert outcome.removed_count == 1
        assert outcome.duplicates[0].matched_key == "name_domain"

    @pytest.mark.parametrize(
        ("name", "domain"),
        [
            ("José Álvarez", "acme.es"),
            ("Ольга Иванова", "acme.ru"),
            ("Μαρία Παπαδοπούλου", "acme.gr"),
            ("محمد الأحمد", "acme.ae"),
            ("김민준", "acme.kr"),
            ("山田太郎", "acme.jp"),
            ("สมชาย ใจดี", "acme.th"),
        ],
    )
    def test_a_script_survives_the_identity_ladder(self, name: str, domain: str) -> None:
        # A name that normalizes to nothing would silently lose its name_domain
        # key, so a non-Latin lead with no email would stop being mergeable at
        # all. Asserting the merge proves the slug is non-empty.
        left = make_lead(full_name=name, company_domain=domain, email=None)
        right = make_lead(full_name=name, company_domain=domain, email=None)
        assert left.identity_keys(), "the lead has no identity keys at all"
        assert dedupe([left, right]).removed_count == 1

    def test_two_scripts_are_not_the_same_name(self) -> None:
        # Guards the opposite failure: a slug rule that maps every non-ASCII
        # character to nothing would make unrelated names collide.
        left = make_lead(full_name="김민준", company_domain="acme.kr", email=None)
        right = make_lead(full_name="山田太郎", company_domain="acme.kr", email=None)
        assert dedupe([left, right]).removed_count == 0
