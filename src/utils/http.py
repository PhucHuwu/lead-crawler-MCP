"""Async HTTP client with a typed retry policy.

Crawler adapters never touch ``httpx`` directly: they go through
:class:`AsyncHttpClient`, which owns timeouts, retries, backoff and the mapping
from HTTP status codes onto this application's exception hierarchy. That keeps
retry behaviour identical across every source and makes it testable in one place.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any, Self

import httpx
from tenacity import AsyncRetrying, RetryCallState, retry_if_exception_type, stop_after_attempt
from tenacity.wait import wait_base

from src.utils.errors import (
    CrawlerError,
    SourceAuthError,
    SourceRateLimitError,
    SourceUnavailableError,
)
from src.utils.logging import get_logger

logger = get_logger(__name__)

#: Statuses worth retrying: transient server-side or throttling responses.
RETRYABLE_STATUS_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})

#: Statuses that mean "your credentials are wrong" — retrying cannot help.
AUTH_STATUS_CODES = frozenset({401, 403})

#: Ceiling for a server-supplied ``Retry-After``; a misconfigured source should
#: not be able to park the whole run for an hour.
MAX_HONORED_RETRY_AFTER = 60.0


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Retry budget and backoff shape for a source."""

    max_attempts: int = 3
    initial_backoff: float = 0.5
    max_backoff: float = 20.0
    multiplier: float = 2.0
    #: Fraction of the computed backoff added as random jitter (0-1). Prevents
    #: synchronised retry storms when several crawlers hit a source at once.
    jitter: float = 0.25

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")


class _SourceAwareWait(wait_base):
    """Exponential backoff that defers to ``Retry-After`` when the server sent one."""

    def __init__(self, policy: RetryPolicy) -> None:
        self._policy = policy

    def __call__(self, retry_state: RetryCallState) -> float:
        if retry_state.outcome is not None:
            exc = retry_state.outcome.exception()
            if isinstance(exc, SourceRateLimitError) and exc.retry_after:
                return min(exc.retry_after, MAX_HONORED_RETRY_AFTER)

        exponent = max(retry_state.attempt_number - 1, 0)
        backoff = min(
            self._policy.initial_backoff * (self._policy.multiplier**exponent),
            self._policy.max_backoff,
        )
        return backoff + random.uniform(0.0, backoff * self._policy.jitter)


def _parse_retry_after(response: httpx.Response) -> float | None:
    """Read a ``Retry-After`` header expressed in seconds.

    The HTTP-date form is ignored on purpose: clock skew between us and the
    source makes it less reliable than our own backoff.
    """
    raw = response.headers.get("retry-after")
    if not raw:
        return None
    try:
        return max(float(raw.strip()), 0.0)
    except ValueError:
        return None


@dataclass
class AsyncHttpClient:
    """Thin async wrapper owning one source's HTTP concerns.

    Use as an async context manager so the underlying connection pool is always
    released, including on the error paths::

        async with AsyncHttpClient(provider="apollo", base_url=...) as http:
            payload = await http.post_json("/mixed_people/search", json=body)
    """

    provider: str
    base_url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    timeout: float = 30.0
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    _client: httpx.AsyncClient | None = field(default=None, init=False, repr=False)

    async def __aenter__(self) -> Self:
        self._ensure_client()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    @property
    def client(self) -> httpx.AsyncClient:
        return self._ensure_client()

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                headers=self.headers,
                timeout=httpx.Timeout(self.timeout),
                follow_redirects=True,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    # ------------------------------------------------------------------ #
    # Request plumbing
    # ------------------------------------------------------------------ #
    async def _send(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """Perform one attempt, translating failures into typed exceptions."""
        try:
            response = await self.client.request(method, url, **kwargs)
        except httpx.TimeoutException as exc:
            raise SourceUnavailableError(
                self.provider, f"{method} {url} timed out after {self.timeout:g}s"
            ) from exc
        except httpx.TransportError as exc:
            raise SourceUnavailableError(self.provider, f"{method} {url} failed: {exc}") from exc

        status = response.status_code

        if status in AUTH_STATUS_CODES:
            raise SourceAuthError(
                self.provider,
                f"{method} {url} rejected our credentials (HTTP {status}). "
                f"Check the API key for this source.",
            )
        if status == 429:
            raise SourceRateLimitError(
                self.provider,
                f"{method} {url} was rate limited (HTTP 429)",
                retry_after=_parse_retry_after(response),
            )
        if status in RETRYABLE_STATUS_CODES:
            raise SourceUnavailableError(self.provider, f"{method} {url} returned HTTP {status}")
        if status >= 400:
            # A non-retryable 4xx is a bug in our request, not a transient fault.
            raise CrawlerError(
                self.provider,
                f"{method} {url} returned HTTP {status}: {_safe_snippet(response)}",
            )
        return response

    def _log_retry(self, retry_state: RetryCallState) -> None:
        exc = retry_state.outcome.exception() if retry_state.outcome else None
        logger.warning(
            "retrying source request",
            extra={
                "provider": self.provider,
                "attempt": retry_state.attempt_number,
                "max_attempts": self.retry.max_attempts,
                "error": str(exc),
            },
        )

    async def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """Send a request, retrying transient failures up to the retry budget.

        Raises the last typed error if every attempt fails.
        """
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(self.retry.max_attempts),
            wait=_SourceAwareWait(self.retry),
            retry=retry_if_exception_type((SourceUnavailableError, SourceRateLimitError)),
            before_sleep=self._log_retry,
            reraise=True,
        ):
            with attempt:
                return await self._send(method, url, **kwargs)

        # AsyncRetrying with reraise=True always either returns or raises above.
        raise AssertionError("unreachable: retry loop exited without a result")

    async def request_json(self, method: str, url: str, **kwargs: Any) -> Any:
        """Send a request and decode the JSON body.

        Raises:
            CrawlerError: if the response is not valid JSON.
        """
        response = await self.request(method, url, **kwargs)
        try:
            return response.json()
        except ValueError as exc:
            raise CrawlerError(
                self.provider,
                f"{method} {url} returned a non-JSON body: {_safe_snippet(response)}",
            ) from exc

    async def post_json(self, url: str, **kwargs: Any) -> Any:
        return await self.request_json("POST", url, **kwargs)


def _safe_snippet(response: httpx.Response, limit: int = 200) -> str:
    """Short, credential-safe excerpt of a response body for error messages.

    URLs are already stripped of query strings by callers where they carry keys;
    here we only guard against enormous HTML error pages flooding the logs.
    """
    try:
        text = response.text
    except Exception:  # pragma: no cover - defensive: body already consumed
        return "<unreadable body>"
    text = " ".join(text.split())
    return text[:limit] + ("…" if len(text) > limit else "")
