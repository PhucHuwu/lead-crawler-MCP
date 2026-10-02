"""URL, domain and website normalization helpers."""

from __future__ import annotations

import re

from src.utils.text import clean_text

_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.-]*://", re.IGNORECASE)
_DOMAIN_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")

_LINKEDIN_RE = re.compile(
    r"(?:https?://)?(?:[a-z]{2,3}\.)?(?:www\.)?linkedin\.com/"
    r"(?P<kind>in|company|school|pub)/(?P<slug>[^/?#\s]+)",
    re.IGNORECASE,
)
_LINKEDIN_KIND_NORMALIZATION = {"pub": "in"}

# Consumer mailbox providers. Leads on these domains usually represent
# individuals rather than a company we can qualify, so they are useful as a
# filter signal (see processors.filters).
FREE_EMAIL_DOMAINS = frozenset(
    {
        "aol.com",
        "gmx.com",
        "gmx.de",
        "gmail.com",
        "googlemail.com",
        "hotmail.co.uk",
        "hotmail.com",
        "icloud.com",
        "inbox.com",
        "live.com",
        "mail.com",
        "mail.ru",
        "me.com",
        "msn.com",
        "outlook.com",
        "pm.me",
        "proton.me",
        "protonmail.com",
        "qq.com",
        "web.de",
        "y7mail.com",
        "yahoo.co.uk",
        "yahoo.com",
        "yandex.com",
        "yeah.net",
        "zoho.com",
        "126.com",
        "163.com",
    }
)


def normalize_domain(value: object) -> str | None:
    """Extract a bare registrable-ish host from a URL, email or domain string.

    Returns lowercase host without scheme, ``www.``, port, path or trailing dot.
    Subdomains are preserved (``careers.acme.com`` stays intact) because they are
    occasionally the only distinguishing signal; callers that need the apex can
    use :func:`apex_domain`.
    """
    text = clean_text(value)
    if text is None:
        return None

    # Allow passing an email address directly.
    if "@" in text and "/" not in text:
        text = text.rsplit("@", 1)[-1]

    text = _SCHEME_RE.sub("", text)
    # Cut off path, query, fragment and any userinfo.
    text = re.split(r"[/?#]", text, maxsplit=1)[0]
    text = text.rsplit("@", 1)[-1]
    text = text.split(":", 1)[0]  # strip port
    text = text.strip().strip(".").casefold()

    if text.startswith("www."):
        text = text[4:]
    if not text or not _DOMAIN_RE.match(text):
        return None
    return text


def apex_domain(value: object) -> str | None:
    """Reduce a host to its last two labels (``careers.acme.co.uk`` -> ``co.uk`` caveat).

    Multi-part public suffixes (``co.uk``, ``com.au``) are not resolved here;
    this is a display/grouping helper, not a security boundary.
    """
    domain = normalize_domain(value)
    if domain is None:
        return None
    parts = domain.split(".")
    return ".".join(parts[-2:]) if len(parts) > 2 else domain


def email_domain(email: str | None) -> str | None:
    """Return the domain portion of a normalized email address."""
    if not email or "@" not in email:
        return None
    return normalize_domain(email.rsplit("@", 1)[-1])


def is_free_email_domain(email: str | None) -> bool:
    """True when the address belongs to a consumer mailbox provider."""
    domain = email_domain(email)
    return domain is not None and domain in FREE_EMAIL_DOMAINS


def normalize_website(value: object) -> str | None:
    """Return a canonical ``https://host`` URL without a trailing slash."""
    domain = normalize_domain(value)
    return f"https://{domain}" if domain else None


def normalize_linkedin_url(value: object, *, kind: str = "any") -> str | None:
    """Canonicalize a LinkedIn profile or company URL.

    ``kind`` may be ``"person"`` (``/in/``), ``"company"`` (``/company/``) or
    ``"any"``. Returns ``None`` when the value is not a LinkedIn URL of the
    requested kind, which is what makes this safe to run over arbitrary strings.
    """
    text = clean_text(value)
    if text is None:
        return None

    match = _LINKEDIN_RE.search(text)
    if match is None:
        return None

    raw_kind = match.group("kind").casefold()
    normalized_kind = _LINKEDIN_KIND_NORMALIZATION.get(raw_kind, raw_kind)
    slug = match.group("slug").strip().casefold().rstrip("/")
    if not slug:
        return None

    if kind == "person" and normalized_kind != "in":
        return None
    if kind == "company" and normalized_kind != "company":
        return None

    return f"https://www.linkedin.com/{normalized_kind}/{slug}"
