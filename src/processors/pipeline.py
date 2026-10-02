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
        stats.duplicates_removed = outcome.removed_count
        kept = outcome.kept

        for pair in outcome.duplicates:
            self._record_rejection(
                rejections,
                RejectedLead(
                    provider="<merged>",
                    label=pair.label,
                    reason=RejectionReason.DUPLICATE,
                    detail=f"merged into {pair.kept_lead_id} on {pair.matched_key}",
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

        logger.info(
            "pipeline finished",
            extra={
                "raw": stats.raw_collected,
                "kept": len(ordered),
                "filtered": stats.filtered_out,
                "duplicates": stats.duplicates_removed,
                "invalid": stats.validation_failed,
                "duration_s": stats.duration_seconds,
            },
        )

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
        """Run each raw lead through normalization, validation and filtering."""
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

            outcome = self.validator.validate(lead)
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
                continue

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
                continue

            processed.append(lead)

        return processed

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    def _sort(self, leads: list[StandardizedLead]) -> list[StandardizedLead]:
        """Order leads best-first.

        Most complete leads first, with ``lead_id`` as a tie-break so the order is
        fully deterministic across runs.
        """
        if not self.settings.sort_by_completeness:
            return leads
        return sorted(leads, key=lambda lead: (-lead.completeness, lead.lead_id))

    def _record_rejection(self, rejections: list[RejectedLead], rejection: RejectedLead) -> None:
        """Append a rejection record, respecting the retention cap."""
        if len(rejections) < self._max_rejections:
            rejections.append(rejection)
