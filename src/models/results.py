"""Result and statistics types produced by a crawl run."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from src.models.enums import RejectionReason
from src.models.lead import StandardizedLead
from src.utils.time import utcnow

MAX_RECORDED_REJECTIONS = 1000
"""Cap on retained rejection records.

Counters in :class:`CrawlStats` are always exact; this only bounds the sample of
individual rejects kept in memory and written to the run report, so a filter
that drops 200k leads cannot exhaust memory.
"""


class RejectedLead(BaseModel):
    """A single lead that was dropped, with the reason and a human label.

    This is the debugging surface for the pipeline: if a run returns fewer leads
    than expected, the rejections say exactly which stage removed what.
    """

    model_config = ConfigDict(extra="forbid")

    provider: str
    label: str
    reason: RejectionReason
    detail: str | None = None
    external_id: str | None = None


class CrawlStats(BaseModel):
    """Per-run counters, mutated in place by the pipeline as it progresses."""

    model_config = ConfigDict(extra="forbid")

    started_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime | None = None
    duration_seconds: float | None = None

    raw_collected: int = 0
    normalized: int = 0
    normalization_failed: int = 0
    #: Records a post-normalization stage (validation, filtering) raised on.
    processing_failed: int = 0
    validation_failed: int = 0
    filtered_out: int = 0
    duplicates_removed: int = 0
    exported: int = 0

    #: Raw leads received per provider, before any processing.
    per_provider: dict[str, int] = Field(default_factory=dict)
    #: Filter rule name -> number of leads it rejected.
    per_filter_reason: dict[str, int] = Field(default_factory=dict)
    #: Provider -> error message, for sources that failed without aborting the run.
    source_errors: dict[str, str] = Field(default_factory=dict)
    #: Paths written by the exporters, relative to the output directory.
    output_files: list[str] = Field(default_factory=list)

    @property
    def total_rejected(self) -> int:
        """Leads dropped after collection, by any stage."""
        return (
            self.normalization_failed
            + self.processing_failed
            + self.validation_failed
            + self.filtered_out
            + self.duplicates_removed
        )

    @property
    def records_invalid(self) -> int:
        """Raw records that could not be turned into a usable lead.

        The single figure the run log reports as "invalid records": a record that
        failed to normalize or that a later processor choked on, plus one that
        normalized cleanly but had no usable identity.
        """
        return self.normalization_failed + self.processing_failed + self.validation_failed

    @property
    def failed_providers(self) -> list[str]:
        """Providers that raised during collection."""
        return sorted(self.source_errors)

    def record_filter(self, rule: str) -> None:
        """Attribute one filtered-out lead to the rule that rejected it."""
        self.filtered_out += 1
        self.per_filter_reason[rule] = self.per_filter_reason.get(rule, 0) + 1

    def record_source_error(self, provider: str, message: str) -> None:
        self.source_errors[provider] = message

    def finalize(self) -> None:
        """Stamp the end of the run. Idempotent."""
        if self.finished_at is not None:
            return
        self.finished_at = utcnow()
        self.duration_seconds = round((self.finished_at - self.started_at).total_seconds(), 3)


class CrawlResult(BaseModel):
    """Everything a run produced: the leads, the counters and a rejection sample."""

    model_config = ConfigDict(extra="forbid")

    leads: list[StandardizedLead] = Field(default_factory=list)
    stats: CrawlStats = Field(default_factory=CrawlStats)
    rejections: list[RejectedLead] = Field(default_factory=list)
    #: True when more rejections occurred than :data:`MAX_RECORDED_REJECTIONS`.
    rejections_truncated: bool = False

    @property
    def is_empty(self) -> bool:
        return not self.leads

    def summary_line(self) -> str:
        """One-line human summary for the CLI."""
        stats = self.stats
        return (
            f"{len(self.leads)} leads  "
            f"(raw={stats.raw_collected} filtered={stats.filtered_out} "
            f"dupes={stats.duplicates_removed} invalid={stats.validation_failed} "
            f"failed_sources={len(stats.source_errors)})  "
            f"in {stats.duration_seconds or 0:.2f}s"
        )
