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

from src.models.results import CrawlResult
from src.utils.errors import ExportError
from src.utils.io import atomic_write
from src.utils.time import to_iso

if TYPE_CHECKING:
    from pathlib import Path

    from src.config import Settings


def build_report(result: CrawlResult, settings: Settings) -> dict[str, Any]:
    """Assemble the report payload.

    Deliberately excludes credentials: only non-secret run parameters are echoed,
    so the report is safe to attach to a ticket.
    """
    stats = result.stats
    return {
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
    result: CrawlResult, path: Path, settings: Settings, *, limit: int | None = None
) -> Path:
    """Write the run report atomically.

    Raises:
        ExportError: if the report cannot be written.
    """
    report = build_report(result, settings)
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
