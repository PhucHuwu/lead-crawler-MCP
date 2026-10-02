"""Company-website enrichment source.

Reads a company's own public pages and produces **one** lead per site: the
company name, a description, the domain, industry and location hints, a contact
page, published email addresses and social profiles. No person data is invented
— this is an account-enrichment source, and the validator deliberately accepts
company-only leads so these survive the pipeline.

Politeness is a design constraint here, not a setting to be maximised:

* ``robots.txt`` is honoured by default (``LEAD_WEBSITE__RESPECT_ROBOTS``).
* At most ``max_pages_per_site`` pages are read, homepage included.
* A configurable delay is enforced between two requests to the same host, and
  the crawler identifies itself with a UA string that says who to contact.
* Only links on the seed host are followed, so this cannot wander off into a
  general crawl of the web.

Failures are contained per site: one unreachable site logs a warning and the
crawl continues. Only when *every* seed fails does the source report an error,
which is the one case the pipeline treats as source-level.

Every piece of HTML knowledge lives in this module — the parsers are private and
nothing else in the application knows what JSON-LD is.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

from src.crawlers.base import BaseCrawler
from src.crawlers.registry import register_crawler
from src.models.lead import RawLead
from src.utils.errors import CrawlerError, LeadCrawlerError
from src.utils.http import AsyncHttpClient, RetryPolicy
from src.utils.text import clean_text, normalize_email
from src.utils.urls import (
    normalize_domain,
    normalize_page_url,
    normalize_website,
    social_platform_for_url,
)

if TYPE_CHECKING:
    from src.config import Settings

logger = logging.getLogger(__name__)

#: `<meta>` keys worth reading, in the order they win when several are present.
#: Open Graph first: it is written deliberately for machines, whereas a meta
#: description is often a marketing tagline with the year baked in.
_DESCRIPTION_META_KEYS = (
    "og:description",
    "description",
    "twitter:description",
)
_SITE_NAME_META_KEYS = ("og:site_name", "application-name")

#: schema.org types that mean "this JSON-LD block describes a company".
_ORG_TYPES = frozenset(
    {
        "organization",
        "localbusiness",
        "corporation",
        "onlinebusiness",
        "brand",
        "ngo",
        "nonprofit",
        "educationalorganization",
        "governmentorganization",
    }
)

#: Path fragments that mark a page as worth reading for contact details.
_CONTACT_HINTS = ("contact", "about", "impressum", "kontakt", "company")

#: A title like ``Acme Corp - We build widgets`` is mostly tagline; this splits
#: on the usual separators so the name can be taken from the first segment.
_TITLE_SPLIT_RE = re.compile(r"\s+[|\-–—·»:]\s+")

#: Trailing noise seen on real homepages ("Home", "Official Site").
_TITLE_NOISE = frozenset({"home", "homepage", "official site", "welcome", "index"})

USER_AGENT_FALLBACK = "TinasoftLeadCrawler/0.1"


# --------------------------------------------------------------------------- #
# HTML parsing
# --------------------------------------------------------------------------- #
@dataclass
class _PageFacts:
    """What one page told us. All fields are optional; real pages are sparse."""

    title: str | None = None
    site_name: str | None = None
    description: str | None = None
    emails: list[str] = field(default_factory=list)
    #: Absolute-profile links only; classified later by :mod:`src.utils.urls`.
    social_links: dict[str, str] = field(default_factory=dict)
    #: Best guess at "the page with the contact details" on this site.
    contact_url: str | None = None
    organization: dict[str, Any] = field(default_factory=dict)

    def merge(self, other: _PageFacts) -> None:
        """Fold another page's findings in, keeping the first non-empty value.

        First-wins is deliberate: pages are read homepage-first, and the
        homepage's own description beats the one on ``/contact``.
        """
        for attribute in ("title", "site_name", "description", "contact_url"):
            if getattr(self, attribute) is None:
                setattr(self, attribute, getattr(other, attribute))
        for email in other.emails:
            if email not in self.emails:
                self.emails.append(email)
        for platform, url in other.social_links.items():
            self.social_links.setdefault(platform, url)
        # Organization blocks are merged key-wise so an address found on the
        # contact page still contributes when the homepage had a JSON-LD block
        # that only carried a name.
        for key, value in other.organization.items():
            self.organization.setdefault(key, value)


class _PageParser(HTMLParser):
    """Pulls the handful of facts we want out of one HTML document.

    Written against the stdlib parser rather than a scraping library: the
    extraction here is a few tags and one embedded JSON document, which does not
    justify a heavyweight dependency or its C extension.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.facts = _PageFacts()
        self.hrefs: list[str] = []
        self._in_title = False
        self._title_parts: list[str] = []
        self._script_parts: list[str] | None = None
        self._script_is_json_ld = False

    # -- tags ----------------------------------------------------------- #
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key.lower(): (value or "") for key, value in attrs}

        if tag == "title":
            self._in_title = True
            self._title_parts = []
        elif tag == "meta":
            self._read_meta(attributes)
        elif tag == "script":
            script_type = attributes.get("type", "").strip().casefold()
            self._script_is_json_ld = script_type == "application/ld+json"
            self._script_parts = []
        elif tag == "a":
            href = attributes.get("href", "").strip()
            if href:
                self.hrefs.append(href)
        elif tag == "link":
            # Some sites put the canonical/alternate contact page in a <link>.
            href = attributes.get("href", "").strip()
            if href:
                self.hrefs.append(href)

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
            if self.facts.title is None:
                self.facts.title = clean_text(" ".join(self._title_parts))
        elif tag == "script":
            if self._script_is_json_ld and self._script_parts:
                self._read_json_ld("".join(self._script_parts))
            self._script_parts = None
            self._script_is_json_ld = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title_parts.append(data)
        elif self._script_parts is not None:
            self._script_parts.append(data)

    # -- extraction ----------------------------------------------------- #
    def _read_meta(self, attributes: dict[str, str]) -> None:
        key = (attributes.get("property") or attributes.get("name") or "").strip().casefold()
        content = clean_text(attributes.get("content"))
        if not key or content is None:
            return
        # `setdefault`-style: the first <meta> for a key is the one the page
        # author meant, so later duplicates do not overwrite it.
        if key in _DESCRIPTION_META_KEYS and self.facts.description is None:
            self.facts.description = content
        elif key in _SITE_NAME_META_KEYS and self.facts.site_name is None:
            self.facts.site_name = content

    def _read_json_ld(self, text: str) -> None:
        try:
            payload = json.loads(text)
        except ValueError:
            # Malformed JSON-LD is common in the wild and never worth a warning.
            logger.debug("ignoring malformed JSON-LD block")
            return
        for node in _walk_json(payload):
            if not _is_organization(node):
                continue
            for key, value in _organization_fields(node).items():
                self.facts.organization.setdefault(key, value)


def _walk_json(payload: Any) -> list[dict[str, Any]]:
    """Every dict in a JSON-LD document, at any depth.

    Graph-shaped documents (``@graph``, nested ``mainEntity``) are the norm for
    anything generated by a CMS plugin, so the search cannot be depth-limited.
    """
    found: list[dict[str, Any]] = []
    stack: list[Any] = [payload]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            found.append(node)
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return found


def _is_organization(node: dict[str, Any]) -> bool:
    raw = node.get("@type")
    types = raw if isinstance(raw, list) else [raw]
    return any(isinstance(item, str) and item.strip().casefold() in _ORG_TYPES for item in types)


def _organization_fields(node: dict[str, Any]) -> dict[str, Any]:
    """Map a schema.org Organization onto our flat field names."""
    fields: dict[str, Any] = {}

    if (name := clean_text(node.get("name"))) is not None:
        fields["name"] = name
    if (description := clean_text(node.get("description"))) is not None:
        fields["description"] = description
    if (industry := clean_text(node.get("industry"))) is not None:
        fields["industry"] = industry
    if (employees := _employee_count(node.get("numberOfEmployees"))) is not None:
        fields["employee_count"] = employees
    if (country := _address_part(node.get("address"), "addressCountry")) is not None:
        fields["country"] = country
    if (city := _address_part(node.get("address"), "addressLocality")) is not None:
        fields["city"] = city

    same_as = node.get("sameAs")
    urls = same_as if isinstance(same_as, list) else [same_as]
    fields["same_as"] = [url for url in urls if isinstance(url, str)]
    return fields


def _employee_count(value: Any) -> int | str | None:
    """Read ``numberOfEmployees``, which is an int, a string or a QuantitativeValue."""
    if isinstance(value, dict):
        value = value.get("value") or value.get("minValue")
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, str)):
        return value
    return None


def _address_part(address: Any, key: str) -> str | None:
    if isinstance(address, dict):
        return clean_text(address.get(key))
    return None


def _clean_title(value: str | None) -> str | None:
    """Reduce a page title to something usable as a company name."""
    text = clean_text(value)
    if text is None:
        return None
    for candidate in _TITLE_SPLIT_RE.split(text):
        candidate = candidate.strip()
        if candidate and candidate.casefold() not in _TITLE_NOISE:
            return candidate
    return text


def _host_of(url: str) -> str | None:
    return normalize_domain(urlparse(url).netloc or urlparse(url).path)


def _is_informative(facts: _PageFacts) -> bool:
    """Whether a site told us anything we did not already have.

    The caller configured a domain; a site that answers with an empty page, or
    with nothing but a bare ``<title>``, leaves us holding essentially that. A
    title is included because it is usually the company's own name, which is
    real enrichment — but a page that yields *only* the domain we started from
    would pad the export without adding a single fact.
    """
    return any(
        (
            facts.organization,
            facts.description,
            facts.site_name,
            facts.title,
            facts.emails,
            facts.social_links,
            facts.contact_url,
        )
    )


# --------------------------------------------------------------------------- #
# Crawler
# --------------------------------------------------------------------------- #
@register_crawler
class WebsiteCrawler(BaseCrawler):
    """Enriches a list of company websites."""

    provider = "website"
    display_name = "Company website"
    description = "Read a company's public pages for description, contacts and socials."

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self._config = settings.website
        self._http = AsyncHttpClient(
            provider=self.provider,
            headers={"User-Agent": self._config.user_agent, "Accept": "text/html,*/*;q=0.8"},
            timeout=settings.http_timeout,
            retry=RetryPolicy(
                max_attempts=settings.http_max_attempts,
                initial_backoff=settings.http_initial_backoff,
                max_backoff=settings.http_max_backoff,
            ),
        )
        #: Monotonic timestamp of the last request per host, so the delay is
        #: respected across seeds that share a host (acme.com and www.acme.com).
        self._last_request: dict[str, float] = {}
        self._robots: dict[str, RobotFileParser | None] = {}

    def is_available(self) -> tuple[bool, str]:
        if not self._config.urls:
            return False, "set --website-url or LEAD_WEBSITE__URLS"
        return True, ""

    async def aclose(self) -> None:
        await self._http.aclose()

    async def crawl(self, limit: int) -> list[RawLead]:
        seeds = self._seeds()
        if not seeds:
            raise CrawlerError(self.provider, "no usable website URLs configured")

        requested = seeds[:limit]
        leads: list[RawLead] = []
        failures = 0
        for seed in requested:
            try:
                lead = await self._enrich(seed)
            except LeadCrawlerError as exc:
                # One bad site must not sink the run; the other seeds still have
                # something to contribute.
                failures += 1
                self.logger.warning(
                    "could not enrich site", extra={"site": seed, "error": str(exc)}
                )
                continue
            except Exception:
                failures += 1
                self.logger.exception("unexpected failure enriching site", extra={"site": seed})
                continue
            if lead is not None:
                leads.append(lead)

        # Only a total wipeout is a source-level failure. A site that answered
        # but told us nothing is unproductive, not broken.
        if failures and failures == len(requested):
            raise CrawlerError(self.provider, f"all {failures} website(s) failed")

        self.logger.info(
            "enriched company websites",
            extra={
                "count": len(leads),
                "requested": len(requested),
                "failed": failures,
                "unproductive": len(requested) - failures - len(leads),
            },
        )
        return leads

    def _seeds(self) -> list[str]:
        """Canonical seed URLs, de-duplicated by domain and order-preserving.

        ``acme.com`` and ``www.acme.com/`` are the same site, and reading it
        twice would be a pointless second round of requests against someone
        else's server, so the repeat spelling is dropped rather than fetched.
        """
        seeds: list[str] = []
        by_domain: dict[str, str] = {}
        discarded = 0
        for entry in self._config.urls:
            seed = normalize_website(entry)
            if seed is None:
                discarded += 1
                continue
            domain = normalize_domain(seed) or seed
            if domain in by_domain:
                discarded += 1
                continue
            by_domain[domain] = seed
            seeds.append(seed)

        if discarded:
            self.logger.warning(
                "ignored unusable or repeated website entries",
                extra={"count": discarded, "configured": list(self._config.urls)},
            )
        return seeds

    # ------------------------------------------------------------------ #
    # One site
    # ------------------------------------------------------------------ #
    async def _enrich(self, seed: str) -> RawLead | None:
        """Read up to ``max_pages_per_site`` pages of one site and merge them.

        Returns:
            The site's lead, or ``None`` when the site answered but yielded
            nothing worth a row — a bare domain the caller already had is not an
            enrichment, and exporting it would only add noise to the result set.
        """
        host = _host_of(seed)
        facts = _PageFacts()
        queue = [seed]
        seen: set[str] = set()
        pages_read = 0

        while queue and pages_read < self._config.max_pages_per_site:
            url = queue.pop(0)
            if url in seen:
                continue
            seen.add(url)

            if not await self._allowed(url):
                self.logger.info("skipping disallowed page", extra={"url": url})
                continue

            # Only the seed page is required: if the site's own homepage will not
            # load, the site has failed. A missing /contact is routine and must
            # not be reported as one.
            html = await self._fetch(url, required=url == seed)
            pages_read += 1
            if html is None:
                continue

            page = self._parse_page(html, url)
            facts.merge(page)

            for candidate in self._followable(page, host):
                if candidate not in seen:
                    queue.append(candidate)

        if not _is_informative(facts):
            self.logger.info(
                "site yielded nothing to enrich with",
                extra={"site": seed, "pages_read": pages_read},
            )
            return None
        return self._to_lead(seed, facts, pages_read=pages_read)

    def _parse_page(self, html: str, url: str) -> _PageFacts:
        """Parse one document and resolve its links against its own URL."""
        parser = _PageParser()
        parser.feed(html)
        parser.close()

        facts = parser.facts
        facts.contact_url = self._contact_url(parser.hrefs, url)

        for href in parser.hrefs:
            if href.casefold().startswith("mailto:"):
                address = normalize_email(href.split(":", 1)[1].split("?", 1)[0])
                if address and address not in facts.emails:
                    facts.emails.append(address)

        # Social profiles come from the parsed JSON-LD `sameAs` list and from
        # ordinary links, in that order — a declared profile beats a footer icon.
        candidates: list[str] = list(facts.organization.get("same_as") or [])
        candidates.extend(
            href for href in parser.hrefs if not href.casefold().startswith("mailto:")
        )
        for candidate in candidates:
            classified = social_platform_for_url(urljoin(url, candidate))
            if classified is not None:
                facts.social_links.setdefault(*classified)
        return facts

    def _followable(self, page: _PageFacts, host: str | None) -> list[str]:
        """The one page worth following from this one: its contact/details page.

        Following only a single, purpose-chosen link is what keeps this an
        enrichment source rather than a site crawler.
        """
        if page.contact_url is None:
            return []
        if self._config.same_host_only and _host_of(page.contact_url) != host:
            return []
        return [page.contact_url]

    def _contact_url(self, hrefs: list[str], base: str) -> str | None:
        """Pick the most likely contact page from a page's links.

        Prefers an explicit "contact" link, then "about"; the ordering of
        :data:`_CONTACT_HINTS` is the preference order.
        """
        resolved: dict[str, str] = {}
        for href in hrefs:
            if href.casefold().startswith(("mailto:", "tel:", "javascript:", "#")):
                continue
            url = normalize_page_url(urljoin(base, href))
            if url is None or url == normalize_page_url(base):
                continue
            path = urlparse(url).path.casefold()
            for hint in _CONTACT_HINTS:
                if hint in path:
                    resolved.setdefault(hint, url)
                    break
        for hint in _CONTACT_HINTS:
            if hint in resolved:
                return resolved[hint]
        return None

    async def _fetch(self, url: str, *, required: bool = False) -> str | None:
        """GET one page.

        Args:
            url: Page to read.
            required: When True the failure is re-raised instead of swallowed.
                Set for a site's seed page, where an unreadable page means the
                site could not be enriched at all rather than that an optional
                extra page is missing.

        Returns:
            The response body, or ``None`` when an optional page is unavailable.
        """
        await self._throttle(url)
        try:
            response = await self._http.request("GET", url)
        except LeadCrawlerError:
            if required:
                raise
            # A missing optional page (a 404 on /contact) is expected and is not
            # worth a warning.
            self.logger.debug("optional page unavailable", extra={"url": url})
            return None
        return response.text

    async def _throttle(self, url: str) -> None:
        """Wait out the configured delay since the last request to this host."""
        host = _host_of(url) or ""
        delay = self._config.request_delay
        if delay > 0 and host in self._last_request:
            elapsed = time.monotonic() - self._last_request[host]
            if elapsed < delay:
                await asyncio.sleep(delay - elapsed)
        self._last_request[host] = time.monotonic()

    async def _allowed(self, url: str) -> bool:
        """Whether ``robots.txt`` permits fetching ``url``.

        Fails open: a site with no robots.txt, or one we cannot read, is treated
        as permitting the single-digit number of pages we read. That is the
        conventional reading of RFC 9309 and avoids a broken robots file
        silently disabling a source the user explicitly configured.
        """
        if not self._config.respect_robots:
            return True

        host = _host_of(url)
        if host is None:
            return False
        if host not in self._robots:
            self._robots[host] = await self._load_robots(host)

        parser = self._robots[host]
        if parser is None:
            return True
        return parser.can_fetch(self._config.user_agent or USER_AGENT_FALLBACK, url)

    async def _load_robots(self, host: str) -> RobotFileParser | None:
        robots_url = f"https://{host}/robots.txt"
        await self._throttle(robots_url)
        try:
            response = await self._http.request("GET", robots_url)
        except LeadCrawlerError as exc:
            self.logger.debug("no usable robots.txt", extra={"host": host, "error": str(exc)})
            return None

        parser = RobotFileParser()
        parser.parse(response.text.splitlines())
        return parser

    def _to_lead(self, seed: str, facts: _PageFacts, *, pages_read: int) -> RawLead:
        """Build the single company lead for one site."""
        organization = facts.organization
        domain = normalize_domain(seed)

        # Name priority: what the site says about itself in structured data
        # beats the Open Graph site name, which beats a scraped <title>.
        name = (
            clean_text(organization.get("name"))
            or clean_text(facts.site_name)
            or _clean_title(facts.title)
        )
        description = clean_text(organization.get("description")) or facts.description

        emails = facts.emails[: self._config.max_emails]
        return RawLead(
            provider=self.provider,
            external_id=domain,
            source_url=seed,
            # The schema's only address slot is the person's, and this source
            # has no person: the address here is the company's published contact
            # mailbox, which is what a company-level lead should be reachable
            # at. Every address found is preserved in `raw` regardless.
            email=emails[0] if emails else None,
            company_name=name,
            company_domain=domain,
            company_website=seed,
            company_industry=clean_text(organization.get("industry")),
            company_employee_count=organization.get("employee_count"),
            company_country=clean_text(organization.get("country")),
            company_city=clean_text(organization.get("city")),
            company_description=description,
            company_contact_url=facts.contact_url,
            company_social_links=dict(facts.social_links),
            raw={
                "seed": seed,
                "pages_read": pages_read,
                "page_title": facts.title,
                "site_name": facts.site_name,
                "emails": emails,
                "organization": {
                    key: value for key, value in organization.items() if key != "same_as"
                },
            },
        )
