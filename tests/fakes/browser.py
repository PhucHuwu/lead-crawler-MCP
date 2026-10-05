"""An in-process browser double for the ``BrowserPage`` protocol.

Why this exists at all: the adapters drive a real browser, and a test suite that
needs Chromium to assert "this selector picks up the company name" is a suite
nobody runs. So the seam the adapters were built on gets a second implementation,
and the adapters cannot tell the difference.

What makes it honest rather than decorative:

* it is a **real DOM**, parsed with :mod:`html.parser` and matched with a real (if
  small) CSS engine. A test that asserts on a selector exercises selector
  semantics, not a hardcoded lookup table.
* the double is only the **provider**. :class:`~src.browser.session.BrowserManager`,
  the concurrency slot, the auth guard, the page lifecycle and the diagnostics
  are the genuine article — see :func:`fake_browser`. Faking at the provider
  keeps every layer above it under test.
* it is **fail-closed on unrouted URLs**. A test that forgets to route a URL gets
  a loud :class:`UnroutedUrlError` naming the routes it did register, rather than
  an empty page that quietly turns into a passing assertion about missing data.

It is not a Chromium emulator and does not pretend to be one. Layout, PDF,
frames, downloads and JavaScript execution are absent; :meth:`FakePage.evaluate`
returns whatever the test registered. Behaviour the adapters actually depend on
is pinned against the real browser by
``tests/browser/test_page_contract.py``.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import TYPE_CHECKING, Any

from src.browser.base import BrowserElement, BrowserPage, SessionSpec
from src.browser.session import BrowserManager
from src.utils.logging import get_logger

if TYPE_CHECKING:
    from src.config import Settings

logger = get_logger("tests.fakes.browser")

#: Elements that never have children, so a start tag must not push onto the
#: open-element stack. Getting this wrong silently nests the rest of the
#: document inside a ``<link>``.
_VOID_TAGS = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param",
     "source", "track", "wbr"}
)

#: Elements whose text is not rendered, so ``inner_text`` must skip them.
_INVISIBLE_TAGS = frozenset({"script", "style", "template", "noscript", "head", "title"})

#: A compound selector: optional tag, then any number of ``.class``, ``#id`` and
#: ``[attr op value]`` parts.
_COMPOUND_RE = re.compile(
    r"""
    (?P<tag>[a-zA-Z*][\w-]*)?                       # tag name or *
    (?P<rest>(?:\#[\w-]+|\.[\w-]+|\[[^\]]+\])*)     # #id .class [attr]
    """,
    re.VERBOSE,
)

_ATTR_RE = re.compile(
    r"""
    \[
      \s*(?P<name>[\w:-]+)\s*
      (?:(?P<op>[~^$*|]?=)\s*
         (?:"(?P<quoted>[^"]*)"|'(?P<single>[^']*)'|(?P<bare>[^\]]*?))\s*
      )?
    \]
    """,
    re.VERBOSE,
)


class UnroutedUrlError(RuntimeError):
    """A URL was navigated to that no route serves.

    Deliberately an error rather than a synthetic 404. A 404 would make a
    forgotten route indistinguishable from a site that genuinely answered 404,
    and the resulting test failure would point at the adapter instead of at the
    fixture.
    """


# --------------------------------------------------------------------------- #
# DOM
# --------------------------------------------------------------------------- #
@dataclass
class _Node:
    """One element in the parsed document."""

    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list[_Node] = field(default_factory=list)
    parent: _Node | None = None
    #: Text directly inside this element, before any child element.
    text: str = ""

    def descendants(self) -> Iterator[_Node]:
        """Every element below this one, in document order."""
        for child in self.children:
            yield child
            yield from child.descendants()

    def ancestors(self) -> Iterator[_Node]:
        node = self.parent
        while node is not None:
            yield node
            node = node.parent

    @property
    def classes(self) -> list[str]:
        return (self.attrs.get("class") or "").split()


class _DomBuilder(HTMLParser):
    """Builds a :class:`_Node` tree from a document.

    Tolerant by design: real pages omit closing tags, and a fake that refused to
    parse them would fail on the very fixtures it exists to serve. A mismatched
    end tag closes back to the nearest matching ancestor; anything else is
    ignored.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node(tag="#document")
        self._stack: list[_Node] = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = _Node(
            tag=tag.casefold(),
            attrs={key.casefold(): (value or "") for key, value in attrs},
            parent=self._stack[-1],
        )
        self._stack[-1].children.append(node)
        if node.tag not in _VOID_TAGS:
            self._stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if self._stack[-1].tag == tag.casefold():
            self._stack.pop()

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        for depth in range(len(self._stack) - 1, 0, -1):
            if self._stack[depth].tag == tag:
                del self._stack[depth:]
                return

    def handle_data(self, data: str) -> None:
        self._stack[-1].text += data


def parse_html(html: str) -> _Node:
    """Parse ``html`` into a document node."""
    builder = _DomBuilder()
    builder.feed(html)
    builder.close()
    return builder.root


def _collapse(text: str) -> str:
    return " ".join(text.split())


def _rendered_text(node: _Node) -> str:
    """Rendered text of a subtree, mirroring what a browser would show."""
    if node.tag in _INVISIBLE_TAGS:
        return ""
    parts = [node.text]
    parts.extend(_rendered_text(child) for child in node.children)
    return _collapse(" ".join(parts))


def _is_visible(node: _Node) -> bool:
    """Whether the element is rendered.

    A small approximation of what a browser computes: an element is hidden when
    it or an ancestor carries the ``hidden`` attribute or an inline
    ``display:none``. Enough for the cases adapters check (a login form that is
    present but hidden), and honest about being an approximation.
    """
    for ancestor in (node, *node.ancestors()):
        if "hidden" in ancestor.attrs:
            return False
        style = ancestor.attrs.get("style", "").replace(" ", "").casefold()
        if "display:none" in style or "visibility:hidden" in style:
            return False
    return True


# --------------------------------------------------------------------------- #
# Selector engine
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class _Compound:
    tag: str | None
    id: str | None
    classes: tuple[str, ...]
    attrs: tuple[tuple[str, str | None, str | None], ...]

    def matches(self, node: _Node) -> bool:
        if self.tag not in (None, "*") and node.tag != self.tag:
            return False
        if self.id is not None and node.attrs.get("id") != self.id:
            return False
        if any(name not in node.classes for name in self.classes):
            return False
        for name, operator, value in self.attrs:
            actual = node.attrs.get(name)
            if actual is None:
                return False
            if operator is None:
                continue
            assert value is not None
            if operator == "=" and actual != value:
                return False
            if operator == "~=" and value not in actual.split():
                return False
            if operator == "^=" and not actual.startswith(value):
                return False
            if operator == "$=" and not actual.endswith(value):
                return False
            if operator == "*=" and value not in actual:
                return False
            if operator == "|=" and actual != value and not actual.startswith(f"{value}-"):
                return False
        return True


def _parse_compound(text: str) -> _Compound:
    match = _COMPOUND_RE.fullmatch(text.strip())
    if match is None or not match.group(0):
        raise ValueError(f"unsupported selector: {text!r}")
    tag = match.group("tag")
    identifier: str | None = None
    classes: list[str] = []
    attrs: list[tuple[str, str | None, str | None]] = []
    rest = match.group("rest")
    for token in re.finditer(r"\#[\w-]+|\.[\w-]+|\[[^\]]+\]", rest):
        piece = token.group(0)
        if piece.startswith("#"):
            identifier = piece[1:]
        elif piece.startswith("."):
            classes.append(piece[1:])
        else:
            attr = _ATTR_RE.fullmatch(piece)
            if attr is None:
                raise ValueError(f"unsupported attribute selector: {piece!r}")
            value = attr.group("quoted")
            if value is None:
                value = attr.group("single")
            if value is None:
                value = attr.group("bare")
            attrs.append((attr.group("name").casefold(), attr.group("op"), value))
    return _Compound(
        tag=None if tag is None else tag.casefold(),
        id=identifier,
        classes=tuple(classes),
        attrs=tuple(attrs),
    )


def _parse_selector(selector: str) -> list[list[tuple[str | None, _Compound]]]:
    """Parse a selector list into alternatives of ``(combinator, compound)`` steps.

    Supports the subset the adapters use — descendant and child combinators,
    tag/class/id/attribute tests and comma-separated alternatives. It is not the
    full CSS grammar, and it raises on anything it does not understand rather
    than silently matching nothing: a selector a test cannot express should be
    loud, because the alternative is a test that passes against an empty result.
    """
    alternatives: list[list[tuple[str | None, _Compound]]] = []
    for group in selector.split(","):
        tokens = [token for token in re.split(r"(\s*>\s*|\s+)", group.strip()) if token.strip()]
        steps: list[tuple[str | None, _Compound]] = []
        combinator: str | None = None
        for token in tokens:
            stripped = token.strip()
            if stripped == ">":
                combinator = ">"
                continue
            steps.append((combinator, _parse_compound(stripped)))
            combinator = " "
        if not steps:
            raise ValueError(f"empty selector: {selector!r}")
        alternatives.append(steps)
    return alternatives


def _matches_steps(node: _Node, steps: list[tuple[str | None, _Compound]]) -> bool:
    """Whether ``node`` is the last step of a chain, walking ancestors backwards."""
    combinator, compound = steps[-1]
    if not compound.matches(node):
        return False
    remaining = steps[:-1]
    if not remaining:
        return True
    if combinator == ">":
        parent = node.parent
        return parent is not None and _matches_steps(parent, remaining)
    # Descendant: any ancestor may satisfy the rest of the chain.
    return any(_matches_steps(ancestor, remaining) for ancestor in node.ancestors())


def select(root: _Node, selector: str) -> list[_Node]:
    """Every node under ``root`` matching ``selector``, in document order."""
    alternatives = _parse_selector(selector)
    return [
        node
        for node in root.descendants()
        if any(_matches_steps(node, steps) for steps in alternatives)
    ]


# --------------------------------------------------------------------------- #
# Page objects
# --------------------------------------------------------------------------- #
class FakeElement:
    """Adapts a DOM node to :class:`~src.browser.base.BrowserElement`."""

    __slots__ = ("_node",)

    def __init__(self, node: _Node) -> None:
        self._node = node

    async def inner_text(self) -> str:
        return _rendered_text(self._node)

    async def text_content(self) -> str | None:
        return _rendered_text(self._node) or None

    async def get_attribute(self, name: str) -> str | None:
        return self._node.attrs.get(name.casefold())

    async def is_visible(self) -> bool:
        return _is_visible(self._node)

    @property
    def html(self) -> str:
        """The node's own markup. Test-only convenience, not part of the protocol."""
        return _render(self._node)

    def __repr__(self) -> str:
        return f"<FakeElement {self._node.tag} {self._node.attrs!r}>"


def _render(node: _Node) -> str:
    attrs = "".join(f' {key}="{value}"' for key, value in node.attrs.items())
    inner = node.text + "".join(_render(child) for child in node.children)
    return f"<{node.tag}{attrs}>{inner}</{node.tag}>"


@dataclass
class FakeRoute:
    """One served URL."""

    status: int = 200
    body: str = ""
    headers: dict[str, str] = field(default_factory=lambda: {"content-type": "text/html"})
    #: Where the browser ends up. Set it to simulate a redirect; the navigation
    #: then reports this as ``page.url``, which is what the auth guard reads.
    final_url: str | None = None
    #: Raised instead of serving. Simulates a dead connection rather than a
    #: response, which is the difference between ``goto`` failing and returning 404.
    error: str | None = None


class FakePage:
    """An in-process :class:`~src.browser.base.BrowserPage`.

    Records every interaction in :attr:`calls` so a test can assert on what the
    adapter asked the browser to do — the state of a search form, the number of
    ``next`` clicks — without a live page to inspect afterwards.
    """

    def __init__(self, connection: FakeConnection, *, url: str = "about:blank") -> None:
        self._connection = connection
        self._url = url
        self._html = ""
        self._root = parse_html("")
        self._closed = False
        #: ``(method, *args)`` for every interaction, in order.
        self.calls: list[tuple[str, ...]] = []
        #: Handlers fired on ``click``, keyed by the selector they watch.
        self._click_handlers: dict[str, Callable[[FakePage], Any]] = {}
        #: Values returned by :meth:`evaluate`, keyed by exact expression.
        self.evaluate_results: dict[str, Any] = {}

    # -- protocol ------------------------------------------------------- #
    @property
    def url(self) -> str:
        return self._url

    @property
    def closed(self) -> bool:
        return self._closed

    async def goto(
        self, url: str, *, wait_until: str = "domcontentloaded", timeout: float | None = None
    ) -> int | None:
        self.calls.append(("goto", url))
        route = self._connection.route_for(url)
        if route.error is not None:
            raise UnroutedUrlError(f"{url} failed: {route.error}")
        self._html = route.body
        self._root = parse_html(_doc(route.body))
        self._url = route.final_url or url
        self._connection.history.append(self._url)
        return route.status

    async def set_content(self, html: str, *, wait_until: str = "domcontentloaded") -> None:
        """Replace the document. Leaves :attr:`url` alone, as Playwright does."""
        self.calls.append(("set_content",))
        self._html = html
        self._root = parse_html(_doc(html))

    async def content(self) -> str:
        return self._html

    async def title(self) -> str:
        found = select(self._root, "title")
        return _collapse(found[0].text) if found else ""

    async def query_selector_all(self, selector: str) -> list[BrowserElement]:
        return [FakeElement(node) for node in select(self._root, selector)]

    async def wait_for_selector(
        self, selector: str, *, state: str = "visible", timeout: float | None = None
    ) -> BrowserElement | None:
        """Match immediately. Returns ``None`` when nothing matches.

        There is no waiting: a fake has no clock, and sleeping would make the
        suite slower without testing anything the real browser does not already
        cover in ``test_page_contract.py``. What matters for the adapters is that
        "absent" is ``None`` rather than an exception, and that is preserved.
        """
        self.calls.append(("wait_for_selector", selector, state))
        nodes = select(self._root, selector)
        if not nodes:
            return None
        if state in ("attached", "detached"):
            return FakeElement(nodes[0])
        visible = [node for node in nodes if _is_visible(node)]
        if state == "hidden":
            return None if visible else FakeElement(nodes[0])
        return FakeElement(visible[0]) if visible else None

    async def fill(self, selector: str, value: str) -> None:
        self.calls.append(("fill", selector, value))

    async def click(self, selector: str) -> None:
        self.calls.append(("click", selector))
        handler = self._click_handlers.get(selector)
        if handler is not None:
            result = handler(self)
            if hasattr(result, "__await__"):
                await result

    async def press(self, selector: str, key: str) -> None:
        self.calls.append(("press", selector, key))

    async def evaluate(self, expression: str) -> Any:
        self.calls.append(("evaluate", expression))
        return self.evaluate_results.get(expression)

    async def wait_for_load_state(
        self, state: str = "networkidle", timeout: float | None = None
    ) -> None:
        self.calls.append(("wait_for_load_state", state))

    async def screenshot(self, *, full_page: bool = False) -> bytes:
        self.calls.append(("screenshot",))
        # A one-pixel PNG: real enough that a test asserting "bytes were written"
        # is asserting something, small enough to keep the suite quick.
        return _ONE_PIXEL_PNG

    async def close(self) -> None:
        self._closed = True

    # -- test hooks ----------------------------------------------------- #
    def on_click(self, selector: str, handler: Callable[[FakePage], Any]) -> None:
        """Run ``handler`` when ``selector`` is clicked.

        How a test simulates the page reacting to a user — paginating, opening a
        detail panel — without a JavaScript engine behind the fake.
        """
        self._click_handlers[selector] = handler

    def selectors(self, method: str) -> list[str]:
        """Selectors passed to ``method``, in order. Convenience for assertions."""
        return [call[1] for call in self.calls if call[0] == method]

    def __repr__(self) -> str:
        return f"<FakePage {self._url!r}>"


class FakeConnection:
    """Routes pages to :class:`FakeRoute` bodies. One per source session."""

    def __init__(self, routes: dict[str, FakeRoute] | None = None) -> None:
        self.routes: dict[str, FakeRoute] = dict(routes or {})
        #: Every page opened on this connection, oldest first.
        self.pages: list[FakePage] = []
        #: Every URL navigated to, across all pages.
        self.history: list[str] = []
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    def route_for(self, url: str) -> FakeRoute:
        """The route serving ``url``.

        Lookup is trailing-slash insensitive, because a browser normalizes
        ``https://acme.com`` to ``https://acme.com/`` and a test should not have
        to spell it the way the URL parser does.
        """
        for candidate in (url, url.rstrip("/"), f"{url.rstrip('/')}/"):
            if candidate in self.routes:
                return self.routes[candidate]
        known = ", ".join(sorted(self.routes)) or "<none registered>"
        raise UnroutedUrlError(f"no route for {url!r}; routes: {known}")

    async def new_page(self) -> FakePage:
        if self._closed:
            raise RuntimeError("connection is closed")
        page = FakePage(self)
        self.pages.append(page)
        return page

    async def close(self) -> None:
        self._closed = True

    async def request_text(self, url: str) -> tuple[int, str, dict[str, str]]:
        route = self.route_for(url)
        if route.error is not None:
            raise UnroutedUrlError(f"{url} failed: {route.error}")
        return route.status, route.body, dict(route.headers)


class FakeBrowserProvider:
    """A :class:`~src.browser.base.BrowserProvider` serving registered routes.

    One connection per ``SessionSpec``, so a two-source run gets two independent
    route tables and two page histories — which is what makes the sequential
    browser test in ``tests/browser/`` meaningful.
    """

    name = "fake"

    def __init__(self, routes: dict[str, FakeRoute] | None = None) -> None:
        self.routes: dict[str, FakeRoute] = dict(routes or {})
        self.connections: list[FakeConnection] = []
        self.specs: list[SessionSpec] = []
        #: Every connection ``connect`` handed out, so a test can inspect what
        #: the fake provider was asked to launch.
        self.available = True
        self.unavailable_reason = ""

    def is_available(self) -> tuple[bool, str]:
        return (True, "") if self.available else (False, self.unavailable_reason)

    async def connect(self, spec: SessionSpec) -> FakeConnection:
        self.specs.append(spec)
        connection = FakeConnection(self.routes)
        self.connections.append(connection)
        return connection

    # -- registration --------------------------------------------------- #
    def route(
        self,
        url: str,
        body: str = "",
        *,
        status: int = 200,
        final_url: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> FakeRoute:
        """Serve ``url``. Returns the route so a test can adjust it later."""
        created = FakeRoute(
            status=status,
            body=body,
            final_url=final_url,
            headers=headers or {"content-type": "text/html"},
        )
        self.routes[url] = created
        return created

    def fail(self, url: str, *, error: str = "net::ERR_CONNECTION_REFUSED") -> FakeRoute:
        """Make navigating to ``url`` fail the way a dead host does."""
        created = FakeRoute(error=error)
        self.routes[url] = created
        return created

    def redirect(self, url: str, *, to: str, status: int = 302) -> FakeRoute:
        """Make ``url`` land on ``to`` — the shape an expired session takes."""
        target = self.routes.get(to) or self.routes.get(to.rstrip("/"))
        return self.route(
            url,
            body=target.body if target is not None else "",
            status=status,
            final_url=to,
        )

    @property
    def pages(self) -> list[FakePage]:
        """Every page opened across every connection."""
        return [page for connection in self.connections for page in connection.pages]

    @property
    def history(self) -> list[str]:
        """Every URL visited, across every connection, in order."""
        return [url for connection in self.connections for url in connection.history]


def fake_browser(settings: Settings, provider: FakeBrowserProvider | None = None) -> BrowserManager:
    """A real :class:`BrowserManager` driving a :class:`FakeBrowserProvider`.

    Faking the *provider* rather than the manager is the point: everything above
    the provider — the concurrency semaphore, session reuse, the auth guard, the
    debug recorder, the run counters — is the production implementation, so a bug
    there fails a test instead of hiding behind a fake.
    """
    return BrowserManager(settings, provider=provider or FakeBrowserProvider())


def _doc(body: str) -> str:
    """Wrap a fragment so the parser always has a document to build."""
    return body if body.lstrip().casefold().startswith(("<!doctype", "<html")) else f"<html>{body}</html>"


#: A 1x1 transparent PNG. Fixed bytes rather than a generated image so the
#: screenshot artifact is byte-identical between runs.
_ONE_PIXEL_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000a49444154789c6300010000050001"
    "0d0a2db40000000049454e44ae426082"
)
