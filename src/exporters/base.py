"""The exporter contract and its registry.

Exporters turn a finished :class:`~src.models.results.CrawlResult` into files on
disk. They are resolved by format name, so the CLI can accept ``--format csv,jsonl``
without importing writers directly, and a new output format is one module plus a
registration.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from pydantic import BaseModel, ConfigDict

from src.models.enums import ExportFormat
from src.utils.errors import ConfigError, ExportError

if TYPE_CHECKING:
    from collections.abc import Sequence

    from src.config import Settings
    from src.models.lead import StandardizedLead


class ExportResult(BaseModel):
    """What an exporter wrote."""

    model_config = ConfigDict(extra="forbid")

    format: ExportFormat
    path: Path
    records: int
    bytes_written: int

    def describe(self) -> str:
        """One-line summary for CLI output."""
        size_kb = self.bytes_written / 1024
        return f"{self.format.value}: {self.path} ({self.records} leads, {size_kb:.1f} KiB)"


class BaseExporter(ABC):
    """Base class for output writers."""

    #: Format this exporter handles.
    format: ClassVar[ExportFormat]
    #: File extension, including the leading dot.
    extension: ClassVar[str] = ""
    #: One-line description for ``--list-formats``.
    description: ClassVar[str] = ""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @abstractmethod
    async def export(self, leads: Sequence[StandardizedLead], path: Path) -> ExportResult:
        """Write ``leads`` to ``path``.

        Implementations must write atomically so a failure cannot corrupt an
        existing export.

        Raises:
            ExportError: if the file cannot be written.
        """

    def build_path(self, directory: Path, prefix: str, timestamp: str) -> Path:
        """Default destination for this format: ``<prefix>_<timestamp><ext>``."""
        return directory / f"{prefix}_{timestamp}{self.extension}"

    def __repr__(self) -> str:
        return f"<{type(self).__name__} format={self.format.value!r}>"


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #
_REGISTRY: dict[ExportFormat, type[BaseExporter]] = {}


def register_exporter(cls: type[BaseExporter]) -> type[BaseExporter]:
    """Class decorator adding an exporter to the registry.

    Raises:
        ConfigError: if the format is already claimed by another class.
    """
    if cls.format in _REGISTRY and _REGISTRY[cls.format] is not cls:
        existing = _REGISTRY[cls.format].__name__
        raise ConfigError(f"export format {cls.format.value!r} is already handled by {existing}")
    _REGISTRY[cls.format] = cls
    return cls


def build_exporter(fmt: ExportFormat, settings: Settings) -> BaseExporter:
    """Instantiate the exporter for ``fmt``.

    Raises:
        ExportError: if no exporter is registered for that format.
    """
    if fmt not in _REGISTRY:
        known = ", ".join(sorted(item.value for item in _REGISTRY)) or "<none>"
        raise ExportError(f"no exporter registered for format {fmt.value!r}; available: {known}")
    return _REGISTRY[fmt](settings)


def available_formats() -> list[ExportFormat]:
    """Registered formats, in enum order for stable CLI output."""
    return [fmt for fmt in ExportFormat if fmt in _REGISTRY]


def registered_exporters() -> dict[ExportFormat, type[BaseExporter]]:
    """Snapshot of the registry, for introspection and tests."""
    return dict(_REGISTRY)
