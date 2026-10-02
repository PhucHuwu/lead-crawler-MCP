"""JSON exporters.

Two shapes are offered because they serve different consumers:

* ``json``  — a single pretty-printed array. The whole document is valid JSON, so
  it can be opened by any tool, but it must be read in full.
* ``jsonl`` — one compact object per line, streamable and appendable, which is
  what downstream pipelines and log processors prefer.

Both emit the *nested* schema (``person`` / ``company`` / ``source``) with the
computed ``completeness`` score included, preserving the full fidelity of the
standardized model that the flat CSV necessarily discards.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

from src.exporters.base import BaseExporter, ExportResult, register_exporter
from src.models.enums import ExportFormat
from src.utils.errors import ExportError
from src.utils.io import atomic_write

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from src.models.lead import StandardizedLead


def lead_to_dict(lead: StandardizedLead) -> dict[str, Any]:
    """Serialize a lead to a JSON-safe dict, computed fields included.

    ``mode="json"`` renders datetimes as ISO-8601 strings and enums as their
    values, so the output needs no custom encoder.
    """
    return lead.model_dump(mode="json")


@register_exporter
class JsonExporter(BaseExporter):
    """Writes leads as a single pretty-printed JSON array."""

    format = ExportFormat.JSON
    extension = ".json"
    description = "Pretty-printed JSON array with the full nested schema."

    async def export(self, leads: Sequence[StandardizedLead], path: Path) -> ExportResult:
        payload = [lead_to_dict(lead) for lead in leads]
        text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
        return await asyncio.to_thread(self._write, text, path, len(leads))

    def _write(self, text: str, path: Path, count: int) -> ExportResult:
        try:
            with atomic_write(path, encoding="utf-8") as handle:
                handle.write(text)
            written = path.stat().st_size
        except OSError as exc:
            raise ExportError(f"could not write JSON to {path}: {exc}") from exc

        return ExportResult(format=self.format, path=path, records=count, bytes_written=written)


@register_exporter
class JsonLinesExporter(BaseExporter):
    """Writes one compact JSON object per line."""

    format = ExportFormat.JSONL
    extension = ".jsonl"
    description = "Newline-delimited JSON, one lead per line (streamable)."

    async def export(self, leads: Sequence[StandardizedLead], path: Path) -> ExportResult:
        lines = [
            json.dumps(lead_to_dict(lead), ensure_ascii=False, separators=(",", ":"))
            for lead in leads
        ]
        return await asyncio.to_thread(self._write, lines, path)

    def _write(self, lines: list[str], path: Path) -> ExportResult:
        try:
            with atomic_write(path, encoding="utf-8") as handle:
                for line in lines:
                    handle.write(line)
                    handle.write("\n")
            written = path.stat().st_size
        except OSError as exc:
            raise ExportError(f"could not write JSONL to {path}: {exc}") from exc

        return ExportResult(
            format=self.format, path=path, records=len(lines), bytes_written=written
        )
