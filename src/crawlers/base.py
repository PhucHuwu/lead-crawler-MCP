"""The crawler adapter contract.

Every data source — an API, a CSV export, a test fixture — is wrapped in a
subclass of :class:`BaseCrawler`. The adapter's only job is to turn whatever the
source gives us into a list of :class:`RawLead`. It does **not** clean, validate,
filter or deduplicate: those are pipeline stages that all sources share.

Adding a source therefore means adding exactly one module and registering it.
Nothing in :mod:`src.processors`, :mod:`src.exporters` or the CLI needs to change.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, ClassVar, Self

from src.models.lead import RawLead
from src.utils.logging import get_logger

if TYPE_CHECKING:
    import logging

    from src.config import Settings


class BaseCrawler(ABC):
    """Base class for all lead sources.

    Subclasses must set :attr:`provider` (the stable slug used by ``--source``)
    and implement :meth:`crawl`. They should be usable as async context managers
    so any HTTP connection pool is released even when a run fails.
    """

    #: Stable slug identifying this source; the value users pass to ``--source``.
    provider: ClassVar[str] = ""
    #: Human-readable name for CLI help and logs.
    display_name: ClassVar[str] = ""
    #: One-line description shown in ``--list-sources``.
    description: ClassVar[str] = ""
    #: True when this source cannot run without credentials.
    requires_credentials: ClassVar[bool] = False

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.logger: logging.Logger = get_logger(f"crawlers.{self.provider or type(self).__name__}")

    # ------------------------------------------------------------------ #
    # Contract
    # ------------------------------------------------------------------ #
    @abstractmethod
    async def crawl(self, limit: int) -> list[RawLead]:
        """Collect up to ``limit`` raw leads.

        Implementations should return *at most* ``limit`` records and may return
        fewer (or none) without raising. Raising :class:`~src.utils.errors.CrawlerError`
        signals a source-level failure; the pipeline records it and continues
        with the remaining sources rather than aborting the run.

        Args:
            limit: Maximum number of leads to return.
        """

    async def aclose(self) -> None:
        """Release any resources held by the crawler. Safe to call twice."""
        return

    def is_available(self) -> tuple[bool, str]:
        """Whether this source can run with the current configuration.

        Returns:
            ``(True, "")`` when usable, otherwise ``(False, reason)``. Callers
            use the reason verbatim in CLI output, so it should name the missing
            setting rather than say "unavailable".
        """
        return True, ""

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    def __repr__(self) -> str:
        return f"<{type(self).__name__} provider={self.provider!r}>"
