"""Stage 4 — deduplication.

Collapses leads that describe the same person. Matching is key-based rather than
fuzzy: each lead exposes an ordered identity ladder (see
:meth:`~src.models.lead.StandardizedLead.identity_keys`), and the active strategy
decides how far down that ladder to trust.

When a duplicate is found the two records are **merged**, not simply discarded —
a second source often carries the phone number, country or LinkedIn URL the first
one lacked. Merging is what makes multi-source crawling worth doing.
"""

from __future__ import annotations

from typing import TypeVar

from pydantic import BaseModel, ConfigDict, Field

from src.models.enums import DedupStrategy, DuplicateKind
from src.models.lead import LeadSource, StandardizedLead, is_empty_value

#: Identity keys each strategy is allowed to match on, strongest first.
#:
#: ``source_id`` heads every non-``NONE`` strategy, including ``EMAIL``. It is
#: not a judgement call: two records from one provider carrying the same external
#: id *are* one record, so collapsing them cannot merge two different people even
#: under the most conservative setting.
_STRATEGY_KEYS: dict[DedupStrategy, tuple[str, ...]] = {
    DedupStrategy.NONE: (),
    DedupStrategy.EMAIL: ("source_id", "email"),
    DedupStrategy.IDENTITY: ("source_id", "email", "linkedin", "phone_name"),
    DedupStrategy.AGGRESSIVE: (
        "source_id",
        "email",
        "linkedin",
        "phone_name",
        "name_domain",
        "name_company",
    ),
}

#: Which keys constitute proof rather than inference. See :class:`DuplicateKind`.
#: Everything not listed here is treated as probable, so a key added to the
#: ladder later defaults to the honest answer rather than the flattering one.
_EXACT_KEYS: frozenset[str] = frozenset({"source_id", "email", "linkedin"})

_ModelT = TypeVar("_ModelT", bound=BaseModel)


class DuplicatePair(BaseModel):
    """A lead that was folded into another, and the key that linked them."""

    model_config = ConfigDict(extra="forbid")

    kept_lead_id: str
    duplicate_lead_id: str
    label: str
    matched_key: str
    matched_value: str
    #: Whether the match is proof or inference.
    kind: DuplicateKind
    #: Provider the absorbed record came from. Recorded because the merge keeps
    #: only the primary's ``external_id``/``source_url``, so without this the
    #: fact that a second source contributed would live only in
    #: ``LeadSource.sources`` — with no way to tell which record it was.
    duplicate_provider: str


class DedupOutcome(BaseModel):
    """Result of a deduplication pass."""

    model_config = ConfigDict(extra="forbid")

    kept: list[StandardizedLead] = Field(default_factory=list)
    duplicates: list[DuplicatePair] = Field(default_factory=list)
    #: Leads that entered this pass, before anything was collapsed. Stored rather
    #: than recomputed so the "before" figure in a report is a measurement, not
    #: arithmetic that has to stay in step with the other stage counters.
    considered: int = 0

    @property
    def removed_count(self) -> int:
        return len(self.duplicates)

    @property
    def exact_count(self) -> int:
        """Duplicates collapsed on proof (same source id, email or LinkedIn)."""
        return sum(1 for pair in self.duplicates if pair.kind is DuplicateKind.EXACT)

    @property
    def probable_count(self) -> int:
        """Duplicates collapsed on inference (name-anchored matches)."""
        return sum(1 for pair in self.duplicates if pair.kind is DuplicateKind.PROBABLE)


class Deduplicator:
    """Collapses duplicate leads using a configurable identity strategy."""

    def __init__(self, strategy: DedupStrategy, *, merge_fields: bool = True) -> None:
        self.strategy = strategy
        self.merge_fields = merge_fields
        self._allowed_keys = _STRATEGY_KEYS[strategy]

    def deduplicate(self, leads: list[StandardizedLead]) -> DedupOutcome:
        """Collapse duplicates, preserving first-seen order.

        The first occurrence of an identity is the record that is kept; later
        occurrences are merged into it. Order stability means a run over the same
        input always produces the same output.

        Determinism rests on two things: matches are looked up in the fixed
        ladder order rather than in dictionary order, and ``index`` uses
        ``setdefault`` so a key always resolves to the *first* lead that claimed
        it. Both are what make the result independent of how the input was
        ordered beyond first-seen precedence.
        """
        if not self._allowed_keys:
            return DedupOutcome(kept=list(leads), considered=len(leads))

        kept: list[StandardizedLead] = []
        # (key_name, key_value) -> position in `kept`
        index: dict[tuple[str, str], int] = {}
        duplicates: list[DuplicatePair] = []

        for lead in leads:
            keys = self._keys_for(lead)
            found = self._lookup(keys, index)

            if found is None:
                position = len(kept)
                kept.append(lead)
                self._index(keys, position, index)
                continue

            matched_key, match = found

            kept_lead = kept[match]
            duplicates.append(
                DuplicatePair(
                    kept_lead_id=kept_lead.lead_id,
                    duplicate_lead_id=lead.lead_id,
                    label=lead.person.full_name or lead.person.email or lead.lead_id,
                    matched_key=matched_key[0],
                    matched_value=matched_key[1],
                    kind=DuplicateKind.EXACT
                    if matched_key[0] in _EXACT_KEYS
                    else DuplicateKind.PROBABLE,
                    duplicate_provider=lead.source.provider,
                )
            )

            if self.merge_fields:
                merged = merge_leads(kept_lead, lead)
                kept[match] = merged
                # Merging can surface identity keys the kept lead did not have
                # before, so the index is refreshed to keep later matches working.
                self._index(self._keys_for(merged), match, index)

        return DedupOutcome(kept=kept, duplicates=duplicates, considered=len(leads))

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #
    def _keys_for(self, lead: StandardizedLead) -> list[tuple[str, str]]:
        """The lead's identity keys permitted by the active strategy, in order."""
        available = lead.identity_keys()
        return [(name, available[name]) for name in self._allowed_keys if name in available]

    @staticmethod
    def _lookup(
        keys: list[tuple[str, str]], index: dict[tuple[str, str], int]
    ) -> tuple[tuple[str, str], int] | None:
        """First identity key that already belongs to a kept lead.

        Returns the matching ``(key, value)`` pair and the position it maps to, so
        the caller can report *why* two leads were considered the same.
        """
        for key in keys:
            if key in index:
                return key, index[key]
        return None

    @staticmethod
    def _index(
        keys: list[tuple[str, str]], position: int, index: dict[tuple[str, str], int]
    ) -> None:
        for key in keys:
            index.setdefault(key, position)


def merge_leads(primary: StandardizedLead, secondary: StandardizedLead) -> StandardizedLead:
    """Fill gaps in ``primary`` from ``secondary``, keeping both provenances.

    Only *missing* fields are taken, so the first-seen record always wins where
    both have a value — "prefer non-empty" in its simplest and most predictable
    form. ``lead_id`` is left untouched: the merged lead stays the same entity,
    so its id stays stable across runs.

    Provenance is the exception to "primary wins". ``source.provider`` stays the
    primary's, but ``source.sources`` becomes the union, because collapsing two
    records must not erase the fact that a second source contributed — that is
    the one piece of information the merge would otherwise destroy.
    """
    merged_person = _merge_entity(primary.person, secondary.person)
    merged_company = _merge_entity(primary.company, secondary.company)
    merged_source = _merge_sources(primary.source, secondary.source)

    updates: dict[str, object] = {}
    if merged_person is not primary.person:
        updates["person"] = merged_person
    if merged_company is not primary.company:
        updates["company"] = merged_company
    if merged_source is not primary.source:
        updates["source"] = merged_source

    return primary.model_copy(update=updates) if updates else primary


def _merge_sources(primary: LeadSource, secondary: LeadSource) -> LeadSource:
    """Union the two provenance lists, primary's order first.

    ``external_id``, ``source_url`` and ``collected_at`` are deliberately *not*
    merged: they describe the primary's own retrieval, and overwriting them with
    another source's values would silently misattribute the record. The second
    source survives in ``sources``, and in the run report's duplicate pairs.
    """
    combined = list(primary.sources)
    for provider in secondary.sources:
        if provider not in combined:
            combined.append(provider)
    # The model's validator would re-seed the provider anyway; comparing first
    # avoids a pointless copy when nothing was added.
    if combined == primary.sources:
        return primary
    return primary.model_copy(update={"sources": combined})


def _merge_entity(target: _ModelT, source: _ModelT) -> _ModelT:
    """Copy non-empty fields from ``source`` into ``target`` where it is empty."""
    updates = {
        name: source_value
        for name in type(target).model_fields
        if is_empty_value(getattr(target, name))
        and not is_empty_value(source_value := getattr(source, name))
    }
    return target.model_copy(update=updates) if updates else target
