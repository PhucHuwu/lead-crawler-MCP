"""The processing pipeline.

Owns the full flow::

    crawl (concurrent, per-source)  ->  normalize  ->  validate  ->  filter
        ->  deduplicate  ->  sort  ->  CrawlResult

The pipeline knows nothing about any specific source or output format. It is
handed crawler instances and returns a :class:`CrawlResult`; the CLI decides what
to do with it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

from src.config import Settings
from src.crawlers.base import BaseCrawler
from src.models.enums import RejectionReason
from src.models.lead import RawLead, StandardizedLead
from src.models.results import (
    MAX_RECORDED_REJECTIONS,
    CrawlResult,
    CrawlStats,
    RejectedLead,
)
from src.processors.deduplicator import Deduplicator
from src.processors.filters import LeadFilter
from src.processors.normalizer import Normalizer
from src.processors.validator import LeadValidator
from src.utils.errors import CrawlerError
from src.utils.logging import get_logger

logger = get_logger(__name__)


class Pipeline:
    """Runs the collection and processing stages over a set of crawlers."""

    def __init__(
        self,
        settings: Settings,
        *,
        normalizer: Normalizer | None = None,
        validator: LeadValidator | None = None,
        lead_filter: LeadFilter | None = None,
        deduplicator: Deduplicator | None = None,
    ) -> None:
        self.settings = settings
        self.normalizer = normalizer or Normalizer()
        self.validator = validator or LeadValidator()
        self.lead_filter = lead_filter or LeadFilter(
            settings.filters, min_completeness=settings.min_completeness
        )
        self.deduplicator = deduplicator or Deduplicator(
            settings.dedup_strategy, merge_fields=settings.dedup_merge_fields
        )
        self._max_rejections = min(settings.max_recorded_rejections, MAX_RECORDED_REJECTIONS)

    # ------------------------------------------------------------------ #
    # Entry point
    # ------------------------------------------------------------------ #
    async def run(
        self,
        crawlers: Sequence[BaseCrawler],
        *,
        limit: int,
        max_leads: int | None = None,
    ) -> CrawlResult:
        """Collect and process leads from every crawler.

        Args:
            crawlers: Instantiated sources to crawl.
            limit: Maximum raw leads to request **from each source**.
            max_leads: Optional cap on the final set, applied after deduplication.

        Returns:
            The leads plus full run statistics. A source that raises is recorded
            in ``stats.source_errors`` and does not abort the run.
        """
        stats = CrawlStats()
        rejections: list[RejectedLead] = []

        raw_leads = await self._collect(crawlers, limit=limit, stats=stats)
        stats.raw_collected = len(raw_leads)

        processed = self._process(raw_leads, stats=stats, rejections=rejections)

        outcome = self.deduplicator.deduplicate(processed)
        stats.records_before_deduplication = outcome.considered
        stats.exact_duplicates = outcome.exact_count
        stats.probable_duplicates = outcome.probable_count
        kept = outcome.kept

        for pair in outcome.duplicates:
            self._record_rejection(
                rejections,
                RejectedLead(
                    # Named after the absorbed record's source, not the one it was
                    # merged into: the rejection is about the record that went
                    # away, and "<merged>" hid which source it came from.
                    provider=pair.duplicate_provider,
                    label=pair.label,
                    reason=RejectionReason.DUPLICATE,
                    detail=(
                        f"{pair.kind.value} duplicate: merged into {pair.kept_lead_id} "
                        f"on {pair.matched_key}"
                    ),
                ),
            )

        ordered = self._sort(kept)

        if max_leads is not None and len(ordered) > max_leads:
            logger.info(
                "trimming result set to max_leads",
                extra={"before": len(ordered), "after": max_leads},
            )
            ordered = ordered[:max_leads]

        stats.exported = len(ordered)
        stats.finalize()

        self._log_run_summary(stats)

        return CrawlResult(
            leads=ordered,
            stats=stats,
            rejections=rejections,
            rejections_truncated=stats.total_rejected > len(rejections),
        )

    # ------------------------------------------------------------------ #
    # Stage 0 — collection
    # ------------------------------------------------------------------ #
    async def _collect(
        self, crawlers: Sequence[BaseCrawler], *, limit: int, stats: CrawlStats
    ) -> list[RawLead]:
        """Crawl every source concurrently, isolating individual failures."""
        if not crawlers:
            return []

        semaphore = asyncio.Semaphore(self.settings.max_concurrency)

        async def run_one(crawler: BaseCrawler) -> list[RawLead]:
            async with semaphore:
                logger.info(
                    "crawling source",
                    extra={"provider": crawler.provider, "limit": limit},
                )
                return await crawler.crawl(limit)

        # return_exceptions keeps one bad source from cancelling the others.
        results = await asyncio.gather(
            *(run_one(crawler) for crawler in crawlers), return_exceptions=True
        )

        collected: list[RawLead] = []
        for crawler, result in zip(crawlers, results, strict=True):
            provider = crawler.provider
            if isinstance(result, BaseException):
                message = str(result)
                stats.record_source_error(provider, message)
                logger.error(
                    "source failed",
                    extra={"provider": provider, "error": message},
                    exc_info=not isinstance(result, CrawlerError),
                )
                continue

            stats.per_provider[provider] = len(result)
            collected.extend(result)
            logger.info(
                "source collected leads",
                extra={"provider": provider, "count": len(result)},
            )
        return collected

    # ------------------------------------------------------------------ #
    # Stages 1–3 — normalize, validate, filter
    # ------------------------------------------------------------------ #
    def _process(
        self,
        raw_leads: Sequence[RawLead],
        *,
        stats: CrawlStats,
        rejections: list[RejectedLead],
    ) -> list[StandardizedLead]:
        """Run each raw lead through normalization, validation and filtering.

        Every record is processed in isolation: an exception raised anywhere in the
        chain is recorded against *that* record and the run continues. Only a
        source-level failure can end a run early.
        """
        processed: list[StandardizedLead] = []

        for raw in raw_leads:
            try:
                lead = self.normalizer.normalize(raw)
            except Exception as exc:
                # A single malformed record must never take down the run.
                stats.normalization_failed += 1
                self._record_rejection(
                    rejections,
                    RejectedLead(
                        provider=raw.provider,
                        label=raw.label(),
                        reason=RejectionReason.NORMALIZATION_FAILED,
                        detail=str(exc),
                        external_id=raw.external_id,
                    ),
                )
                logger.warning(
                    "normalization failed",
                    extra={"provider": raw.provider, "label": raw.label(), "error": str(exc)},
                )
                continue

            stats.normalized += 1

            try:
                keep = self._evaluate(lead, raw, stats=stats, rejections=rejections)
            except Exception as exc:
                # A bug in a downstream stage is contained the same way bad input
                # is — one record is lost, the run is not.
                stats.processing_failed += 1
                self._record_rejection(
                    rejections,
                    RejectedLead(
                        provider=lead.source.provider,
                        label=raw.label(),
                        reason=RejectionReason.PROCESSING_FAILED,
                        detail=str(exc),
                        external_id=lead.source.external_id,
                    ),
                )
                logger.warning(
                    "processing failed",
                    extra={
                        "provider": lead.source.provider,
                        "label": raw.label(),
                        "error": str(exc),
                    },
                )
                continue

            if keep:
                processed.append(lead)

        return processed

    def _evaluate(
        self,
        lead: StandardizedLead,
        raw: RawLead,
        *,
        stats: CrawlStats,
        rejections: list[RejectedLead],
    ) -> bool:
        """Validate and filter one normalized lead. True when it should be kept."""
        outcome = self.validator.validate(lead)

        # Attribute every rule that fired, whether or not it was fatal, so the
        # run report says which checks are noisy rather than only how many
        # records died.
        for rule in outcome.rules:
            stats.record_validation(rule)

        if not outcome.is_valid:
            stats.validation_failed += 1
            self._record_rejection(
                rejections,
                RejectedLead(
                    provider=lead.source.provider,
                    label=raw.label(),
                    reason=RejectionReason.VALIDATION_FAILED,
                    detail=outcome.summary(),
                    external_id=lead.source.external_id,
                ),
            )
            return False

        if outcome.has_warnings:
            # "Reject or mark": the record is usable, so it is kept and flagged.
            # The flag reaches the operator through the run summary counters and
            # this DEBUG line, which --verbose turns on.
            stats.validation_warned += 1
            logger.debug(
                "lead flagged",
                extra={
                    "provider": lead.source.provider,
                    "label": raw.label(),
                    "detail": outcome.summary(),
                },
            )

        decision = self.lead_filter.evaluate(lead)
        if not decision.passed:
            stats.record_filter(decision.rule or "unknown")
            self._record_rejection(
                rejections,
                RejectedLead(
                    provider=lead.source.provider,
                    label=raw.label(),
                    reason=RejectionReason.FILTERED_OUT,
                    detail=decision.detail,
                    external_id=lead.source.external_id,
                ),
            )
            return False

        return True

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    def _log_run_summary(self, stats: CrawlStats) -> None:
        """Emit one structured record describing the whole run.

        This is the record an operator greps for: emitted exactly once per run,
        carrying every figure needed to answer "what did the crawl actually do?"
        without correlating several lines. With ``--log-format json`` each field
        below becomes a top-level JSON key.
        """
        logger.info(
            "run summary",
            extra={
                # Every source the run touched, whether it produced anything or
                # failed; ``errors`` below says which of them went wrong.
                "sources": sorted(set(stats.per_provider) | set(stats.source_errors)),
                "started_at": stats.started_at.isoformat(),
                "finished_at": stats.finished_at.isoformat() if stats.finished_at else None,
                "records_discovered": stats.raw_collected,
                "records_parsed": stats.normalized,
                "records_invalid": stats.records_invalid,
                "records_warned": stats.validation_warned,
                "records_filtered": stats.filtered_out,
                # The dedup block, under the names the run report uses. Before and
                # after bracket the stage; the two duplicate counts say how much
                # of the collapse was proof versus inference.
                "records_before_deduplication": stats.records_before_deduplication,
                "exact_duplicates": stats.exact_duplicates,
                "probable_duplicates": stats.probable_duplicates,
                "records_after_deduplication": stats.records_after_deduplication,
                "duplicates_removed": stats.duplicates_removed,
                "records_exported": stats.exported,
                "validation_rules": stats.per_validation_reason,
                "filter_rules": stats.per_filter_reason,
                "errors": stats.source_errors or {},
                "duration_seconds": stats.duration_seconds,
            },
        )

    def _sort(self, leads: list[StandardizedLead]) -> list[StandardizedLead]:
        """Order leads best-first.

        Most complete leads first, with ``lead_id`` as a tie-break so the order is
        fully deterministic across runs.
        """
        if not self.settings.sort_by_completeness:
            return leads
        return sorted(leads, key=lambda lead: (-lead.completeness, lead.lead_id))

    def _record_rejection(self, rejections: list[RejectedLead], rejection: RejectedLead) -> None:
        """Append a rejection record, respecting the retention cap.

        Also logged at DEBUG: a run that returns fewer leads than expected is
        almost always explained by these lines, and they are far too numerous to
        belong in a normal run's log.
        """
        logger.debug(
            "lead rejected",
            extra={
                "provider": rejection.provider,
                "label": rejection.label,
                "reason": rejection.reason.value,
                "detail": rejection.detail,
            },
        )
        if len(rejections) < self._max_rejections:
            rejections.append(rejection)
