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

from src.browser.auth import LoginWall
from src.browser.base import SessionSpec
from src.browser.session import (
    BrowserManager,
    BrowserSession,
    build_session_spec,
    get_browser_manager,
)
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

    def browser_counters(self) -> dict[str, int]:
        """Pages visited and browser failures for this source.

        Empty for a source that does not drive a browser, so the pipeline can
        ask every crawler the same question rather than type-testing them.
        """
        return {}

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
        limit: int | None = None,
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
            records: Raw source records, in source order. Consumed lazily, so a
                generator that reads from a file is left unread past ``limit``.
            mapper: Turns one record into a :class:`RawLead`. Returning ``None``
                means "there was nothing to map here" — a blank CSV row — and is
                not a failure.
            kind: What this source calls one record, for the log line
                (``"person"``, ``"csv row"``).
            label: Short identifier for a record, used in the log line. Defaults
                to the record's position, which is all a source with no stable id
                can offer.
            limit: Stop once this many leads have been produced. Counts *leads*,
                not records, so a source whose pages contain skipped entries is
                not cut short by them.

        Returns:
            ``(leads, failures)`` — the records that mapped, and how many did
            not. The count is returned rather than only logged so the caller's
            own summary line can carry it.
        """
        leads: list[RawLead] = []
        failures = 0

        for index, record in enumerate(records):
            if limit is not None and len(leads) >= limit:
                break
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


class BrowserCrawler(BaseCrawler):
    """A source driven through a real browser instead of an HTTP client.

    Kept as a separate subclass rather than folded into :class:`BaseCrawler` so
    that sources which need no browser — ``csv``, ``mock`` — keep exactly the
    contract they had, including being constructible without one.

    The session is opened lazily on first use and reused for the crawler's
    lifetime. A browser costs seconds to start, and the free tier permits one at
    a time, so acquiring one per page would be both slow and self-defeating.
    """

    #: What this source's signed-out state looks like, or ``None`` for a source
    #: with no login at all. Declared here so the guard in
    #: :meth:`~src.browser.session.BrowserSession.goto` can reject a login page
    #: before the adapter is handed it.
    login_wall: ClassVar[LoginWall | None] = None

    #: Persistent sources keep cookies on disk between runs, which is what makes
    #: a signed-in session survive. A source that reads public pages does not
    #: need one and should not pay for it.
    persistent_session: ClassVar[bool] = False

    #: Anti-fingerprinting arguments. On for a source being driven as an
    #: application; deliberately off for one that identifies itself honestly,
    #: since a forced-stealth fingerprint contradicting an honest user agent is
    #: itself a signal.
    stealth: ClassVar[bool] = True

    user_agent: ClassVar[str | None] = None

    def __init__(self, settings: Settings, *, browser: BrowserManager | None = None) -> None:
        super().__init__(settings)
        self._browser = browser
        self._session: BrowserSession | None = None

    # ------------------------------------------------------------------ #
    # Session
    # ------------------------------------------------------------------ #
    def session_spec(self) -> SessionSpec:
        """Describe the browser session this source needs."""
        return build_session_spec(
            self.settings,
            self.provider,
            persistent=self.persistent_session,
            stealth=self.stealth,
            user_agent=self.user_agent,
        )

    @property
    def browser(self) -> BrowserManager:
        """The run's browser manager, created on first use."""
        if self._browser is None:
            self._browser = get_browser_manager(self.settings)
        return self._browser

    async def session(self) -> BrowserSession:
        """The source's browser session, opened on first use.

        Reopened if it was closed, so a ``crawl()`` after an ``aclose()`` works
        rather than failing on a stale handle.
        """
        if self._session is None or self._session.closed:
            self._session = await self.browser.session_for(
                self.session_spec(), login_wall=self.login_wall
            )
        return self._session

    async def aclose(self) -> None:
        """Close the session. Safe to call twice."""
        session = self._session
        self._session = None
        if session is not None:
            await session.aclose()

    def is_available(self) -> tuple[bool, str]:
        """Whether a browser can be started here at all.

        Cheap and offline: it consults the provider's own check, which reads the
        filesystem and never downloads or launches. A source that also needs a
        signed-in profile overrides this to add that check.
        """
        return self.browser.is_available()

    def browser_counters(self) -> dict[str, int]:
        """Pages visited and browser failures for this source."""
        return self.browser.debug.counters(self.provider).as_dict()
