"""Session-expiry detection.

A crawler driving a signed-in web application has one failure mode that is worse
than any other: the saved session has expired, the site quietly serves the login
page, and the adapter parses *that* into leads. The run reports success, the CSV
has rows, and every row is wrong. This module exists so that cannot happen — the
guard runs between navigation and the adapter, so the adapter never sees a login
page at all.

Detection is **declared, not guessed**. Each adapter states what its login wall
looks like via :class:`LoginWall`; the framework does not carry a list of
"things that look like a login form", because a heuristic that is wrong in the
permissive direction produces the failure above and a heuristic that is wrong in
the strict direction aborts healthy runs.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlsplit

from src.browser.base import BrowserPage
from src.browser.redact import redact_url
from src.utils.errors import AuthExpiredError

#: Query parameters a site uses to send you back where you were after signing
#: in. Present with a login-shaped value, they mean the request was bounced.
_REDIRECT_PARAMS = frozenset(
    {
        "continue",
        "next",
        "redirect",
        "redirect_to",
        "redirect_uri",
        "return",
        "return_to",
        "returnto",
    }
)

#: Substrings that make a path look like a sign-in route.
_LOGIN_HINTS = ("login", "log-in", "signin", "sign-in", "sign_in", "auth")

#: How long to wait for a declared signed-in marker before concluding it is
#: absent. Bounded because this is the weakest signal and it runs on every
#: navigation: long enough to survive a client-side hydrate, short enough that a
#: genuinely logged-out session fails fast.
DEFAULT_PROBE_TIMEOUT_MS = 2_000


@dataclass(frozen=True, slots=True)
class LoginWall:
    """What an adapter's signed-out state looks like.

    All three signals are optional, and an adapter that declares none is treated
    as having no login wall — correct for ``website``, which reads public pages.
    """

    #: ``fnmatch`` globs matched against the final URL. ``*`` spans ``/`` in
    #: ``fnmatch``, so ``*login*`` covers every route shape.
    url_patterns: tuple[str, ...] = ()

    #: Selectors that exist **only** when signed out — a password field, a
    #: sign-in form. Checked without waiting: if it is on the page now, it is on
    #: the page.
    logged_out_selectors: tuple[str, ...] = ()

    #: Selectors that must exist when signed in. Checked last and with a bounded
    #: wait, because "not there yet" and "not signed in" look identical during a
    #: client-side hydrate.
    logged_in_selectors: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AuthVerdict:
    """The outcome of one check, with the signal that produced it."""

    authenticated: bool
    #: ``url`` | ``selectors`` | ``marker_missing`` | ``asserted``
    signal: str
    #: Operator-facing explanation. Never contains a credential: URLs pass
    #: through :func:`~src.browser.redact.redact_url` and selectors are static.
    detail: str


class AuthGuard:
    """Checks one adapter's navigation results against its declared login wall."""

    def __init__(
        self,
        provider: str,
        wall: LoginWall,
        *,
        probe_timeout_ms: int = DEFAULT_PROBE_TIMEOUT_MS,
    ) -> None:
        self.provider = provider
        self.wall = wall
        self.probe_timeout_ms = probe_timeout_ms

    async def check(self, page: BrowserPage) -> AuthVerdict:
        """Classify the page the adapter is about to read.

        Signals run strongest-first and stop at the first conclusive one, so the
        common case costs one string comparison.
        """
        url = page.url

        if (pattern := self._matching_url_pattern(url)) is not None:
            return AuthVerdict(
                False, "url", f"landed on {redact_url(url)}, which matches {pattern!r}"
            )

        if (bounced := self._login_redirect(url)) is not None:
            return AuthVerdict(False, "url", f"was bounced to a sign-in route ({bounced})")

        for selector in self.wall.logged_out_selectors:
            for element in await page.query_selector_all(selector):
                if await element.is_visible():
                    return AuthVerdict(
                        False,
                        "selectors",
                        f"{selector!r} is on the page at {redact_url(url)}",
                    )

        if self.wall.logged_in_selectors:
            for selector in self.wall.logged_in_selectors:
                found = await page.wait_for_selector(
                    selector, state="attached", timeout=self.probe_timeout_ms
                )
                if found is not None:
                    return AuthVerdict(True, "selectors", f"{selector!r} is present")
            return AuthVerdict(
                False,
                "marker_missing",
                f"none of {list(self.wall.logged_in_selectors)} appeared at {redact_url(url)}",
            )

        return AuthVerdict(True, "asserted", "no signed-out signal was found")

    def _matching_url_pattern(self, url: str) -> str | None:
        path = urlsplit(url).path
        for pattern in self.wall.url_patterns:
            if fnmatch.fnmatch(url, pattern) or fnmatch.fnmatch(path, pattern):
                return pattern
        return None

    def _login_redirect(self, url: str) -> str | None:
        """Detect a bounce-to-login carried in the query string.

        A site that redirects an expired session usually appends where you were
        headed, so the final URL is *not* itself a login route. Without this
        signal that case would pass every other check.
        """
        for key, value in parse_qsl(urlsplit(url).query, keep_blank_values=True):
            if key.casefold() in _REDIRECT_PARAMS and any(
                hint in value.casefold() for hint in _LOGIN_HINTS
            ):
                return f"{key}={redact_url(value)}"
        return None


def auth_error(
    guard: AuthGuard, verdict: AuthVerdict, *, profile_dir: object = None
) -> AuthExpiredError:
    """Build the error raised when a guard rejects a page.

    The message names the signal that fired and the exact command that fixes it,
    because this is the one auth failure an operator can resolve without
    touching configuration. It also states that nothing was collected — the
    question a reader immediately has, and the thing that would be silently
    untrue if the guard were ever moved after the adapter.
    """
    where = f" (profile: {profile_dir})" if profile_dir is not None else ""
    return AuthExpiredError(
        guard.provider,
        f"the saved browser session is no longer signed in{where}: {verdict.detail}. "
        f"Nothing was collected from this page — a login page is never parsed as results. "
        f"Re-authenticate with: python -m src.main --login {guard.provider}",
    )
