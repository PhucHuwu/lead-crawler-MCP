"""URL, domain and website normalization helpers."""

from __future__ import annotations

import re

from src.utils.text import clean_text

_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.-]*://", re.IGNORECASE)
#: A scheme written *without* the ``//``, as ``mailto:`` and ``tel:`` are. The
#: lookahead is what keeps ``acme.com:8080`` — a port, not a scheme — out of it.
_SCHEME_LIKE_RE = re.compile(r"^([a-z][a-z0-9+.-]*):(?![0-9])", re.IGNORECASE)
_DOMAIN_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")

_LINKEDIN_RE = re.compile(
    r"(?:https?://)?(?:[a-z]{2,3}\.)?(?:www\.)?linkedin\.com/"
    r"(?P<kind>in|company|school|pub)/(?P<slug>[^/?#\s]+)",
    re.IGNORECASE,
)
_LINKEDIN_KIND_NORMALIZATION = {"pub": "in"}

#: Query parameters that exist only to attribute a click, never to select a
#: page. Kept deliberately narrow: see :func:`strip_tracking_params` for why
#: plausible-looking candidates such as ``ref`` and ``source`` are *not* here.
TRACKING_PARAMS = frozenset(
    {
        "_ga",
        "_gl",
        "_openstat",
        "dclid",
        "epik",
        "fbclid",
        "gbraid",
        "gclid",
        "igshid",
        "li_fat_id",
        "mkt_tok",
        "msclkid",
        "rdt_cid",
        "s_kwcid",
        "ttclid",
        "twclid",
        "vero_conv",
        "vero_id",
        "wbraid",
        "wickedid",
        "yclid",
    }
)

#: Whole families of tracking parameters, matched as prefixes so the vendor's
#: own suffixes come along: ``utm_source``/``utm_campaign``, Mailchimp's
#: ``mc_cid``, HubSpot's ``_hsenc``, Matomo's ``pk_campaign``.
TRACKING_PARAM_PREFIXES: tuple[str, ...] = ("_hs", "hsa_", "mc_", "oly_", "pk_", "utm_")

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


def normalize_page_url(value: object) -> str | None:
    """Canonicalize a full page URL, **keeping the path**.

    :func:`normalize_website` deliberately collapses a URL to its host, which is
    right for a company's home page and wrong for a contact page — there the
    path is the entire point. This keeps scheme, host and path, and drops the
    query and fragment so tracking parameters cannot make two exports of the
    same page differ. Anything that is not http(s) (``mailto:``, ``javascript:``,
    ``tel:``) returns ``None``.
    """
    text = clean_text(value)
    if text is None:
        return None

    if _SCHEME_RE.match(text):
        scheme = text.split(":", 1)[0].casefold()
        if scheme not in {"http", "https"}:
            return None

    # ``_SCHEME_RE`` only catches the ``scheme://`` form, so a scheme written
    # without the slashes has to be checked separately. Without this,
    # ``mailto:a@acme.com`` reads as a link to acme.com — and a ``mailto:`` in a
    # page's markup would be recorded as the company's contact page.
    if (scheme_like := _SCHEME_LIKE_RE.match(text)) is not None and (
        scheme_like.group(1).casefold() not in {"http", "https"}
    ):
        return None

    text = _SCHEME_RE.sub("", text)
    # A fragment never reaches the server, so it can never distinguish two pages.
    text = text.split("#", 1)[0]
    if not text:
        return None

    host, _, rest = text.partition("/")
    path, _, query = rest.partition("?")
    domain = normalize_domain(host)
    if domain is None:
        return None

    segments = path.strip("/")
    base = f"https://{domain}/{segments}" if segments else f"https://{domain}"
    kept = strip_tracking_params(query)
    return f"{base}?{kept}" if kept else base


def strip_tracking_params(query: str) -> str:
    """Drop campaign-tracking parameters from a query string.

    Returns the surviving query without its leading ``?``, or ``""`` when
    nothing meaningful is left.

    The bar for removal is "obviously tracking, never meaningful": vendor
    click-ids (``fbclid``, ``gclid``, ``msclkid``), the ``utm_*`` family, and
    the prefixed families used by Mailchimp, HubSpot, Matomo and friends. Two
    parameters that merely *look* like tracking are deliberately kept — ``ref``
    and ``source`` are just as often real page selectors (``?source=careers``),
    and guessing wrong would silently point a lead at the wrong page.

    Surviving parameters keep their original order, so this is a filter rather
    than a rewrite of the URL.
    """
    if not query:
        return ""

    kept: list[str] = []
    seen: set[str] = set()
    for part in query.split("&"):
        if not part:
            continue
        key = part.partition("=")[0]
        folded = key.casefold()
        if folded in TRACKING_PARAMS or folded.startswith(TRACKING_PARAM_PREFIXES):
            continue
        # Two different values for one parameter is a malformed URL, not a
        # richer one; keeping the first makes the result deterministic.
        if folded in seen:
            continue
        seen.add(folded)
        kept.append(part)
    return "&".join(kept)


#: Social and profile platforms worth recording, mapped from the hostname that
#: identifies them to the slug used in the exported record. Ordered longest
#: host first so a more specific entry wins over a shorter suffix of it.
SOCIAL_PLATFORMS: tuple[tuple[str, str], ...] = (
    ("linkedin.com", "linkedin"),
    ("twitter.com", "x"),
    ("x.com", "x"),
    ("facebook.com", "facebook"),
    ("instagram.com", "instagram"),
    ("github.com", "github"),
    ("gitlab.com", "gitlab"),
    ("youtube.com", "youtube"),
    ("youtu.be", "youtube"),
    ("crunchbase.com", "crunchbase"),
    ("angel.co", "angellist"),
    ("medium.com", "medium"),
    ("glassdoor.com", "glassdoor"),
    ("t.me", "telegram"),
)


def social_platform_for_url(value: object) -> tuple[str, str] | None:
    """Classify a URL as a social/profile link.

    Returns:
        ``(platform_slug, canonical_url)``, or ``None`` when the URL is not one
        of the recognised platforms. Shared pages (a post, a search result) are
        rejected — only a profile a company actually owns is worth recording.
    """
    url = normalize_page_url(value)
    if url is None:
        return None

    host = normalize_domain(url)
    if host is None:
        return None
    # Match on label boundaries so ``notlinkedin.com`` is not read as LinkedIn.
    labels = host.split(".")
    for suffix, slug in SOCIAL_PLATFORMS:
        parts = suffix.split(".")
        if len(labels) >= len(parts) and labels[-len(parts) :] == parts:
            path = url.split("/", 3)[3] if url.count("/") >= 3 else ""
            if not path:
                return None
            if slug == "linkedin":
                # One canonical LinkedIn form across the whole application: the
                # dedicated company field and the social-links map must agree,
                # or deduplication would treat one company as two.
                return slug, normalize_linkedin_url(url) or url
            return slug, url
    return None


def normalize_social_links(values: object) -> dict[str, str]:
    """Build a ``platform -> url`` mapping from anything resembling one.

    Accepts a mapping (as a crawler adapter supplies) or a list of URLs. Entries
    that are not recognisable profile links are dropped rather than guessed at,
    and the first URL wins for a platform so the result is deterministic.
    """
    candidates: list[object] = []
    if isinstance(values, dict):
        candidates.extend(values.values())
    elif isinstance(values, (list, tuple, set)):
        candidates.extend(values)

    links: dict[str, str] = {}
    for candidate in candidates:
        classified = social_platform_for_url(candidate)
        if classified is None:
            continue
        platform, url = classified
        links.setdefault(platform, url)
    return links


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
