"""CSV exporter.

Writes one flat row per lead. This is the format the rest of the business will
actually open, so it is optimized for spreadsheets: a stable column order, an
ISO-8601 timestamp, and a UTF-8 BOM so Excel detects the encoding instead of
mangling non-ASCII names.
"""

from __future__ import annotations

import asyncio
import csv
from typing import TYPE_CHECKING, Any

from src.exporters.base import BaseExporter, ExportResult, register_exporter
from src.models.enums import ExportFormat
from src.utils.errors import ExportError
from src.utils.io import atomic_write

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from src.models.lead import StandardizedLead

#: Canonical column order. Kept explicit (rather than derived from the first
#: lead) so an empty result still produces a usable header row, and so the column
#: order never shifts between runs. ``tests/test_exporters.py`` asserts this
#: matches :meth:`StandardizedLead.flatten`.
LEAD_COLUMNS: tuple[str, ...] = (
    "lead_id",
    "first_name",
    "last_name",
    "full_name",
    "job_title",
    "seniority",
    "email",
    "phone",
    "linkedin_url",
    "company_name",
    "company_domain",
    "company_website",
    "company_industry",
    "company_employee_count",
    "company_country",
    "company_city",
    "company_linkedin_url",
    "company_description",
    "company_contact_url",
    "company_social_links",
    "source_provider",
    "source_external_id",
    "source_url",
    "collected_at",
    "completeness",
)


@register_exporter
class CsvExporter(BaseExporter):
    """Writes leads as a delimited text file."""

    format = ExportFormat.CSV
    extension = ".csv"
    description = "Comma-separated values, one row per lead (spreadsheet-friendly)."

    async def export(self, leads: Sequence[StandardizedLead], path: Path) -> ExportResult:
        rows = [lead.flatten() for lead in leads]
        delimiter = self.settings.output_csv_delimiter
        # newline="" is required by the csv module to avoid doubled line endings.
        return await asyncio.to_thread(
            self._write, rows, path, delimiter, self.settings.output_encoding
        )

    def _write(
        self, rows: list[dict[str, Any]], path: Path, delimiter: str, encoding: str
    ) -> ExportResult:
        try:
            with atomic_write(path, encoding=encoding, newline="") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=list(LEAD_COLUMNS),
                    delimiter=delimiter,
                    extrasaction="ignore",
                    quoting=csv.QUOTE_MINIMAL,
                    lineterminator="\n",
                )
                writer.writeheader()
                writer.writerows(rows)
            written = path.stat().st_size
        except OSError as exc:
            raise ExportError(f"could not write CSV to {path}: {exc}") from exc

        return ExportResult(format=self.format, path=path, records=len(rows), bytes_written=written)
