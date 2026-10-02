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

from src.models.enums import DedupStrategy, SeniorityLevel
from src.models.lead import StandardizedLead

#: Identity keys each strategy is allowed to match on, strongest first.
_STRATEGY_KEYS: dict[DedupStrategy, tuple[str, ...]] = {
    DedupStrategy.NONE: (),
    DedupStrategy.EMAIL: ("email",),
    DedupStrategy.IDENTITY: ("email", "linkedin", "phone_name"),
    DedupStrategy.AGGRESSIVE: (
        "email",
        "linkedin",
        "phone_name",
        "name_domain",
        "name_company",
        "lastname_domain",
    ),
}

_ModelT = TypeVar("_ModelT", bound=BaseModel)


class DuplicatePair(BaseModel):
    """A lead that was folded into another, and the key that linked them."""

    model_config = ConfigDict(extra="forbid")

    kept_lead_id: str
    duplicate_lead_id: str
    label: str
    matched_key: str
    matched_value: str


class DedupOutcome(BaseModel):
    """Result of a deduplication pass."""

    model_config = ConfigDict(extra="forbid")

    kept: list[StandardizedLead] = Field(default_factory=list)
    duplicates: list[DuplicatePair] = Field(default_factory=list)

    @property
    def removed_count(self) -> int:
        return len(self.duplicates)


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
        """
        if not self._allowed_keys:
            return DedupOutcome(kept=list(leads))

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
                )
            )

            if self.merge_fields:
                merged = merge_leads(kept_lead, lead)
                kept[match] = merged
                # Merging can surface identity keys the kept lead did not have
                # before, so the index is refreshed to keep later matches working.
                self._index(self._keys_for(merged), match, index)

        return DedupOutcome(kept=kept, duplicates=duplicates)

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


def _is_missing(value: object) -> bool:
    """Whether a field carries no information and may be filled by a duplicate."""
    # Enums must be checked before `str`: SeniorityLevel is a StrEnum, so its
    # members satisfy `isinstance(value, str)` and would otherwise be judged on
    # their text ("unknown" is non-empty, and so looks like real data).
    if isinstance(value, SeniorityLevel):
        return value is SeniorityLevel.UNKNOWN
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    return False


def merge_leads(primary: StandardizedLead, secondary: StandardizedLead) -> StandardizedLead:
    """Fill gaps in ``primary`` from ``secondary``.

    Only *missing* fields are taken, so the first-seen record always wins where
    both have a value. ``lead_id`` and ``source`` are deliberately left untouched:
    the merged lead stays the same entity, with the same provenance, and its id
    remains stable across runs.
    """
    merged_person = _merge_entity(primary.person, secondary.person)
    merged_company = _merge_entity(primary.company, secondary.company)

    if merged_person is primary.person and merged_company is primary.company:
        return primary
    return primary.model_copy(update={"person": merged_person, "company": merged_company})


def _merge_entity(target: _ModelT, source: _ModelT) -> _ModelT:
    """Copy non-empty fields from ``source`` into ``target`` where it is empty."""
    updates = {
        name: source_value
        for name in type(target).model_fields
        if _is_missing(getattr(target, name))
        and not _is_missing(source_value := getattr(source, name))
    }
    return target.model_copy(update=updates) if updates else target
