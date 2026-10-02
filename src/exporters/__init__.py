"""Output writers.

Importing this package registers every built-in exporter, so the CLI can resolve
``--format csv`` without importing writers directly.

To add a format:

1. Create ``src/exporters/<name>.py`` with a :class:`~src.exporters.base.BaseExporter`
   subclass decorated with ``@register_exporter``, and a new member on
   :class:`~src.models.enums.ExportFormat`.
2. Import it below.
"""

from src.exporters.base import (
    BaseExporter,
    ExportResult,
    available_formats,
    build_exporter,
    register_exporter,
    registered_exporters,
)
from src.exporters.csv_exporter import LEAD_COLUMNS, CsvExporter
from src.exporters.json_exporter import JsonExporter, JsonLinesExporter, lead_to_dict
from src.exporters.report import build_report, write_run_report

__all__ = [
    "LEAD_COLUMNS",
    "BaseExporter",
    "CsvExporter",
    "ExportResult",
    "JsonExporter",
    "JsonLinesExporter",
    "available_formats",
    "build_exporter",
    "build_report",
    "lead_to_dict",
    "register_exporter",
    "registered_exporters",
    "write_run_report",
]
