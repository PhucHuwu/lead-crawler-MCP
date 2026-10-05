"""Provider-agnostic browser contracts.

This module is the seam the whole crawler architecture turns on, so it is worth
stating what it is for.

The crawler must drive a real browser today (CloakBrowser) and must be
convertible to a browser-behind-MCP tomorrow without rewriting the crawlers.
Those two goals pick the seam:

* If only the **provider** were abstracted and adapters held live Playwright
  ``Page`` objects, an MCP conversion would mean rewriting every adapter — a
  ``Page`` is a handle to an in-process object and cannot cross a process
  boundary. That is the expensive option masquerading as the cheap one.
* Abstracting the provider *and* exposing a narrow page protocol keeps
  Playwright inside :mod:`src.browser.cloak`. Every method here maps 1:1 onto an
  MCP tool call, so an MCP provider implements these protocols and the adapters
  do not change at all.

The cost is a small API surface and a fake to maintain. The protocols are
deliberately Playwright-shaped (``goto``, ``content``, ``query_selector_all``,
``screenshot``) so the in-process implementation is a 1:1 delegation and so
existing Playwright knowledge transfers directly.

Nothing in this module imports a third-party package. That is load-bearing:
``src/crawlers/__init__.py`` eagerly imports every adapter to register it, so a
module-scope ``import playwright`` here would break ``--source mock`` on any
machine without the browser extra.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

#: Playwright's default navigation timeout. Kept as a module constant so the
#: provider and the session agree on what "no timeout given" means.
DEFAULT_TIMEOUT_MS = 30_000

#: Wait states accepted by :meth:`BrowserPage.goto`. Mirrors Playwright's names
#: rather than inventing our own, so the mapping stays obvious.
WAIT_UNTIL_CHOICES = ("commit", "domcontentloaded", "load", "networkidle")


@dataclass(frozen=True, slots=True)
class ProxyConfig:
    """Proxy settings for a browser session.

    ``password`` is a plain string here rather than a ``SecretStr`` because the
    provider has to hand it to Chromium. It is never logged: the provider builds
    its argument list and the settings redaction covers the ``SecretStr`` the
    value came from.
    """

    server: str
    username: str | None = None
    password: str | None = None


@dataclass(frozen=True, slots=True)
class Viewport:
    """Browser viewport size in CSS pixels."""

    width: int = 1440
    height: int = 900


@dataclass(frozen=True, slots=True)
class SessionSpec:
    """Everything needed to open one source's browser session.

    Provider-agnostic by construction: it names *what* the session needs, not
    how CloakBrowser happens to spell it. An MCP provider reads the same spec.
    """

    #: Crawler slug the session belongs to (``"apollo"``, ``"website"``, ...).
    #: Used for the profile path, the debug subdirectory and error messages.
    provider: str

    #: True opens one browser process per ``user_data_dir`` and keeps it across
    #: runs. Required for a signed-in session — cookies live in that directory.
    persistent: bool = False
    user_data_dir: Path | None = None

    headless: bool = True

    #: Anti-fingerprinting arguments. On by default because the browsers we
    #: drive are automation; ``website`` turns it off deliberately (see the
    #: adapter) since it declares an honest user agent, and a forced-stealth
    #: fingerprint contradicting that header is itself a detection signal.
    stealth: bool = True

    proxy: ProxyConfig | None = None
    locale: str | None = None
    timezone: str | None = None
    geoip: bool = False

    #: Human-like input timing. Off by default: it multiplies every interaction
    #: and the sources we drive do not require it.
    humanize: bool = False

    user_agent: str | None = None
    viewport: Viewport | None = None
    color_scheme: str | None = None

    #: Per-navigation timeout in milliseconds. ``None`` falls back to the
    #: provider default.
    nav_timeout_ms: int | None = None

    #: Extra provider arguments, passed through verbatim. An escape hatch for
    #: provider-specific flags that do not deserve a field on the shared spec.
    extra: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class BrowserElement(Protocol):
    """One element matched by a selector.

    Deliberately tiny: an adapter reads text and attributes off a result row and
    nothing else, so that is all this exposes. A wider surface here would be a
    wider surface for the MCP provider to reimplement.
    """

    async def inner_text(self) -> str:
        """Visible text of the element, whitespace-normalized by the browser."""
        ...

    async def text_content(self) -> str | None:
        """Text content including hidden nodes, or ``None`` if detached."""
        ...

    async def get_attribute(self, name: str) -> str | None:
        """Attribute value, or ``None`` when the attribute is absent."""
        ...

    async def is_visible(self) -> bool:
        """Whether the element is currently rendered."""
        ...


@runtime_checkable
class BrowserPage(Protocol):
    """A single browsing context. The MCP RPC surface.

    Every method maps onto one MCP tool call, which is what makes the later
    conversion mechanical. Add a method here only when an adapter genuinely
    needs it — each addition is work the MCP provider inherits.
    """

    @property
    def url(self) -> str:
        """The page's current URL.

        After :meth:`goto` this is the **final** URL once redirects resolved —
        that is what auth-expiry detection reads. After :meth:`set_content` it is
        unchanged, matching Playwright.
        """
        ...

    async def goto(
        self, url: str, *, wait_until: str = "domcontentloaded", timeout: float | None = None
    ) -> int | None:
        """Navigate to ``url``. Returns the main response's HTTP status."""
        ...

    async def set_content(self, html: str, *, wait_until: str = "domcontentloaded") -> None:
        """Replace the document with ``html``, leaving :attr:`url` unchanged."""
        ...

    async def content(self) -> str:
        """Serialized HTML of the current document."""
        ...

    async def title(self) -> str:
        """Document title. Empty string when there is none."""
        ...

    async def query_selector_all(self, selector: str) -> list[BrowserElement]:
        """All matching elements. Returns ``[]``, never ``None``, when none match."""
        ...

    async def wait_for_selector(
        self, selector: str, *, state: str = "visible", timeout: float | None = None
    ) -> BrowserElement | None:
        """Wait for a match. Returns ``None`` on timeout rather than raising.

        Callers that treat absence as an error go through
        :func:`src.browser.helpers.wait_for`, which raises a typed error and
        records the failed selector for diagnostics. Returning ``None`` here
        keeps "was not there" a decision for the caller instead of an exception
        the caller has to catch.
        """
        ...

    async def fill(self, selector: str, value: str) -> None:
        """Type ``value`` into the input matched by ``selector``."""
        ...

    async def click(self, selector: str) -> None:
        """Click the element matched by ``selector``."""
        ...

    async def press(self, selector: str, key: str) -> None:
        """Press ``key`` while focused on the element matched by ``selector``."""
        ...

    async def evaluate(self, expression: str) -> Any:
        """Evaluate ``expression`` in the page. Must return a JSON-serializable value.

        The escape hatch for reading state the DOM does not expose directly.
        Nothing that returns a live handle is permitted through here, or the
        MCP conversion stops working.
        """
        ...

    async def wait_for_load_state(
        self, state: str = "networkidle", timeout: float | None = None
    ) -> None:
        """Wait until the page reaches ``state``."""
        ...

    async def screenshot(self, *, full_page: bool = False) -> bytes:
        """PNG screenshot bytes."""
        ...

    async def close(self) -> None:
        """Close the page. Idempotent."""
        ...


@runtime_checkable
class BrowserProvider(Protocol):
    """Supplies browser connections. CloakBrowser today, MCP later.

    The split of responsibility is deliberate:

    * the **provider** knows how to bring a browser process up and hand back a
      raw connection for one source (``connect``);
    * :class:`~src.browser.session.BrowserSession` — engine code, shared by
      every provider — owns the concurrency slot, the page lifecycle, the auth
      guard and the diagnostics.

    That is why :meth:`connect` returns a bare :class:`BrowserConnection`
    rather than a session: a second provider implements only this small surface
    and inherits all the surrounding behaviour.
    """

    name: str

    def is_available(self) -> tuple[bool, str]:
        """Whether this provider can run here, and why not if it cannot.

        Must be cheap, offline and side-effect free: no download, no launch, no
        network. It runs during ``--list-sources`` and config validation, so a
        call that fetches a 150 MB binary would make listing sources both slow
        and network-dependent.
        """
        ...

    async def connect(self, spec: SessionSpec) -> BrowserConnection:
        """Bring up whatever ``spec`` needs and return a connection to it."""
        ...


@runtime_checkable
class BrowserConnection(Protocol):
    """A live browser connection for one source.

    Owns the process and context it created, and nothing else. Lifetime is
    managed by :class:`~src.browser.session.BrowserSession`.
    """

    async def new_page(self) -> BrowserPage:
        """Open a new isolated page in this connection's context."""
        ...

    async def close(self) -> None:
        """Tear down the context and any browser process behind it. Idempotent."""
        ...

    async def request_text(self, url: str) -> tuple[int, str, dict[str, str]]:
        """Fetch ``url`` outside any page, as ``(status, body, headers)``.

        Used for ``robots.txt``, which is a plain document that must not be
        rendered. Kept on the connection rather than opened as a page so it does
        not disturb the page an adapter is working in.
        """
        ...
