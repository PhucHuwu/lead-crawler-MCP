"""Shared page utilities for adapters.

Two jobs, both about keeping adapters small and their failures diagnosable.

The first is waiting. ``await asyncio.sleep(5)`` is the wrong tool: it is either
too short on a slow page or pure waste on a fast one, and when it is too short
the failure is a missing field that looks like a data problem. Every wait here is
a condition wait with a bound, and every timeout becomes a structured record
carrying the selector and how many elements actually matched — the difference
between "the selector never appeared" and "it appeared and matched nothing" is
the difference between a site redesign and a parsing bug.

The second is severity. Adapters contain per-item failures: one malformed row
must not end the run. But an authentication failure or a dead browser is
systemic, and swallowing it produces a run that reports success with no data.
:func:`is_fatal_for_source` is the single place that distinction is decided, so
every adapter makes the same call.
"""

from __future__ import annotations

from src.browser.base import BrowserElement, BrowserPage
from src.browser.debug import DebugRecorder
from src.utils.errors import (
    AuthExpiredError,
    CrawlerError,
    SelectorNotFoundError,
    SourceAuthError,
    SourceUnavailableError,
)

#: Errors that mean the source cannot produce anything and must abort the whole
#: crawl for that source, rather than being contained per item.
#:
#: ``AuthExpiredError`` is a ``SourceAuthError``; it is listed separately only
#: because it is the one an operator can fix, which the message says.
_FATAL_FOR_SOURCE: tuple[type[BaseException], ...] = (
    SourceAuthError,
    AuthExpiredError,
    SourceUnavailableError,
    SelectorNotFoundError,
)


def is_fatal_for_source(exc: BaseException) -> bool:
    """Whether ``exc`` should abort its whole source instead of one item.

    A systemic failure that an adapter's per-item ``except`` swallows turns into
    a successful-looking run with no records, which is the worst outcome the
    crawler can produce.
    """
    return isinstance(exc, _FATAL_FOR_SOURCE)


async def count_matches(page: BrowserPage, selector: str) -> int:
    """How many elements ``selector`` currently matches. Never raises."""
    try:
        return len(await page.query_selector_all(selector))
    except CrawlerError:
        raise
    except Exception:
        return 0


async def wait_for(
    page: BrowserPage,
    selector: str,
    *,
    timeout_ms: int = 15_000,
    state: str = "visible",
) -> BrowserElement | None:
    """Wait for ``selector``, returning ``None`` on timeout.

    The non-raising form, for optional fields: a lead missing a phone number is
    still a lead.
    """
    return await page.wait_for_selector(selector, state=state, timeout=timeout_ms)


async def wait_for_required(
    page: BrowserPage,
    selector: str,
    *,
    provider: str,
    label: str,
    timeout_ms: int = 15_000,
    state: str = "visible",
    recorder: DebugRecorder | None = None,
) -> BrowserElement:
    """Wait for ``selector``, recording the failure and raising on timeout.

    The raising form, for the selectors without which no records can be produced.
    The diagnostic record distinguishes the two ways a selector can "not be
    there": zero matches means it never rendered, a non-zero count means it
    rendered but not in the state that was waited for.
    """
    found = await page.wait_for_selector(selector, state=state, timeout=timeout_ms)
    if found is not None:
        return found

    matches = await count_matches(page, selector)
    if recorder is not None:
        recorder.record_error(
            provider=provider,
            label=label,
            kind="selector_timeout",
            message=f"required selector {selector!r} did not reach state {state!r}",
            url=page.url,
            selector=selector,
            waited_ms=timeout_ms,
            found_count=matches,
        )
    raise SelectorNotFoundError(
        provider,
        f"{label}: required selector {selector!r} did not reach state {state!r} "
        f"within {timeout_ms}ms ({matches} element(s) matched). "
        f"The page structure has probably changed.",
    )


async def text_of(element: BrowserElement | None) -> str:
    """Trimmed visible text, or ``""`` when there is no element."""
    if element is None:
        return ""
    try:
        return (await element.inner_text()).strip()
    except CrawlerError:
        raise
    except Exception:
        return ""


async def attribute_of(element: BrowserElement | None, name: str) -> str | None:
    """Trimmed attribute value, or ``None`` when absent.

    Returns ``None`` rather than ``""`` so a caller can tell "the page did not
    provide this" from "the page provided an empty string" — the difference
    between a field to leave null and a field that is genuinely blank.
    """
    if element is None:
        return None
    try:
        value = await element.get_attribute(name)
    except CrawlerError:
        raise
    except Exception:
        return None
    if value is None:
        return None
    return value.strip() or None
