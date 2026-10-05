"""Run report writer.

Every run optionally emits a JSON sidecar describing what happened: the counters,
which filter rules rejected what, which sources failed, and a sample of the
individual rejections. This is the artifact to read when a crawl returns fewer
leads than expected — without it, a filter change is invisible.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

from src.exporters.base import source_slug
from src.models.results import CrawlResult
from src.utils.errors import ExportError
from src.utils.io import atomic_write
from src.utils.time import to_iso

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from src.config import Settings


def build_report(
    result: CrawlResult, settings: Settings, *, sources: Sequence[str] | None = None
) -> dict[str, Any]:
    """Assemble the report payload.

    Deliberately excludes credentials: only non-secret run parameters are echoed,
    so the report is safe to attach to a ticket.

    ``sources`` names the providers the run requested; it is what the summary's
    ``source`` field and the export filenames are both built from, so a report
    always names the files it belongs to. Omitted, the requested set is inferred
    from the counters — which loses a source that failed before returning
    anything, hence the argument.
    """
    stats = result.stats
    names = (
        list(sources)
        if sources is not None
        else sorted(set(stats.per_provider) | set(stats.source_errors))
    )
    return {
        # A flat digest, first, for the question actually being asked of a report
        # — "did this run work?" — without making the reader walk the nested
        # blocks below to answer it.
        "summary": {
            "source": source_slug(names),
            "started_at": to_iso(stats.started_at),
            "finished_at": to_iso(stats.finished_at),
            "discovered": stats.raw_collected,
            "valid": stats.normalized,
            "filtered": stats.filtered_out,
            "duplicates_removed": stats.duplicates_removed,
            "exported": stats.exported,
            "errors": stats.records_invalid,
            # Browser activity sits beside the record counts because it answers
            # the question they cannot: "the site had nothing" and "we never
            # reached the site" produce the same zero in every field above, and
            # only ``pages_visited`` separates them.
            "pages_visited": stats.pages_visited,
            "browser_errors": stats.browser_errors,
            "selector_failures": stats.selector_failures,
            "auth_failures": stats.auth_failures,
        },
        "generated_at": to_iso(stats.finished_at),
        "run": {
            "started_at": to_iso(stats.started_at),
            "duration_seconds": stats.duration_seconds,
            "limit_per_source": None,
            "dedup_strategy": settings.dedup_strategy.value,
            "min_completeness": settings.min_completeness,
            "filters_active": settings.filters.is_active,
            "filters": settings.filters.model_dump(mode="json"),
        },
        "stats": stats.model_dump(mode="json"),
        # The four deduplication statistics, stated together and under their own
        # names. Two of them are derived on CrawlStats — a printed total that
        # could disagree with the split beside it would be worse than no total —
        # so `stats` above carries only the stored fields. This block is the
        # complete set, so a consumer does not have to re-derive the arithmetic.
        "deduplication": {
            "records_before_deduplication": stats.records_before_deduplication,
            "exact_duplicates": stats.exact_duplicates,
            "probable_duplicates": stats.probable_duplicates,
            "records_after_deduplication": stats.records_after_deduplication,
        },
        "sources": {
            "collected": stats.per_provider,
            "errors": stats.source_errors,
        },
        "rejections": {
            "total": stats.total_rejected,
            "by_rule": stats.per_filter_reason,
            "recorded": len(result.rejections),
            "truncated": result.rejections_truncated,
            "sample": [item.model_dump(mode="json") for item in result.rejections],
        },
        "outputs": stats.output_files,
    }


async def write_run_report(
    result: CrawlResult,
    path: Path,
    settings: Settings,
    *,
    limit: int | None = None,
    sources: Sequence[str] | None = None,
) -> Path:
    """Write the run report atomically.

    Raises:
        ExportError: if the report cannot be written.
    """
    report = build_report(result, settings, sources=sources)
    if limit is not None:
        report["run"]["limit_per_source"] = limit

    text = json.dumps(report, indent=2, ensure_ascii=False) + "\n"

    def _write() -> None:
        with atomic_write(path, encoding="utf-8") as handle:
            handle.write(text)

    try:
        await asyncio.to_thread(_write)
    except OSError as exc:
        raise ExportError(f"could not write run report to {path}: {exc}") from exc
    return path
