"""Session and manager lifecycle.

This is engine code, shared by every provider. It owns the things a crawler gets
wrong when it owns them itself: how many browsers may run at once, what a page's
lifetime is, when to check for an expired session, and where diagnostics go. A
provider supplies only "launch this and hand me a connection" — see
:class:`~src.browser.base.BrowserProvider`.

**Scoping.** One :class:`BrowserManager` per run. One :class:`BrowserSession` per
crawler, opened lazily on first use. One :class:`BrowserPage` per unit of work,
never shared between concurrent tasks.

**The concurrency limit is real.** CloakBrowser's free tier permits one
concurrent session, and a persistent context is its own Chromium process, so
persistent contexts cannot share one. The manager therefore holds an
``asyncio.Semaphore`` for a session's whole lifetime — narrower than and separate
from the pipeline's ``max_concurrency``. Two browser-backed sources in one run
therefore serialize, and :meth:`BrowserManager.start` says so in the log rather
than letting the pipeline's apparent parallelism mislead.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.browser.auth import AuthGuard, LoginWall, auth_error
from src.browser.base import (
    BrowserConnection,
    BrowserPage,
    BrowserProvider,
    SessionSpec,
    Viewport,
)
from src.browser.cloak import CloakBrowserProvider
from src.browser.debug import DebugRecorder, RunCounters, prune_debug_dirs
from src.browser.redact import redact_url
from src.utils.errors import (
    CrawlerError,
    SourceNotFoundError,
    SourceUnavailableError,
)
from src.utils.logging import get_logger

#: Provider registry, keyed by ``LEAD_BROWSER__PROVIDER``. The MCP provider the
#: architecture is being prepared for becomes a second entry here and nothing
#: else changes.
_PROVIDERS: dict[str, type[CloakBrowserProvider]] = {
    "cloakbrowser": CloakBrowserProvider,
}


@dataclass(frozen=True, slots=True)
class NavigationOutcome:
    """What a navigation actually did."""

    status: int | None
    final_url: str
    #: True when the browser ended somewhere other than the requested URL.
    #: A redirect is not an error by itself, but an unexpected one is worth
    #: knowing about — and for a signed-in source it is often the first sign the
    #: session lapsed.
    redirected: bool


class BrowserSession:
    """One source's browser session, plus the guard and diagnostics around it."""

    def __init__(
        self,
        spec: SessionSpec,
        *,
        connection: BrowserConnection,
        manager: BrowserManager,
        auth_guard: AuthGuard | None = None,
        recorder: DebugRecorder | None = None,
        nav_timeout_ms: int | None = None,
    ) -> None:
        self.spec = spec
        self.provider_name = spec.provider
        # Annotated rather than inferred: the connection is dropped on close and
        # rebuilt on the next use, so ``None`` is a real state and not just an
        # initialisation detail.
        self._connection: BrowserConnection | None = connection
        self._manager = manager
        self._auth_guard = auth_guard
        self.recorder = recorder
        self._nav_timeout_ms = nav_timeout_ms
        self._closed = False
        #: Set by the manager. The session only releases a slot it actually holds,
        #: otherwise a close-after-close would inflate the semaphore and let more
        #: browsers run than the license permits.
        self.holds_slot = True

    @property
    def closed(self) -> bool:
        return self._closed

    # ------------------------------------------------------------------ #
    # Pages
    # ------------------------------------------------------------------ #
    @asynccontextmanager
    async def page(self) -> AsyncIterator[BrowserPage]:
        """Open a fresh isolated page for the duration of the block.

        The viewport is set when the connection is created, from the spec, not
        here — Playwright fixes it per context, so a per-page override would be a
        parameter that silently did nothing.
        """
        connection = await self._open_connection()
        page = await connection.new_page()
        try:
            yield page
        finally:
            await page.close()

    async def _open_connection(self) -> BrowserConnection:
        if self._connection is None:
            self._connection = await self._manager.connect(self.spec)
            self._closed = False
            self.holds_slot = True
        return self._connection

    # ------------------------------------------------------------------ #
    # Navigation
    # ------------------------------------------------------------------ #
    async def goto(
        self,
        page: BrowserPage,
        url: str,
        *,
        required: bool = True,
        debug_label: str | None = None,
        wait_until: str = "domcontentloaded",
    ) -> NavigationOutcome:
        """Navigate, then check the result against the source's login wall.

        The guard runs **here**, between the browser and the adapter, and that
        placement is the whole point: an adapter never receives a login page, so
        it cannot parse one into leads. A comment asking adapters to remember
        would not survive the first refactor.

        There is deliberately no retry. A navigation that lands on a login wall
        will land there again, so retrying only delays the error and multiplies
        the requests sent to a site that has already refused us.
        """
        timeout = self._nav_timeout_ms
        try:
            status = await page.goto(url, wait_until=wait_until, timeout=timeout)
        except Exception as exc:
            await self._capture(page, debug_label or "navigation-failed", "navigation_failed", str(exc))
            raise SourceUnavailableError(
                self.provider_name, f"navigation to {redact_url(url)} failed: {exc}"
            ) from exc

        self._counters().pages_visited += 1
        outcome = NavigationOutcome(
            status=status, final_url=page.url, redirected=page.url.rstrip("/") != url.rstrip("/")
        )

        if self._auth_guard is not None:
            verdict = await self._auth_guard.check(page)
            if not verdict.authenticated:
                await self._capture(page, debug_label or "login-wall", "auth_expired", verdict.detail)
                raise auth_error(self._auth_guard, verdict, profile_dir=self.spec.user_data_dir)

        if required and status is not None and status >= 400:
            raise _http_error(self.provider_name, status, url)

        return outcome

    def _counters(self) -> RunCounters:
        return self._manager.debug.counters(self.provider_name)

    async def _capture(self, page: BrowserPage, label: str, kind: str, error: str) -> None:
        """Record evidence for a failure.

        ``force=True`` because an auth wall or a dead navigation is exactly the
        case where the operator needs the page whether or not diagnostics were
        switched on.
        """
        if self.recorder is None:
            return
        await self.recorder.record_page(
            page, provider=self.provider_name, label=label, kind=kind, error=error, force=True
        )

    async def request_text(self, url: str) -> tuple[int, str, dict[str, str]]:
        """Fetch ``url`` without rendering it. Used for ``robots.txt``."""
        connection = await self._open_connection()
        return await connection.request_text(url)

    # ------------------------------------------------------------------ #
    # Teardown
    # ------------------------------------------------------------------ #
    async def aclose(self) -> None:
        """Close the session and release its slot. Idempotent.

        Safe to call twice, and safe to call when the browser has already died:
        the pipeline calls this from a ``finally`` and has a report to finish, so
        a teardown failure must not replace the run's actual outcome.
        """
        if self._closed:
            return
        self._closed = True
        connection = self._connection
        self._connection = None
        try:
            if connection is not None:
                await connection.close()
        finally:
            if self.holds_slot:
                self.holds_slot = False
                self._manager.release_slot()


def _http_error(provider: str, status: int, url: str) -> CrawlerError:
    if status == 404:
        return SourceNotFoundError(provider, f"{redact_url(url)} returned 404")
    return SourceUnavailableError(provider, f"{redact_url(url)} returned HTTP {status}")


class BrowserManager:
    """Owns the run's browser provider, its concurrency slot and its diagnostics."""

    def __init__(
        self,
        settings: Any,
        *,
        provider: BrowserProvider | None = None,
        run_id: str | None = None,
    ) -> None:
        self.settings = settings
        self.logger = get_logger("browser.manager")
        self._provider = provider if provider is not None else _build_provider(settings)
        max_sessions = max(1, int(getattr(settings.browser, "max_sessions", 1)))
        self._semaphore = asyncio.Semaphore(max_sessions)
        self.max_sessions = max_sessions
        self._sessions: list[BrowserSession] = []
        self._closed = False
        self.debug = DebugRecorder(
            Path(settings.browser.debug_dir),
            run_id=run_id,
            enabled=bool(settings.browser.debug_artifacts),
            max_bytes=int(settings.browser.debug_max_bytes),
            max_files=int(settings.browser.debug_max_files),
        )
        self.run_id = self.debug.run_id

    @property
    def provider(self) -> BrowserProvider:
        return self._provider

    def is_available(self) -> tuple[bool, str]:
        return self._provider.is_available()

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    async def start(self, *, browser_sources: int = 0) -> None:
        """Prepare the run: prune old diagnostics and warn about serialization."""
        if self.settings.browser.debug_artifacts:
            prune_debug_dirs(
                Path(self.settings.browser.debug_dir),
                keep=int(self.settings.browser.debug_keep_runs),
            )
        if browser_sources > self.max_sessions:
            # Say it out loud. The pipeline reports its own concurrency and will
            # look parallel; browser sources will not be, and a silent difference
            # between what the log implies and what happens is how a "slow run"
            # turns into a bug report.
            self.logger.warning(
                "browser sources will run sequentially",
                extra={
                    "browser_sources": browser_sources,
                    "max_sessions": self.max_sessions,
                    "hint": "raise LEAD_BROWSER__MAX_SESSIONS with a Pro license key",
                },
            )

    async def session_for(
        self,
        spec: SessionSpec,
        *,
        login_wall: LoginWall | None = None,
        reuse: bool = True,
    ) -> BrowserSession:
        """Return a session for ``spec``, opening one if needed.

        Blocking here is intentional: the slot is held for the session's whole
        lifetime because acquiring a browser is expensive and acquiring one per
        page would thrash the limit rather than respect it.
        """
        if reuse:
            for session in self._sessions:
                if session.provider_name == spec.provider and not session.closed:
                    return session

        connection = await self.connect(spec)
        session = BrowserSession(
            spec,
            connection=connection,
            manager=self,
            auth_guard=None if login_wall is None else AuthGuard(spec.provider, login_wall),
            recorder=self.debug,
            nav_timeout_ms=_nav_timeout_ms(self.settings, spec),
        )
        self._sessions.append(session)
        return session

    async def connect(self, spec: SessionSpec) -> BrowserConnection:
        """Acquire a slot and open a raw connection. Releases it again on failure."""
        await self._semaphore.acquire()
        try:
            return await self._provider.connect(spec)
        except BaseException:
            self._semaphore.release()
            raise

    def release_slot(self) -> None:
        self._semaphore.release()

    async def aclose(self) -> None:
        """Close every live session. Idempotent."""
        if self._closed:
            return
        self._closed = True
        for session in list(self._sessions):
            try:
                await session.aclose()
            except Exception as exc:
                self.logger.debug("session close failed", extra={"error": str(exc)})
        self._sessions.clear()
        try:
            self.debug.flush()
        except Exception as exc:
            self.logger.warning("could not write diagnostics manifest", extra={"error": str(exc)})


def build_session_spec(
    settings: Any,
    provider: str,
    *,
    persistent: bool = False,
    headless: bool | None = None,
    stealth: bool = True,
    user_agent: str | None = None,
    profile_dir: Path | None = None,
) -> SessionSpec:
    """Assemble a :class:`SessionSpec` for ``provider`` from settings.

    The single place the settings model is read for browser configuration, so an
    adapter never has to know whether the profile root is overridden, which
    timeout applies, or how the viewport is spelled.
    """
    browser = settings.browser
    resolved_headless = bool(browser.headless if headless is None else headless)
    resolved_profile = profile_dir
    if resolved_profile is None and persistent:
        # ``user_data_dir`` is the operator's explicit override; otherwise each
        # source gets its own subdirectory so two sources never share cookies.
        override = getattr(browser, "user_data_dir", None)
        resolved_profile = Path(override) if override else Path(browser.profile_root) / provider
    return SessionSpec(
        provider=provider,
        persistent=persistent,
        user_data_dir=resolved_profile,
        headless=resolved_headless,
        stealth=stealth,
        user_agent=user_agent,
        viewport=Viewport(),
    )


def _build_provider(settings: Any) -> BrowserProvider:
    name = str(getattr(settings.browser, "provider", "cloakbrowser"))
    try:
        factory = _PROVIDERS[name]
    except KeyError:
        known = ", ".join(sorted(_PROVIDERS))
        raise CrawlerError(
            "browser", f"unknown browser provider {name!r} (known: {known})"
        ) from None
    return factory(settings.browser)


def _nav_timeout_ms(settings: Any, spec: SessionSpec) -> int:
    if spec.nav_timeout_ms is not None:
        return spec.nav_timeout_ms
    return int(float(settings.browser.nav_timeout) * 1000)


_manager: BrowserManager | None = None


def get_browser_manager(settings: Any) -> BrowserManager:
    """The process-wide manager for this run, created on first use."""
    global _manager
    if _manager is None:
        _manager = BrowserManager(settings)
    return _manager


def reset_browser_manager() -> None:
    """Forget the shared manager. Test-only: the manager is process-wide."""
    global _manager
    _manager = None
