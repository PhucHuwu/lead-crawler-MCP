"""Tests for the deduplication stage."""

from __future__ import annotations

import pytest

from src.models.enums import DedupStrategy
from src.processors.deduplicator import Deduplicator, merge_leads
from tests.conftest import make_lead


class TestStrategies:
    def test_none_keeps_everything(self) -> None:
        leads = [make_lead(), make_lead()]
        assert Deduplicator(DedupStrategy.NONE).deduplicate(leads).removed_count == 0

    def test_email_strategy_matches_on_email_only(self) -> None:
        leads = [
            make_lead(email="ada@acme.com"),
            make_lead(email="ada@acme.com", full_name="Ada B Lovelace"),
        ]
        assert Deduplicator(DedupStrategy.EMAIL).deduplicate(leads).removed_count == 1

    def test_email_strategy_ignores_name_matches(self) -> None:
        # Same person, different address: only the aggressive strategy catches it.
        leads = [
            make_lead(email="ada@acme.com"),
            make_lead(email="a.lovelace@acme.com"),
        ]
        assert Deduplicator(DedupStrategy.EMAIL).deduplicate(leads).removed_count == 0

    def test_identity_strategy_matches_on_linkedin(self) -> None:
        leads = [
            make_lead(email="ada@acme.com", linkedin_url="https://www.linkedin.com/in/ada"),
            make_lead(
                email="other@acme.com",
                full_name="Ada Lovelace",
                linkedin_url="https://www.linkedin.com/in/ada",
            ),
        ]
        assert Deduplicator(DedupStrategy.IDENTITY).deduplicate(leads).removed_count == 1

    def test_aggressive_strategy_matches_on_name_and_domain(self) -> None:
        leads = [
            make_lead(email="ada@acme.com"),
            make_lead(email="a.lovelace@acme.com", full_name="Ada Lovelace"),
        ]
        assert Deduplicator(DedupStrategy.AGGRESSIVE).deduplicate(leads).removed_count == 1

    def test_different_people_are_not_merged(self) -> None:
        leads = [
            make_lead(email="ada@acme.com", full_name="Ada Lovelace"),
            make_lead(email="alan@bletchley.com", full_name="Alan Turing"),
        ]
        assert Deduplicator(DedupStrategy.AGGRESSIVE).deduplicate(leads).removed_count == 0


class TestBehaviour:
    def test_preserves_first_seen_order(self) -> None:
        leads = [
            make_lead(email="a@acme.com"),
            make_lead(email="b@acme.com"),
            make_lead(email="a@acme.com"),
        ]
        kept = Deduplicator(DedupStrategy.EMAIL).deduplicate(leads).kept
        assert [lead.person.email for lead in kept] == ["a@acme.com", "b@acme.com"]

    def test_reports_the_matching_key(self) -> None:
        leads = [make_lead(email="ada@acme.com"), make_lead(email="ada@acme.com")]
        pair = Deduplicator(DedupStrategy.EMAIL).deduplicate(leads).duplicates[0]
        assert pair.matched_key == "email"
        assert pair.matched_value == "ada@acme.com"

    def test_records_the_kept_lead_id(self) -> None:
        first, second = make_lead(email="ada@acme.com"), make_lead(email="ada@acme.com")
        outcome = Deduplicator(DedupStrategy.EMAIL).deduplicate([first, second])
        assert outcome.duplicates[0].kept_lead_id == first.lead_id
        assert outcome.duplicates[0].duplicate_lead_id == second.lead_id

    def test_transitive_chain_collapses(self) -> None:
        # a-b linked by email, b-c linked by linkedin: all three become one.
        leads = [
            make_lead(email="ada@acme.com", linkedin_url=None),
            make_lead(email="ada@acme.com", linkedin_url="https://www.linkedin.com/in/ada"),
            make_lead(email="a.lovelace@acme.com", linkedin_url="https://www.linkedin.com/in/ada"),
        ]
        outcome = Deduplicator(DedupStrategy.IDENTITY).deduplicate(leads)
        assert len(outcome.kept) == 1
        assert outcome.removed_count == 2

    def test_empty_input(self) -> None:
        outcome = Deduplicator(DedupStrategy.IDENTITY).deduplicate([])
        assert outcome.kept == []
        assert outcome.removed_count == 0

    def test_lead_with_no_identity_keys_is_kept(self) -> None:
        bare = make_lead(
            email=None,
            full_name=None,
            first_name=None,
            last_name=None,
            company_name=None,
            company_domain=None,
        )
        bare.person.linkedin_url = None
        outcome = Deduplicator(DedupStrategy.AGGRESSIVE).deduplicate([bare, bare])
        assert len(outcome.kept) == 2


class TestFieldMerging:
    def test_missing_fields_are_filled_from_the_duplicate(self) -> None:
        # The whole point of multi-source crawling: the second record completes
        # the first rather than being thrown away.
        primary = make_lead(email="ada@acme.com", phone=None, linkedin_url=None, country=None)
        secondary = make_lead(
            email="ada@acme.com",
            phone="+14155550142",
            linkedin_url="https://www.linkedin.com/in/ada",
            country="CA",
        )
        kept = Deduplicator(DedupStrategy.EMAIL).deduplicate([primary, secondary]).kept[0]
        assert kept.person.phone == "+14155550142"
        assert kept.person.linkedin_url == "https://www.linkedin.com/in/ada"
        assert kept.company.country == "CA"

    def test_primary_values_win(self) -> None:
        primary = make_lead(email="ada@acme.com", full_name="Ada Lovelace")
        secondary = make_lead(email="ada@acme.com", full_name="Augusta King")
        kept = Deduplicator(DedupStrategy.EMAIL).deduplicate([primary, secondary]).kept[0]
        assert kept.person.full_name == "Ada Lovelace"

    def test_merging_can_be_disabled(self) -> None:
        primary = make_lead(email="ada@acme.com", phone=None)
        secondary = make_lead(email="ada@acme.com", phone="+14155550142")
        kept = (
            Deduplicator(DedupStrategy.EMAIL, merge_fields=False)
            .deduplicate([primary, secondary])
            .kept[0]
        )
        assert kept.person.phone is None

    def test_lead_id_survives_merging(self) -> None:
        # The merged lead is the same entity; its id must stay stable.
        primary = make_lead(email="ada@acme.com", phone=None)
        secondary = make_lead(email="ada@acme.com", phone="+14155550142")
        kept = Deduplicator(DedupStrategy.EMAIL).deduplicate([primary, secondary]).kept[0]
        assert kept.lead_id == primary.lead_id

    def test_merged_keys_are_indexed(self) -> None:
        # After merging, the kept lead gains a LinkedIn URL; a later lead sharing
        # only that URL must still be recognized as the same person.
        leads = [
            make_lead(email="ada@acme.com", linkedin_url=None),
            make_lead(email="ada@acme.com", linkedin_url="https://www.linkedin.com/in/ada"),
            make_lead(
                email="a.lovelace@acme.com",
                full_name="Ada Lovelace",
                linkedin_url="https://www.linkedin.com/in/ada",
            ),
        ]
        outcome = Deduplicator(DedupStrategy.IDENTITY).deduplicate(leads)
        assert len(outcome.kept) == 1
        assert outcome.kept[0].person.linkedin_url == "https://www.linkedin.com/in/ada"

    def test_merge_leads_is_a_noop_when_nothing_to_add(self) -> None:
        lead = make_lead()
        assert merge_leads(lead, make_lead()) is lead

    def test_merge_does_not_mutate_either_input(self) -> None:
        primary = make_lead(email="ada@acme.com", phone=None)
        secondary = make_lead(email="ada@acme.com", phone="+14155550142")
        merge_leads(primary, secondary)
        assert primary.person.phone is None

    def test_unknown_seniority_is_treated_as_missing(self) -> None:
        from src.models.enums import SeniorityLevel

        primary = make_lead(email="ada@acme.com")
        primary.person.seniority = SeniorityLevel.UNKNOWN
        secondary = make_lead(email="ada@acme.com")
        secondary.person.seniority = SeniorityLevel.VP
        kept = Deduplicator(DedupStrategy.EMAIL).deduplicate([primary, secondary]).kept[0]
        assert kept.person.seniority is SeniorityLevel.VP


class TestScale:
    @pytest.mark.parametrize("size", [1, 100, 1000])
    def test_handles_larger_inputs_linearly(self, size: int) -> None:
        leads = [make_lead(email=f"user{i}@acme.com", full_name=f"User {i}") for i in range(size)]
        leads.extend(
            make_lead(email=f"user{i}@acme.com", full_name=f"User {i}") for i in range(size)
        )
        outcome = Deduplicator(DedupStrategy.IDENTITY).deduplicate(leads)
        assert len(outcome.kept) == size
        assert outcome.removed_count == size
