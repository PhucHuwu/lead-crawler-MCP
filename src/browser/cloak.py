"""CloakBrowser browser provider.

**The only module in this project that imports ``cloakbrowser`` or
``playwright``.** Everything the crawlers touch comes through
:mod:`src.browser.base`, which is what makes the MCP conversion later a matter
of writing one more provider rather than rewriting every adapter.

Both imports are deferred into function bodies rather than performed at module
scope. That is load-bearing, not stylistic: ``src/crawlers/__init__.py`` eagerly
imports every adapter in order to register it, so a module-scope ``import
playwright`` here would make ``--source mock`` fail on any machine that has not
installed the optional ``browser`` extra.

CloakBrowser wraps a stealth-patched Chromium and returns ordinary Playwright
objects, so the adapters below are thin delegations. Two details of its lifecycle
are worth recording because the rest of this module is shaped by them:

* ``launch_async`` returns a Playwright ``Browser`` whose ``close`` is patched to
  also stop the Playwright instance it started internally — so closing the
  browser is the complete teardown, and there is no separate ``playwright.stop``
  to call.
* ``launch_persistent_context_async`` returns a ``BrowserContext`` with the same
  patched-``close`` treatment. A persistent context is its own browser process,
  which is why persistent contexts cannot share a process and why the free
  tier's one-concurrent-session limit shows up as serialization between sources.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

from src.browser.base import (
    DEFAULT_TIMEOUT_MS,
    BrowserConnection,
    SessionSpec,
)
from src.utils.errors import ConfigError, LeadCrawlerError, SourceUnavailableError
from src.utils.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover - typing only, keeps third-party imports out of runtime
    from playwright.async_api import Browser, BrowserContext, ElementHandle, Page

# Playwright pins these parameters to string literals. The public protocols in
# :mod:`src.browser.base` take plain ``str`` on purpose — an adapter should not
# have to import a Playwright type to ask for a wait state, and the MCP provider
# will not have these literals at all. So the narrowing happens here, at the one
# boundary where a Playwright call is actually made, instead of leaking outward.
_WaitUntil = Literal["commit", "domcontentloaded", "load", "networkidle"]
_SelectorState = Literal["attached", "detached", "hidden", "visible"]
_LoadState = Literal["domcontentloaded", "load", "networkidle"]


class PlaywrightElement:
    """Adapts a Playwright ``ElementHandle`` to :class:`BrowserElement`."""

    __slots__ = ("_element",)

    def __init__(self, element: ElementHandle) -> None:
        self._element = element

    async def inner_text(self) -> str:
        return await self._element.inner_text()

    async def text_content(self) -> str | None:
        return await self._element.text_content()

    async def get_attribute(self, name: str) -> str | None:
        return await self._element.get_attribute(name)

    async def is_visible(self) -> bool:
        return await self._element.is_visible()


class PlaywrightPage:
    """Adapts a Playwright ``Page`` to :class:`BrowserPage`.

    Every method is a 1:1 delegation, with two deliberate translations:

    * :meth:`goto` returns the response status rather than Playwright's
      ``Response`` object, because a response is a live handle and cannot cross
      a process boundary — the MCP provider has to be able to satisfy this same
      signature.
    * :meth:`wait_for_selector` returns ``None`` instead of raising on timeout.
      "The selector did not appear" is a decision for the caller, and
      :func:`src.browser.helpers.wait_for` is where that decision is made and
      recorded.
    """

    __slots__ = ("_page",)

    def __init__(self, page: Page) -> None:
        self._page = page

    @property
    def url(self) -> str:
        return self._page.url

    async def goto(
        self, url: str, *, wait_until: str = "domcontentloaded", timeout: float | None = None
    ) -> int | None:
        response = await self._page.goto(
            url, wait_until=cast("_WaitUntil", wait_until), timeout=timeout
        )
        return None if response is None else response.status

    async def set_content(self, html: str, *, wait_until: str = "domcontentloaded") -> None:
        await self._page.set_content(html, wait_until=cast("_WaitUntil", wait_until))

    async def content(self) -> str:
        return await self._page.content()

    async def title(self) -> str:
        return await self._page.title()

    async def query_selector_all(self, selector: str) -> list[Any]:
        elements = await self._page.query_selector_all(selector)
        return [PlaywrightElement(element) for element in elements]

    async def wait_for_selector(
        self, selector: str, *, state: str = "visible", timeout: float | None = None
    ) -> PlaywrightElement | None:
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError

        try:
            element = await self._page.wait_for_selector(
                selector, state=cast("_SelectorState", state), timeout=timeout
            )
        except PlaywrightTimeoutError:
            return None
        return None if element is None else PlaywrightElement(element)

    async def fill(self, selector: str, value: str) -> None:
        await self._page.fill(selector, value)

    async def click(self, selector: str) -> None:
        await self._page.click(selector)

    async def press(self, selector: str, key: str) -> None:
        await self._page.press(selector, key)

    async def evaluate(self, expression: str) -> Any:
        return await self._page.evaluate(expression)

    async def wait_for_load_state(
        self, state: str = "networkidle", timeout: float | None = None
    ) -> None:
        await self._page.wait_for_load_state(cast("_LoadState", state), timeout=timeout)

    async def screenshot(self, *, full_page: bool = False) -> bytes:
        return await self._page.screenshot(full_page=full_page)

    async def close(self) -> None:
        try:
            await self._page.close()
        except Exception as exc:  # a page whose browser already died is already closed
            get_logger("browser.cloak").debug("page close failed", extra={"error": str(exc)})


class _CloakConnection:
    """A live CloakBrowser context plus, for non-persistent sessions, its browser."""

    __slots__ = ("_browser", "_closed", "_context", "_provider")

    def __init__(self, *, provider: str, context: BrowserContext, browser: Browser | None) -> None:
        self._provider = provider
        self._context = context
        self._browser = browser
        self._closed = False

    async def new_page(self) -> PlaywrightPage:
        return PlaywrightPage(await self._context.new_page())

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        logger = get_logger("browser.cloak")
        try:
            await self._context.close()
        except Exception as exc:
            # A browser that already crashed must not mask the run's result; the
            # caller is in a ``finally`` and has a report to finish.
            logger.debug("browser context close failed", extra={"error": str(exc)})
        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception as exc:
                logger.debug("browser close failed", extra={"error": str(exc)})

    async def request_text(self, url: str) -> tuple[int, str, dict[str, str]]:
        response = await self._context.request.get(url)
        try:
            return response.status, await response.text(), dict(response.headers)
        finally:
            await response.dispose()


class CloakBrowserProvider:
    """Launches stealth Chromium through the official ``cloakbrowser`` package."""

    name = "cloakbrowser"

    def __init__(self, settings: Any) -> None:
        # Typed loosely to avoid importing the settings model here; the only
        # fields read are documented on ``BrowserSettings``.
        self._settings = settings
        self.logger = get_logger("browser.cloak")

    # ------------------------------------------------------------------ #
    # Availability
    # ------------------------------------------------------------------ #
    def is_available(self) -> tuple[bool, str]:
        """Whether a stealth Chromium binary is present and usable.

        Filesystem checks only. ``ensure_binary()`` would download ~150 MB when
        the binary is missing, and this runs during ``--list-sources`` and config
        validation — a listing that silently downloads is a listing nobody can
        run offline.

        A missing binary is a *configuration* problem with a one-line fix, not a
        transient failure, so the message names the command rather than saying
        "unavailable".
        """
        try:
            from cloakbrowser.config import get_local_binary_override
            from cloakbrowser.download import binary_info
        except ImportError as exc:
            self.logger.debug("cloakbrowser import failed", extra={"error": str(exc)})
            return False, (
                "the browser extra is not installed; "
                "install it with: uv pip install -e '.[browser]'"
            )

        override = get_local_binary_override()
        if override:
            if Path(override).exists():
                return True, ""
            return False, (f"CLOAKBROWSER_BINARY_PATH points at {override!r}, which does not exist")

        if binary_info()["installed"]:
            return True, ""
        return False, (
            "the stealth Chromium binary is not downloaded; "
            "run: cloakbrowser install  (downloads ~150 MB, once)"
        )

    # ------------------------------------------------------------------ #
    # Connection
    # ------------------------------------------------------------------ #
    async def connect(self, spec: SessionSpec) -> BrowserConnection:
        """Launch the browser described by ``spec`` and return a connection."""
        from cloakbrowser import launch_async, launch_persistent_context_async

        launch_kwargs = self._launch_kwargs(spec)
        try:
            if spec.persistent:
                if spec.user_data_dir is None:
                    raise ConfigError(
                        f"source {spec.provider!r} needs a persistent browser session "
                        f"but no profile directory was given"
                    )
                spec.user_data_dir.mkdir(parents=True, exist_ok=True)
                context = await launch_persistent_context_async(
                    os.fspath(spec.user_data_dir), **launch_kwargs, **self._context_kwargs(spec)
                )
                return _CloakConnection(provider=spec.provider, context=context, browser=None)

            browser = await launch_async(**launch_kwargs)
            try:
                context = await browser.new_context(**self._context_kwargs(spec))
            except BaseException:
                await browser.close()
                raise
            return _CloakConnection(provider=spec.provider, context=context, browser=browser)
        except LeadCrawlerError:
            raise
        except Exception as exc:
            raise SourceUnavailableError(
                spec.provider, f"could not start the browser: {exc}"
            ) from exc

    # ------------------------------------------------------------------ #
    # Argument assembly
    # ------------------------------------------------------------------ #
    def _launch_kwargs(self, spec: SessionSpec) -> dict[str, Any]:
        """Arguments shared by the launch and persistent-launch entry points."""
        kwargs: dict[str, Any] = {
            "headless": spec.headless,
            "stealth_args": spec.stealth,
            "geoip": spec.geoip,
            "humanize": spec.humanize,
        }
        if spec.locale:
            kwargs["locale"] = spec.locale
        if spec.timezone:
            kwargs["timezone"] = spec.timezone
        if spec.proxy is not None:
            proxy: dict[str, str] = {"server": spec.proxy.server}
            if spec.proxy.username:
                proxy["username"] = spec.proxy.username
            if spec.proxy.password:
                proxy["password"] = spec.proxy.password
            kwargs["proxy"] = proxy
        if spec.extra.get("args"):
            kwargs["args"] = spec.extra["args"]
        if (key := self._license_key()) is not None:
            kwargs["license_key"] = key
        return kwargs

    def _context_kwargs(self, spec: SessionSpec) -> dict[str, Any]:
        """Arguments that describe the browsing context rather than the process.

        ``viewport`` is only passed when the caller asked for one. CloakBrowser
        treats "unset" and "None" differently — unset lets a headed window size
        itself, ``None`` disables viewport emulation outright — so sending a
        value unconditionally would silently change headed behaviour.
        """
        kwargs: dict[str, Any] = {}
        if spec.user_agent:
            kwargs["user_agent"] = spec.user_agent
        if spec.viewport is not None:
            kwargs["viewport"] = {"width": spec.viewport.width, "height": spec.viewport.height}
        if spec.color_scheme:
            kwargs["color_scheme"] = spec.color_scheme
        return kwargs

    def _license_key(self) -> str | None:
        secret = getattr(self._settings, "license_key", None)
        return None if secret is None else str(secret.get_secret_value())

    def nav_timeout_ms(self, spec: SessionSpec) -> int:
        """Navigation timeout for ``spec``, falling back to the provider default."""
        if spec.nav_timeout_ms is not None:
            return spec.nav_timeout_ms
        configured = getattr(self._settings, "nav_timeout", None)
        return int(configured * 1000) if configured else DEFAULT_TIMEOUT_MS
