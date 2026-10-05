"""Secret scrubbing for artifacts written to disk.

:mod:`src.utils.redaction` protects **log records**. It is installed as a
``logging.Filter`` and never sees anything an exporter or a diagnostic writer
puts on disk, so it offers this module's problem exactly zero protection. A
browser diagnostic is the highest-risk artifact in the project — it is a
verbatim copy of a page rendered inside a signed-in session — so it needs its
own guarantees rather than an inherited assumption.

The guarantees are layered, strongest first:

1. **Structural non-collection.** :class:`~src.browser.debug.DebugRecorder` only
   ever receives ``page.content()``, ``page.screenshot()``, ``page.url`` and
   explicit error metadata. There is no code path that reads cookies,
   ``storage_state()``, or request/response headers, so the highest-value
   secrets are never in memory to be written. Not collecting beats scrubbing.
2. **URL redaction.** :func:`redact_url` drops the query string, which is where
   tokens ride.
3. **HTML scrubbing.** :func:`scrub_html` rewrites CSRF/token-shaped values and
   then runs the process-wide :func:`~src.utils.redaction.redact`, so a secret
   configured as a ``SecretStr`` is removed even if it reached the page.
4. **Fail-closed assertion.** :func:`assert_no_secrets` inspects the scrubbed
   payload *before* it is written. Layers 1-3 are meant to make it a no-op; it
   exists so a future edit that weakens one of them fails loudly instead of
   quietly writing a credential to a file that gets attached to a bug report.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from src.utils.errors import SecretLeakError
from src.utils.redaction import REDACTED, redact

#: Query, form and JSON keys whose *value* is a credential. Matched
#: case-insensitively as a whole key, and also as a substring for the compound
#: spellings real applications use (``csrf_token``, ``X-Api-Key``).
SENSITIVE_KEY_PATTERNS: tuple[str, ...] = (
    "access_token",
    "api_key",
    "apikey",
    "auth",
    "authenticity_token",
    "authorization",
    "bearer",
    "client_secret",
    "code",
    "cookie",
    "csrf",
    "id_token",
    "jwt",
    "key",
    "license",
    "nonce",
    "passwd",
    "password",
    "private_key",
    "refresh_token",
    "saml",
    "secret",
    "session",
    "sessionid",
    "sig",
    "signature",
    "state",
    "ticket",
    "token",
    "xsrf",
)

#: Header names that carry a live credential.
SENSITIVE_HEADERS: tuple[str, ...] = (
    "authorization",
    "cookie",
    "proxy-authorization",
    "set-cookie",
    "x-api-key",
    "x-auth-token",
    "x-csrf-token",
)

_KEY_RE = "|".join(re.escape(pattern) for pattern in SENSITIVE_KEY_PATTERNS)

# ``<input type="hidden" name="csrf_token" value="...">`` — the value is a live
# CSRF token that would let anyone replay a form submission.
_HIDDEN_INPUT_RE = re.compile(
    r"""<input\b[^>]*\bvalue\s*=\s*(?P<quote>["'])(?P<value>.*?)(?P=quote)[^>]*>""",
    re.IGNORECASE | re.DOTALL,
)
_INPUT_KEY_RE = re.compile(
    rf"""\b(?:name|id)\s*=\s*["'][^"']*(?:{_KEY_RE})[^"']*["']""",
    re.IGNORECASE,
)

# ``<meta name="csrf-token" content="...">``
_CSRF_META_RE = re.compile(
    rf"""<meta\b[^>]*\bname\s*=\s*["'][^"']*(?:{_KEY_RE})[^"']*["'][^>]*>""",
    re.IGNORECASE,
)

# ``"access_token": "eyJ..."`` and ``csrfToken: "abc"`` — JSON and JS object
# literals. Only the *value* is rewritten; the surrounding payload stays, or the
# artifact stops being useful for diagnosing a selector change.
_INLINE_PAIR_RE = re.compile(
    rf"""(?P<prefix>["']?(?:{_KEY_RE})[A-Za-z0-9_\-]*["']?\s*:\s*)(?P<quote>["'])(?P<value>[^"']*)(?P=quote)""",
    re.IGNORECASE,
)

# Cookie-shaped text that must never survive a scrub, whatever wrote it.
_COOKIE_SHAPED_RE = re.compile(
    r"(?:document\.cookie|set-cookie\s*:|cookie\s*:|storage_state)",
    re.IGNORECASE,
)


def _is_sensitive_key(key: str) -> bool:
    """Whether ``key`` names a credential.

    Substring rather than equality on purpose: ``csrf_token``, ``X-Api-Key`` and
    ``user_session_id`` all need to match, and an exact-match list would have to
    enumerate them. The cost is over-redaction of keys like ``state`` — which is
    an OAuth parameter anyway, so redacting it is correct.
    """
    lowered = key.casefold()
    return any(pattern in lowered for pattern in SENSITIVE_KEY_PATTERNS)


def redact_url(url: str, *, keep_query: bool = False) -> str:
    """Return ``url`` safe to write to a diagnostic artifact.

    The query string is dropped by default. A debug URL's job is to record
    *which page* the crawler was on, and the path plus fragment says that —
    Apollo is hash-routed, so the useful part survives. Queries are where
    tokens ride, and an unknown parameter name cannot be recognised as secret,
    so the safe default is to keep none of it.

    Pass ``keep_query=True`` when the parameters are known to matter; the
    :data:`SENSITIVE_KEY_PATTERNS` denylist then filters individual pairs.

    The fragment is preserved: it is routing information, not a credential.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        # A URL that cannot be parsed cannot be trusted either; withhold it.
        return REDACTED
    if keep_query and parts.query:
        pairs = [
            (key, REDACTED if _is_sensitive_key(key) else value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
        ]
        query = urlencode(pairs)
    else:
        query = ""
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))


def scrub_html(html: str, *, max_bytes: int | None = None) -> str:
    """Strip credential-shaped values from captured page HTML.

    Handles the four routes a token actually takes into a page: a hidden form
    input, a ``csrf-token`` meta tag, an inline JSON/JS string, and a cookie
    document property. Afterwards :func:`~src.utils.redaction.redact` runs over
    the whole text, which removes any value this process holds as a ``SecretStr``
    — so a credential configured by the operator is covered even if the page
    echoed it back in a shape no pattern predicts.

    ``max_bytes`` truncates the result, with a marker, before returning.
    """
    scrubbed = _INLINE_PAIR_RE.sub(
        lambda match: (
            f"{match.group('prefix')}{match.group('quote')}{REDACTED}{match.group('quote')}"
        ),
        html,
    )
    scrubbed = _HIDDEN_INPUT_RE.sub(_scrub_hidden_input, scrubbed)
    scrubbed = _CSRF_META_RE.sub("<!-- redacted: credential-bearing meta tag -->", scrubbed)
    scrubbed = _COOKIE_SHAPED_RE.sub(f"/* redacted: {REDACTED} */", scrubbed)
    scrubbed = str(redact(scrubbed))
    if max_bytes is not None:
        scrubbed = _truncate_bytes(scrubbed, max_bytes)
    return scrubbed


def _scrub_hidden_input(match: re.Match[str]) -> str:
    """Blank the ``value`` of an input whose name/id looks credential-bearing."""
    tag = match.group(0)
    if not _INPUT_KEY_RE.search(tag):
        return tag
    return tag.replace(match.group("value"), REDACTED)


def _truncate_bytes(text: str, max_bytes: int) -> str:
    """Truncate ``text`` to ``max_bytes`` of UTF-8 without splitting a character."""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    marker = "\n<!-- truncated -->\n"
    budget = max(max_bytes - len(marker.encode("utf-8")), 0)
    # ``errors="ignore"`` drops a trailing partial multi-byte character rather
    # than raising, which is the only sane outcome for a diagnostic artifact.
    return encoded[:budget].decode("utf-8", errors="ignore") + marker


def find_secret_leaks(text: str) -> tuple[str, ...]:
    """Describe every credential-looking value still present in ``text``.

    Returns descriptions, never the values themselves — a leak report that
    quotes the leak is a second copy of the leak.
    """
    findings: list[str] = []
    if redact(text) != text:
        findings.append("a registered secret value")
    for match in _COOKIE_SHAPED_RE.finditer(text):
        findings.append(f"cookie-shaped text ({match.group(0).strip()!r})")
    return tuple(findings)


def assert_no_secrets(text: str, *, what: str = "artifact") -> None:
    """Raise :class:`SecretLeakError` if ``text`` still carries a credential.

    Called *before* the payload is written. Order matters: writing first and
    checking afterwards would leave the raw bytes on disk regardless of the
    verdict.
    """
    findings = find_secret_leaks(text)
    if findings:
        joined = "; ".join(findings)
        raise SecretLeakError(
            f"refusing to write {what}: scrubbing left {joined}. "
            f"The file was withheld rather than written with a credential in it."
        )
