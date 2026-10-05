"""The crawler adapter contract.

Every data source — an API, a CSV export, a test fixture — is wrapped in a
subclass of :class:`BaseCrawler`. The adapter's only job is to turn whatever the
source gives us into a list of :class:`RawLead`. It does **not** clean, validate,
filter or deduplicate: those are pipeline stages that all sources share.

Adding a source therefore means adding exactly one module and registering it.
Nothing in :mod:`src.processors`, :mod:`src.exporters` or the CLI needs to change.

Because the adapter is where an untrusted payload first becomes our data, the
base class also carries the containment for that step: :meth:`BaseCrawler.map_records`
turns a source's records into leads one at a time, so a record that cannot be
mapped costs that record and nothing else.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, ClassVar, Self

from src.models.lead import RawLead
from src.utils.logging import get_logger

if TYPE_CHECKING:
    import logging
    from collections.abc import Callable, Iterable

    from src.config import Settings

#: Longest record excerpt echoed into a mapping-failure log line. Enough to
#: identify the record — an id, an email, a name — without turning one bad row
#: into a screenful of payload.
_MAX_RECORD_EXCERPT = 300


def record_excerpt(record: Any) -> str:
    """Short, log-safe rendering of a source record that failed to map.

    Truncated and whitespace-collapsed because its job is identification, not
    reproduction: the full payload is preserved on every record that *does*
    map, in ``RawLead.raw``.
    """
    try:
        text = json.dumps(record, default=str, ensure_ascii=False)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        text = repr(record)
    text = " ".join(text.split())
    return text[:_MAX_RECORD_EXCERPT] + ("…" if len(text) > _MAX_RECORD_EXCERPT else "")


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
    # Record mapping
    # ------------------------------------------------------------------ #
    def map_records(
        self,
        records: Iterable[Any],
        mapper: Callable[[Any], RawLead | None],
        *,
        kind: str = "record",
        label: Callable[[Any], str] | None = None,
    ) -> tuple[list[RawLead], int]:
        """Turn a source's records into leads, containing any that fail.

        A source hands us records it believes are well-formed; an adapter does
        not get to assume that, and the failure mode of assuming it is ugly —
        an exception raised while mapping *one* record unwinds the whole
        :meth:`crawl` call, so every lead collected alongside it is lost and the
        source is recorded as failed. Containing the failure here costs one
        record instead.

        Used rather than a bare comprehension in every adapter so the containment
        cannot be forgotten when the next source is added.

        Args:
            records: Raw source records, in source order.
            mapper: Turns one record into a :class:`RawLead`. Returning ``None``
                means "there was nothing to map here" — a blank CSV row — and is
                not a failure.
            kind: What this source calls one record, for the log line
                (``"person"``, ``"csv row"``).
            label: Short identifier for a record, used in the log line. Defaults
                to the record's position, which is all a source with no stable id
                can offer.

        Returns:
            ``(leads, failures)`` — the records that mapped, and how many did
            not. The count is returned rather than only logged so the caller's
            own summary line can carry it.
        """
        leads: list[RawLead] = []
        failures = 0

        for index, record in enumerate(records):
            try:
                lead = mapper(record)
            except Exception as exc:
                # Every failure is logged individually, not just counted: the
                # excerpt is what says *which* record was unreadable, and a bare
                # total would leave the operator to bisect the source by hand.
                failures += 1
                self.logger.warning(
                    f"could not map {kind}; skipping it",
                    extra={
                        "provider": self.provider,
                        "record": label(record) if label is not None else f"#{index}",
                        "error": str(exc),
                        "excerpt": record_excerpt(record),
                    },
                )
                continue
            if lead is not None:
                leads.append(lead)

        return leads, failures

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    def __repr__(self) -> str:
        return f"<{type(self).__name__} provider={self.provider!r}>"
