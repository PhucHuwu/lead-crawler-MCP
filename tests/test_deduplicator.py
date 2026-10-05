"""Tests for the deduplication stage."""

from __future__ import annotations

import pytest

from src.models.enums import DedupStrategy, DuplicateKind
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


class TestSourceId:
    """``source + external_id`` heads the ladder, and is scoped by provider."""

    def test_the_same_source_record_collapses(self) -> None:
        leads = [
            make_lead(email="ada@acme.com", provider="apollo", external_id="p1"),
            make_lead(email="a.lovelace@acme.com", provider="apollo", external_id="p1"),
        ]
        outcome = Deduplicator(DedupStrategy.EMAIL).deduplicate(leads)
        assert outcome.removed_count == 1
        assert outcome.duplicates[0].matched_key == "source_id"

    def test_the_source_id_wins_over_a_weaker_email_match(self) -> None:
        # Both records share an email, but the ladder must report the proof.
        leads = [
            make_lead(email="ada@acme.com", provider="apollo", external_id="p1"),
            make_lead(email="ada@acme.com", provider="apollo", external_id="p1"),
        ]
        pair = Deduplicator(DedupStrategy.EMAIL).deduplicate(leads).duplicates[0]
        assert pair.matched_key == "source_id"
        assert pair.matched_value == "apollo:p1"

    def test_ids_from_different_providers_do_not_collide(self) -> None:
        # Two sources using the same id space is normal, not a duplicate.
        leads = [
            make_lead(email="ada@acme.com", provider="apollo", external_id="1"),
            make_lead(
                email="alan@other.com", full_name="Alan Turing", provider="csv", external_id="1"
            ),
        ]
        assert Deduplicator(DedupStrategy.AGGRESSIVE).deduplicate(leads).removed_count == 0

    def test_a_source_id_is_proof(self) -> None:
        leads = [
            make_lead(provider="apollo", external_id="p1"),
            make_lead(email=None, provider="apollo", external_id="p1"),
        ]
        pair = Deduplicator(DedupStrategy.EMAIL).deduplicate(leads).duplicates[0]
        assert pair.kind is DuplicateKind.EXACT


class TestDuplicateKind:
    """Proof versus inference, and the counters derived from it."""

    def test_an_email_match_is_exact(self) -> None:
        leads = [make_lead(email="ada@acme.com"), make_lead(email="ada@acme.com")]
        pair = Deduplicator(DedupStrategy.EMAIL).deduplicate(leads).duplicates[0]
        assert pair.kind is DuplicateKind.EXACT

    def test_a_linkedin_match_is_exact(self) -> None:
        url = "https://www.linkedin.com/in/ada"
        leads = [
            make_lead(email="ada@acme.com", linkedin_url=url),
            make_lead(email="other@acme.com", linkedin_url=url),
        ]
        pair = Deduplicator(DedupStrategy.IDENTITY).deduplicate(leads).duplicates[0]
        assert pair.kind is DuplicateKind.EXACT

    def test_a_phone_and_name_match_is_only_probable(self) -> None:
        # A shared switchboard number is not proof; the name only narrows it.
        leads = [
            make_lead(email="ada@acme.com", phone="+14155550142"),
            make_lead(email="a.lovelace@acme.com", phone="+14155550142"),
        ]
        pair = Deduplicator(DedupStrategy.IDENTITY).deduplicate(leads).duplicates[0]
        assert pair.matched_key == "phone_name"
        assert pair.kind is DuplicateKind.PROBABLE

    def test_a_name_and_company_match_is_only_probable(self) -> None:
        leads = [
            make_lead(email="ada@acme.com"),
            make_lead(email="a.lovelace@acme.com"),
        ]
        pair = Deduplicator(DedupStrategy.AGGRESSIVE).deduplicate(leads).duplicates[0]
        assert pair.matched_key == "name_domain"
        assert pair.kind is DuplicateKind.PROBABLE

    def test_the_counters_split_the_removals(self) -> None:
        leads = [
            # exact pair
            make_lead(email="ada@acme.com", full_name="Ada Lovelace", phone=None),
            make_lead(email="ada@acme.com", full_name="Ada Lovelace", phone=None),
            # probable pair: name + domain, distinct addresses
            make_lead(email="grace@navy.example.com", full_name="Grace Hopper"),
            make_lead(email="g.hopper@navy.example.com", full_name="Grace Hopper"),
        ]
        outcome = Deduplicator(DedupStrategy.AGGRESSIVE).deduplicate(leads)
        assert outcome.considered == 4
        assert outcome.exact_count == 1
        assert outcome.probable_count == 1
        assert outcome.removed_count == 2
        assert len(outcome.kept) == 2

    def test_considered_counts_the_input_before_collapsing(self) -> None:
        leads = [make_lead(email="a@acme.com"), make_lead(email="a@acme.com")]
        outcome = Deduplicator(DedupStrategy.EMAIL).deduplicate(leads)
        assert outcome.considered == 2
        assert len(outcome.kept) + outcome.removed_count == outcome.considered

    def test_considered_is_zero_for_no_input(self) -> None:
        outcome = Deduplicator(DedupStrategy.IDENTITY).deduplicate([])
        assert outcome.considered == 0
        assert outcome.exact_count == 0
        assert outcome.probable_count == 0

    def test_the_strategy_none_pass_reports_its_input(self) -> None:
        outcome = Deduplicator(DedupStrategy.NONE).deduplicate([make_lead(), make_lead()])
        assert outcome.considered == 2
        assert outcome.removed_count == 0

    def test_the_duplicate_provider_is_recorded(self) -> None:
        # The merge keeps the primary's external_id, so without this the fact
        # that a second source contributed would be untraceable.
        leads = [
            make_lead(email="ada@acme.com", provider="apollo"),
            make_lead(email="ada@acme.com", provider="company_website"),
        ]
        pair = Deduplicator(DedupStrategy.EMAIL).deduplicate(leads).duplicates[0]
        assert pair.duplicate_provider == "company_website"


class TestNameIsNotAnIdentity:
    """The prompt's hard rule: a shared name must never *prove* two people equal."""

    def test_the_same_name_at_different_companies_is_not_a_match(self) -> None:
        leads = [
            make_lead(
                email="john.smith@acme.com",
                full_name="John Smith",
                company_name="Acme Corp",
                company_domain="acme.com",
            ),
            make_lead(
                email="john.smith@globex.com",
                full_name="John Smith",
                company_name="Globex",
                company_domain="globex.com",
            ),
        ]
        assert Deduplicator(DedupStrategy.AGGRESSIVE).deduplicate(leads).removed_count == 0

    def test_two_people_at_one_company_are_not_merged(self) -> None:
        # A shared surname at one employer is the weakest possible signal, and
        # the ladder deliberately contains no such key at all — the removed
        # `lastname_domain` key used to merge these two.
        leads = [
            make_lead(email="a@acme.com", full_name="Ada Smith"),
            make_lead(email="b@acme.com", full_name="Bob Smith"),
        ]
        assert Deduplicator(DedupStrategy.AGGRESSIVE).deduplicate(leads).removed_count == 0

    def test_a_shared_name_at_one_company_is_never_proof(self) -> None:
        # It *is* a probable match, and that is the point of the kind split: the
        # report says the collapse was inferred, not proven, so a reviewer knows
        # which merges to check by hand.
        leads = [
            make_lead(email="john.smith@acme.com", full_name="John Smith"),
            make_lead(email="j.smith@acme.com", full_name="John Smith"),
        ]
        pair = Deduplicator(DedupStrategy.AGGRESSIVE).deduplicate(leads).duplicates[0]
        assert pair.matched_key == "name_domain"
        assert pair.kind is DuplicateKind.PROBABLE

    def test_a_name_alone_merges_nothing(self) -> None:
        # No domain to anchor it: name_company still needs the company name, so
        # strip that too and the two records share no key at all.
        leads = [
            make_lead(email=None, full_name="John Smith", company_name=None, company_domain=None),
            make_lead(email=None, full_name="John Smith", company_name=None, company_domain=None),
        ]
        leads[0].person.linkedin_url = leads[1].person.linkedin_url = None
        assert Deduplicator(DedupStrategy.AGGRESSIVE).deduplicate(leads).removed_count == 0

    def test_the_ladder_has_no_bare_name_key(self) -> None:
        keys = make_lead(full_name="Ada Lovelace").identity_keys()
        assert "lastname_domain" not in keys
        # Every remaining key is anchored to something beyond the name alone.
        assert set(keys) <= {
            "source_id",
            "email",
            "linkedin",
            "phone_name",
            "name_domain",
            "name_company",
        }


class TestProvenance:
    def test_duplicate_matches_are_reported_in_order(self) -> None:
        leads = [
            make_lead(email="ada@acme.com"),
            make_lead(email="ada@acme.com"),
            make_lead(email="ada@acme.com"),
        ]
        outcome = Deduplicator(DedupStrategy.EMAIL).deduplicate(leads)
        assert [pair.kept_lead_id for pair in outcome.duplicates] == [leads[0].lead_id] * 2
        assert [pair.duplicate_lead_id for pair in outcome.duplicates] == [
            leads[1].lead_id,
            leads[2].lead_id,
        ]

    def test_merging_unions_the_source_list(self) -> None:
        # Collapsing two records must not erase the fact that a second source
        # contributed — that is the one thing the merge would otherwise destroy.
        primary = make_lead(email="ada@acme.com", provider="apollo")
        secondary = make_lead(email="ada@acme.com", provider="company_website")
        kept = Deduplicator(DedupStrategy.EMAIL).deduplicate([primary, secondary]).kept[0]
        assert kept.source.sources == ["apollo", "company_website"]

    def test_the_primary_provider_stays_primary(self) -> None:
        primary = make_lead(email="ada@acme.com", provider="apollo")
        secondary = make_lead(email="ada@acme.com", provider="company_website")
        kept = Deduplicator(DedupStrategy.EMAIL).deduplicate([primary, secondary]).kept[0]
        assert kept.source.provider == "apollo"
        assert kept.source.sources[0] == "apollo"

    def test_merging_three_sources_keeps_all_three(self) -> None:
        leads = [
            make_lead(email="ada@acme.com", provider="apollo"),
            make_lead(email="ada@acme.com", provider="company_website"),
            make_lead(email="ada@acme.com", provider="csv"),
        ]
        kept = Deduplicator(DedupStrategy.EMAIL).deduplicate(leads).kept[0]
        assert kept.source.sources == ["apollo", "company_website", "csv"]

    def test_the_single_source_of_a_non_duplicate_is_untouched(self) -> None:
        lead = make_lead(email="ada@acme.com", provider="apollo")
        assert lead.source.sources == ["apollo"]

    def test_merge_leads_unions_sources(self) -> None:
        primary = make_lead(email="ada@acme.com", phone=None, provider="apollo")
        secondary = make_lead(email="ada@acme.com", phone="+14155550142", provider="csv")
        merged = merge_leads(primary, secondary)
        assert merged.source.sources == ["apollo", "csv"]
        assert merged.person.phone == "+14155550142"

    def test_merge_is_a_noop_when_provenance_already_matches(self) -> None:
        lead = make_lead(provider="apollo")
        assert merge_leads(lead, make_lead(provider="apollo")) is lead

    def test_provenance_survives_a_second_merge(self) -> None:
        # The union must be cumulative, not a pairwise overwrite.
        leads = [
            make_lead(email="ada@acme.com", provider="apollo"),
            make_lead(email="ada@acme.com", provider="company_website"),
            make_lead(email="ada@acme.com", provider="csv"),
        ]
        kept = Deduplicator(DedupStrategy.EMAIL).deduplicate(leads).kept[0]
        assert len(kept.source.sources) == 3
        assert len(set(kept.source.sources)) == 3


class TestDeterminism:
    def test_the_same_input_gives_the_same_outcome(self) -> None:
        leads = [
            make_lead(email="ada@acme.com"),
            make_lead(email="a.lovelace@acme.com", full_name="Ada Lovelace"),
            make_lead(email="alan@bletchley.com", full_name="Alan Turing"),
        ]
        first = Deduplicator(DedupStrategy.AGGRESSIVE).deduplicate(leads)
        second = Deduplicator(DedupStrategy.AGGRESSIVE).deduplicate(leads)
        assert [lead.lead_id for lead in first.kept] == [lead.lead_id for lead in second.kept]
        assert [(p.duplicate_lead_id, p.matched_key, p.kind) for p in first.duplicates] == [
            (p.duplicate_lead_id, p.matched_key, p.kind) for p in second.duplicates
        ]

    def test_the_first_record_that_claims_an_identity_is_the_one_kept(self) -> None:
        # First-seen precedence is the tie-break, so the outcome does not depend
        # on how the index happened to be built.
        first = make_lead(email="ada@acme.com", full_name="Ada Lovelace")
        second = make_lead(email="ada@acme.com", full_name="Augusta King")
        outcome = Deduplicator(DedupStrategy.EMAIL).deduplicate([first, second])
        assert outcome.kept[0].person.full_name == "Ada Lovelace"

    def test_a_transitive_chain_reports_each_merge(self) -> None:
        leads = [
            make_lead(email="ada@acme.com"),
            make_lead(email="a.lovelace@acme.com", full_name="Ada Lovelace"),
            make_lead(email="ada@acme.com", full_name="Ada Lovelace"),
        ]
        outcome = Deduplicator(DedupStrategy.AGGRESSIVE).deduplicate(leads)
        assert len(outcome.kept) == 1
        assert len(outcome.duplicates) == 2
        assert outcome.considered == 3


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
