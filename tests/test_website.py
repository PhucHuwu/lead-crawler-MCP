"""Tests for the company-website enrichment source.

Every test runs against ``respx``, so no request can leave the process — which
matters more here than elsewhere, because this is the one adapter that reads
pages belonging to someone else. Politeness is asserted rather than assumed:
the robots rules, the page cap, the same-host rule and the delay are all
behaviour under test, not implementation details.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from src.config import Settings, load_settings
from src.crawlers.website import WebsiteCrawler
from src.models.lead import RawLead
from src.processors.geo import country_matches
from src.processors.normalizer import Normalizer
from src.processors.validator import LeadValidator
from src.utils.errors import CrawlerError

HOME = "https://acme.com"
CONTACT = "https://acme.com/contact"
ROBOTS = "https://acme.com/robots.txt"

#: Stand-in for a site's contact page, which the homepage fixture links to.
MINIMAL_CONTACT = "<html><head><title>Contact us</title></head></html>"

FULL_PAGE = """
<html><head>
  <title>Acme Corp | Industrial widgets</title>
  <meta property="og:site_name" content="Acme Corp">
  <meta name="description" content="Widgets, made well.">
  <script type="application/ld+json">
  {"@context": "https://schema.org", "@graph": [
    {"@type": "Organization",
     "name": "Acme Corporation",
     "description": "Acme builds industrial widgets.",
     "industry": "Manufacturing",
     "numberOfEmployees": {"@type": "QuantitativeValue", "value": 250},
     "address": {"@type": "PostalAddress",
                 "addressLocality": "Singapore", "addressCountry": "SG"},
     "sameAs": ["https://www.linkedin.com/company/acme",
                "https://github.com/acme"]}]}
  </script>
</head><body>
  <a href="mailto:hello@acme.com">Mail us</a>
  <a href="mailto:hello@acme.com">Again</a>
  <a href="mailto:sales@acme.com">Sales</a>
  <a href="/contact">Contact</a>
  <a href="https://twitter.com/acme">Twitter</a>
</body></html>
"""


def website_settings(tmp_path: Path, **website: Any) -> Settings:
    """Settings pointing the website source at test-only URLs.

    ``respect_robots`` defaults to off here: most tests are about extraction,
    and a robots fetch in each one would add a request that says nothing about
    what is being asserted. The tests that *are* about robots turn it on.
    """
    return load_settings(
        output_dir=tmp_path / "out",
        website={"urls": ["acme.com"], "request_delay": 0.0, "respect_robots": False, **website},
        http_initial_backoff=0.001,
        http_max_backoff=0.002,
    )


@pytest.fixture
async def make_crawler(tmp_path: Path) -> Any:
    """Factory for crawlers that are always closed, even when a test fails."""
    created: list[WebsiteCrawler] = []

    def _make(**website: Any) -> WebsiteCrawler:
        instance = WebsiteCrawler(website_settings(tmp_path, **website))
        created.append(instance)
        return instance

    yield _make

    for instance in created:
        await instance.aclose()


def mock_site(*, home: str = FULL_PAGE, contact: str | None = MINIMAL_CONTACT) -> None:
    """Route the seed page and its contact page.

    ``contact`` defaults to a small page rather than a 404 because the homepage
    fixture links to ``/contact``: leaving it unrouted would let respx's
    ``assert_all_mocked`` surface as an error the crawler legitimately swallows,
    which would make request counts look wrong for the wrong reason.
    """
    respx.get(HOME).mock(return_value=httpx.Response(200, text=home))
    if contact is not None:
        respx.get(CONTACT).mock(return_value=httpx.Response(200, text=contact))


def fetched() -> set[str]:
    """Absolute URLs requested during this test, with the empty path dropped.

    ``httpx`` normalizes ``https://acme.com`` to ``https://acme.com/``, so
    comparing raw URLs would fail on a distinction nobody cares about.
    """
    return {str(call.request.url).rstrip("/") for call in respx.calls}


class TestAvailability:
    def test_no_urls_means_unavailable(self, tmp_path: Path) -> None:
        crawler = WebsiteCrawler(load_settings(output_dir=tmp_path / "out"))
        ok, reason = crawler.is_available()
        assert ok is False
        assert "--website-url" in reason

    def test_urls_make_it_available(self, make_crawler: Callable[..., WebsiteCrawler]) -> None:
        assert make_crawler().is_available() == (True, "")

    @respx.mock
    async def test_unusable_urls_are_reported(
        self, make_crawler: Callable[..., WebsiteCrawler]
    ) -> None:
        with pytest.raises(CrawlerError, match="no usable website URLs"):
            await make_crawler(urls=["not a domain", "  "]).crawl(5)


class TestExtraction:
    @respx.mock
    async def test_one_lead_per_site(self, make_crawler: Callable[..., WebsiteCrawler]) -> None:
        mock_site()
        leads = await make_crawler().crawl(5)
        assert len(leads) == 1
        assert leads[0].provider == "website"
        assert leads[0].company_domain == "acme.com"

    @respx.mock
    async def test_structured_data_wins_over_the_page_title(
        self, make_crawler: Callable[..., WebsiteCrawler]
    ) -> None:
        mock_site()
        lead = (await make_crawler().crawl(5))[0]
        # The JSON-LD name, not "Acme Corp" from og:site_name and not the <title>.
        assert lead.company_name == "Acme Corporation"

    @respx.mock
    async def test_open_graph_beats_the_title(
        self, make_crawler: Callable[..., WebsiteCrawler]
    ) -> None:
        page = (
            "<html><head><title>Acme | Widgets</title>"
            '<meta property="og:site_name" content="Acme Corp"></head></html>'
        )
        mock_site(home=page)
        assert (await make_crawler().crawl(5))[0].company_name == "Acme Corp"

    @respx.mock
    async def test_the_title_is_split_on_its_tagline(
        self, make_crawler: Callable[..., WebsiteCrawler]
    ) -> None:
        mock_site(home="<html><head><title>Acme Corp - We build widgets</title></head></html>")
        assert (await make_crawler().crawl(5))[0].company_name == "Acme Corp"

    @respx.mock
    async def test_organization_details_are_extracted(
        self, make_crawler: Callable[..., WebsiteCrawler]
    ) -> None:
        mock_site()
        lead = (await make_crawler().crawl(5))[0]
        assert lead.company_description == "Acme builds industrial widgets."
        assert lead.company_industry == "Manufacturing"
        assert lead.company_employee_count == 250
        assert lead.company_country == "SG"
        assert lead.company_city == "Singapore"

    @respx.mock
    async def test_meta_description_is_the_fallback(
        self, make_crawler: Callable[..., WebsiteCrawler]
    ) -> None:
        page = '<html><head><meta name="description" content="Widgets, made well."></head></html>'
        mock_site(home=page)
        assert (await make_crawler().crawl(5))[0].company_description == "Widgets, made well."

    @respx.mock
    async def test_emails_are_collected_once_and_capped(
        self, make_crawler: Callable[..., WebsiteCrawler]
    ) -> None:
        mock_site()
        lead = (await make_crawler(max_emails=1).crawl(5))[0]
        # hello@ appears twice on the page; the cap keeps the first only.
        assert lead.email == "hello@acme.com"
        assert lead.raw["emails"] == ["hello@acme.com"]

    @respx.mock
    async def test_social_links_come_from_both_sources(
        self, make_crawler: Callable[..., WebsiteCrawler]
    ) -> None:
        mock_site()
        links = (await make_crawler().crawl(5))[0].company_social_links
        # The declared profile is canonicalized on the way in, so one company
        # cannot end up with two LinkedIn URLs depending on where it was seen.
        assert links["linkedin"] == "https://www.linkedin.com/company/acme"  # from sameAs
        assert links["github"] == "https://github.com/acme"  # from sameAs
        assert links["x"] == "https://twitter.com/acme"  # from an <a href>

    @respx.mock
    async def test_raw_payload_keeps_what_the_model_cannot_hold(
        self, make_crawler: Callable[..., WebsiteCrawler]
    ) -> None:
        mock_site()
        raw = (await make_crawler().crawl(5))[0].raw
        assert raw["seed"] == HOME
        assert raw["page_title"] == "Acme Corp | Industrial widgets"
        assert raw["emails"] == ["hello@acme.com", "sales@acme.com"]

    @respx.mock
    async def test_malformed_structured_data_is_ignored(
        self, make_crawler: Callable[..., WebsiteCrawler]
    ) -> None:
        page = (
            "<html><head><title>Acme</title>"
            '<script type="application/ld+json">{not json}</script></head></html>'
        )
        mock_site(home=page)
        lead = (await make_crawler().crawl(5))[0]
        assert lead.company_name == "Acme"

    @respx.mock
    async def test_a_barren_site_produces_no_lead(
        self, make_crawler: Callable[..., WebsiteCrawler]
    ) -> None:
        # A domain-only "lead" would just restate what the caller configured.
        mock_site(home="<html><head></head><body>Nothing here.</body></html>")
        assert await make_crawler().crawl(5) == []


class TestPoliteness:
    @respx.mock
    async def test_only_the_contact_page_is_followed(
        self, make_crawler: Callable[..., WebsiteCrawler]
    ) -> None:
        mock_site()
        await make_crawler().crawl(5)
        assert fetched() == {HOME, CONTACT}

    @respx.mock
    async def test_the_page_cap_is_respected(
        self, make_crawler: Callable[..., WebsiteCrawler]
    ) -> None:
        mock_site()
        lead = (await make_crawler(max_pages_per_site=1).crawl(5))[0]
        assert lead.raw["pages_read"] == 1
        assert fetched() == {HOME}

    @respx.mock
    async def test_an_offsite_contact_link_is_not_followed(
        self, make_crawler: Callable[..., WebsiteCrawler]
    ) -> None:
        # same_host_only keeps this an enrichment source rather than a crawler.
        page = (
            "<html><head><title>Acme</title></head>"
            '<body><a href="https://tracker.example/contact">Contact</a></body></html>'
        )
        mock_site(home=page, contact=None)
        await make_crawler(same_host_only=True).crawl(5)
        assert fetched() == {HOME}

    @respx.mock
    async def test_robots_disallow_is_honoured(
        self, make_crawler: Callable[..., WebsiteCrawler]
    ) -> None:
        respx.get(ROBOTS).mock(
            return_value=httpx.Response(200, text="User-agent: *\nDisallow: /\n")
        )
        home = respx.get(HOME).mock(return_value=httpx.Response(200, text=FULL_PAGE))
        leads = await make_crawler(respect_robots=True).crawl(5)
        assert leads == []
        assert not home.called

    @respx.mock
    async def test_a_missing_robots_file_allows_everything(
        self, make_crawler: Callable[..., WebsiteCrawler]
    ) -> None:
        # A 404 is the normal answer for a site with no robots.txt; treating it
        # as a refusal would disable the source for most of the web.
        respx.get(ROBOTS).mock(return_value=httpx.Response(404))
        mock_site()
        assert len(await make_crawler(respect_robots=True).crawl(5)) == 1

    @respx.mock
    async def test_a_delay_is_enforced_between_requests(
        self, make_crawler: Callable[..., WebsiteCrawler]
    ) -> None:
        mock_site()
        crawler = make_crawler(request_delay=0.05)
        started = time.monotonic()
        await crawler.crawl(5)
        # Two page requests, so at least one delay elapses between them.
        assert time.monotonic() - started >= 0.05

    @respx.mock
    async def test_the_configured_user_agent_is_sent(
        self, make_crawler: Callable[..., WebsiteCrawler]
    ) -> None:
        mock_site()
        await make_crawler().crawl(5)
        assert "TinasoftLeadCrawler" in respx.calls[0].request.headers["user-agent"]


class TestSeeds:
    @respx.mock
    async def test_repeat_spellings_of_one_site_are_fetched_once(
        self, make_crawler: Callable[..., WebsiteCrawler]
    ) -> None:
        mock_site()
        crawler = make_crawler(urls=["acme.com", "https://www.acme.com/", "ACME.COM"])
        leads = await crawler.crawl(5)
        assert len(leads) == 1
        # One homepage read, plus the contact page it links to.
        assert fetched() == {HOME, CONTACT}

    @respx.mock
    async def test_the_limit_bounds_how_many_sites_are_read(
        self, make_crawler: Callable[..., WebsiteCrawler]
    ) -> None:
        respx.get("https://acme.com").mock(return_value=httpx.Response(200, text=FULL_PAGE))
        respx.get(CONTACT).mock(return_value=httpx.Response(200, text=MINIMAL_CONTACT))
        respx.get("https://beta.com").mock(return_value=httpx.Response(200, text=FULL_PAGE))
        crawler = make_crawler(urls=["acme.com", "beta.com"])
        leads = await crawler.crawl(1)
        assert len(leads) == 1
        assert "https://beta.com" not in fetched()


class TestFailureContainment:
    @respx.mock
    async def test_one_broken_site_does_not_sink_the_others(
        self, make_crawler: Callable[..., WebsiteCrawler]
    ) -> None:
        respx.get("https://acme.com").mock(return_value=httpx.Response(500))
        respx.get("https://beta.com").mock(return_value=httpx.Response(200, text=FULL_PAGE))
        leads = await make_crawler(urls=["acme.com", "beta.com"]).crawl(5)
        assert len(leads) == 1
        assert leads[0].company_domain == "beta.com"

    @respx.mock
    async def test_a_missing_contact_page_is_not_a_failure(
        self, make_crawler: Callable[..., WebsiteCrawler]
    ) -> None:
        mock_site(contact=None)
        respx.get(CONTACT).mock(return_value=httpx.Response(404))
        leads = await make_crawler().crawl(5)
        assert leads[0].company_name == "Acme Corporation"

    @respx.mock
    async def test_every_site_failing_is_a_source_error(
        self, make_crawler: Callable[..., WebsiteCrawler]
    ) -> None:
        respx.get("https://acme.com").mock(return_value=httpx.Response(500))
        respx.get("https://beta.com").mock(return_value=httpx.Response(503))
        with pytest.raises(CrawlerError, match="all 2 website"):
            await make_crawler(urls=["acme.com", "beta.com"]).crawl(5)


class TestPipelineIntegration:
    """The point of this source is that its output survives the pipeline.

    A company-level record has no person, so it exercises the identity rules the
    other sources never reach.
    """

    @respx.mock
    async def test_a_website_lead_normalizes_and_validates(
        self, make_crawler: Callable[..., WebsiteCrawler]
    ) -> None:
        mock_site()
        raw = (await make_crawler().crawl(5))[0]
        lead = Normalizer().normalize(raw)

        assert lead.company.name == "Acme Corporation"
        assert lead.company.domain == "acme.com"
        assert lead.company.contact_url == CONTACT
        assert lead.company.social_links["github"] == "https://github.com/acme"
        # The JSON-LD address carried "SG"; the canonical stored form is the ISO
        # code, and a country filter written either way still matches it.
        assert lead.company.country == "SG"
        assert country_matches(lead.company.country, "Singapore")
        assert lead.person.full_name is None
        assert LeadValidator().validate(lead).is_valid

    @respx.mock
    async def test_the_company_linkedin_survives_deduplication(
        self, make_crawler: Callable[..., WebsiteCrawler]
    ) -> None:
        # The pipeline matches companies on their LinkedIn page, which reaches
        # it through the social-links mapping rather than the dedicated field.
        mock_site()
        raw: RawLead = (await make_crawler().crawl(5))[0]
        lead = Normalizer().normalize(raw)
        assert lead.company.linkedin_url == "https://www.linkedin.com/company/acme"
