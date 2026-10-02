"""Tests for the Apollo source and the shared HTTP/retry layer.

The HTTP client is exercised through :class:`ApolloCrawler` rather than in
isolation, because that is how every real source uses it — the retry, backoff and
status-code mapping behaviour is what matters downstream, not the internals.

``respx`` defaults to ``assert_all_mocked=True``, so a request that no test route
matches raises instead of quietly reaching the network. No extra guard is needed,
and adding one would break respx's own interception.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest
import respx
from tenacity import AsyncRetrying, RetryCallState

from src.config import Settings, load_settings
from src.crawlers.apollo import ApolloCrawler
from src.models.lead import RawLead
from src.utils.errors import (
    CrawlerError,
    SourceAuthError,
    SourceRateLimitError,
    SourceUnavailableError,
)
from src.utils.http import MAX_HONORED_RETRY_AFTER, AsyncHttpClient, RetryPolicy, _SourceAwareWait

BASE_URL = "https://api.apollo.io/api/v1"
SEARCH_PATH = "/mixed_people/search"


def apollo_settings(**apollo: Any) -> Settings:
    """Settings with a usable key and near-instant backoff.

    The real backoff starts at half a second, which would make a suite that
    deliberately triggers retries take seconds for no added coverage.
    """
    return load_settings(
        apollo={"api_key": "test-key", **apollo},
        http_initial_backoff=0.001,
        http_max_backoff=0.002,
        http_max_attempts=3,
    )


@pytest.fixture
async def make_crawler() -> Any:
    """Factory for crawlers that are always closed, even when a test fails."""
    created: list[ApolloCrawler] = []

    def _make(**apollo: Any) -> ApolloCrawler:
        instance = ApolloCrawler(apollo_settings(**apollo))
        created.append(instance)
        return instance

    yield _make

    for instance in created:
        await instance.aclose()


@respx.mock
async def test_requires_an_api_key(settings: Settings) -> None:
    crawler = ApolloCrawler(settings)
    ok, reason = crawler.is_available()
    assert ok is False
    assert "LEAD_APOLLO__API_KEY" in reason

    with pytest.raises(SourceAuthError, match="no API key"):
        await crawler.crawl(1)
    await crawler.aclose()


class TestRequestShape:
    @respx.mock
    async def test_api_key_is_sent_as_a_header(
        self, make_crawler: Callable[..., ApolloCrawler]
    ) -> None:
        route = respx.post(f"{BASE_URL}{SEARCH_PATH}").mock(
            return_value=httpx.Response(200, json={"people": []})
        )
        await make_crawler().crawl(5)
        assert route.calls[0].request.headers["x-api-key"] == "test-key"

    @respx.mock
    async def test_omitted_filters_are_not_sent(
        self, make_crawler: Callable[..., ApolloCrawler]
    ) -> None:
        # Apollo distinguishes an absent key from an empty list, so an unset
        # filter must not be serialized as [].
        route = respx.post(f"{BASE_URL}{SEARCH_PATH}").mock(
            return_value=httpx.Response(200, json={"people": []})
        )
        await make_crawler().crawl(5)
        body = json.loads(route.calls[0].request.content)
        assert body["page"] == 1
        assert "person_titles" not in body
        assert "q_keywords" not in body

    @respx.mock
    async def test_configured_filters_are_forwarded(
        self, make_crawler: Callable[..., ApolloCrawler]
    ) -> None:
        route = respx.post(f"{BASE_URL}{SEARCH_PATH}").mock(
            return_value=httpx.Response(200, json={"people": []})
        )
        crawler = make_crawler(person_titles=["CTO"], q_keywords="fintech")
        await crawler.crawl(5)
        body = json.loads(route.calls[0].request.content)
        assert body["person_titles"] == ["CTO"]
        assert body["q_keywords"] == "fintech"

    @respx.mock
    async def test_page_size_never_exceeds_the_limit(
        self, make_crawler: Callable[..., ApolloCrawler]
    ) -> None:
        route = respx.post(f"{BASE_URL}{SEARCH_PATH}").mock(
            return_value=httpx.Response(200, json={"people": []})
        )
        await make_crawler().crawl(3)
        assert json.loads(route.calls[0].request.content)["per_page"] == 3


class TestResponseMapping:
    @respx.mock
    async def test_a_person_record_is_mapped(
        self, make_crawler: Callable[..., ApolloCrawler]
    ) -> None:
        respx.post(f"{BASE_URL}{SEARCH_PATH}").mock(
            return_value=httpx.Response(
                200,
                json={
                    "people": [
                        {
                            "id": "abc123",
                            "first_name": "Ada",
                            "last_name": "Lovelace",
                            "name": "Ada Lovelace",
                            "title": "CTO",
                            "seniority": "c_suite",
                            "email": "ada@acme.com",
                            "linkedin_url": "https://www.linkedin.com/in/ada",
                            "organization": {
                                "name": "Acme Corp",
                                "primary_domain": "acme.com",
                                "industry": "Software",
                                "estimated_num_employees": 250,
                                "country": "United States",
                                "city": "Austin",
                                "linkedin_url": "https://www.linkedin.com/company/acme",
                            },
                        }
                    ]
                },
            )
        )
        lead = (await make_crawler().crawl(1))[0]
        assert lead.provider == "apollo"
        assert lead.external_id == "abc123"
        assert lead.full_name == "Ada Lovelace"
        assert lead.job_title == "CTO"
        assert lead.company_domain == "acme.com"
        assert lead.company_employee_count == 250
        assert lead.company_city == "Austin"
        assert lead.raw["id"] == "abc123"

    @respx.mock
    async def test_source_url_is_derived_from_the_id(
        self, make_crawler: Callable[..., ApolloCrawler]
    ) -> None:
        respx.post(f"{BASE_URL}{SEARCH_PATH}").mock(
            return_value=httpx.Response(200, json={"people": [{"id": "abc123"}]})
        )
        lead = (await make_crawler().crawl(1))[0]
        assert lead.source_url == "https://app.apollo.io/#/contacts/abc123"

    @respx.mock
    async def test_sparse_records_do_not_raise(
        self, make_crawler: Callable[..., ApolloCrawler]
    ) -> None:
        # Apollo omits keys freely; an unrecognizable record must still survive
        # long enough for the validator to reject it with a reason.
        respx.post(f"{BASE_URL}{SEARCH_PATH}").mock(
            return_value=httpx.Response(200, json={"people": [{}]})
        )
        lead = (await make_crawler().crawl(1))[0]
        assert lead.external_id is None
        assert lead.source_url is None
        assert lead.company_name is None

    @respx.mock
    async def test_blank_strings_are_treated_as_absent(
        self, make_crawler: Callable[..., ApolloCrawler]
    ) -> None:
        respx.post(f"{BASE_URL}{SEARCH_PATH}").mock(
            return_value=httpx.Response(200, json={"people": [{"first_name": "   ", "title": ""}]})
        )
        lead = (await make_crawler().crawl(1))[0]
        assert lead.first_name is None
        assert lead.job_title is None

    @respx.mock
    async def test_container_values_are_not_stringified(
        self, make_crawler: Callable[..., ApolloCrawler]
    ) -> None:
        respx.post(f"{BASE_URL}{SEARCH_PATH}").mock(
            return_value=httpx.Response(
                200,
                json={"people": [{"last_name": ["Lovelace"], "organization": ["nope"]}]},
            )
        )
        lead = (await make_crawler().crawl(1))[0]
        assert lead.last_name is None
        assert lead.company_name is None

    @respx.mock
    async def test_alternate_people_key_is_accepted(
        self, make_crawler: Callable[..., ApolloCrawler]
    ) -> None:
        respx.post(f"{BASE_URL}{SEARCH_PATH}").mock(
            return_value=httpx.Response(200, json={"contacts": [{"id": "x1"}]})
        )
        assert (await make_crawler().crawl(1))[0].external_id == "x1"


class TestPhoneExtraction:
    @pytest.mark.parametrize(
        ("person", "expected"),
        [
            ({"sanitized_phone": "+14155550142"}, "+14155550142"),
            ({"phone_numbers": [{"sanitized_number": "+14155550143"}]}, "+14155550143"),
            ({"phone_numbers": [{"raw_number": "+1 415 555 0144"}]}, "+1 415 555 0144"),
            ({"phone_numbers": [{"number": "+14155550145"}]}, "+14155550145"),
            # The direct field wins over the list.
            (
                {
                    "sanitized_phone": "+14155550142",
                    "phone_numbers": [{"sanitized_number": "+19999999999"}],
                },
                "+14155550142",
            ),
            ({"phone_numbers": []}, None),
            ({"phone_numbers": ["not-a-dict"]}, None),
            ({"phone_numbers": [{"other": "x"}]}, None),
        ],
    )
    @respx.mock
    async def test_phone_shapes(
        self,
        make_crawler: Callable[..., ApolloCrawler],
        person: dict[str, Any],
        expected: str | None,
    ) -> None:
        respx.post(f"{BASE_URL}{SEARCH_PATH}").mock(
            return_value=httpx.Response(200, json={"people": [person]})
        )
        assert (await make_crawler().crawl(1))[0].phone == expected


class TestPagination:
    @respx.mock
    async def test_stops_when_a_page_is_short(
        self, make_crawler: Callable[..., ApolloCrawler]
    ) -> None:
        # A short page means the result set is exhausted; asking for more would
        # burn credits for nothing.
        route = respx.post(f"{BASE_URL}{SEARCH_PATH}").mock(
            side_effect=[
                httpx.Response(200, json={"people": [{"id": "1"}, {"id": "2"}]}),
                httpx.Response(200, json={"people": [{"id": "3"}]}),
            ]
        )
        leads = await make_crawler(per_page=2).crawl(10)
        assert [lead.external_id for lead in leads] == ["1", "2", "3"]
        assert len(route.calls) == 2

    @respx.mock
    async def test_stops_at_the_limit_without_a_second_request(
        self, make_crawler: Callable[..., ApolloCrawler]
    ) -> None:
        route = respx.post(f"{BASE_URL}{SEARCH_PATH}").mock(
            return_value=httpx.Response(200, json={"people": [{"id": "1"}]})
        )
        leads = await make_crawler(per_page=5).crawl(1)
        assert len(leads) == 1
        assert len(route.calls) == 1

    @respx.mock
    async def test_an_empty_page_ends_the_run(
        self, make_crawler: Callable[..., ApolloCrawler]
    ) -> None:
        route = respx.post(f"{BASE_URL}{SEARCH_PATH}").mock(
            return_value=httpx.Response(200, json={"people": []})
        )
        assert await make_crawler().crawl(10) == []
        assert len(route.calls) == 1

    @respx.mock
    async def test_page_count_is_capped(self, make_crawler: Callable[..., ApolloCrawler]) -> None:
        # A source that never returns a short page must not be able to spin
        # forever, so the page loop has a hard ceiling.
        route = respx.post(f"{BASE_URL}{SEARCH_PATH}").mock(
            return_value=httpx.Response(200, json={"people": [{"id": "1"}]})
        )
        leads = await make_crawler(per_page=1).crawl(50)
        assert len(route.calls) == 20  # MAX_PAGES
        assert len(leads) == 20
        assert [lead.external_id for lead in leads] == ["1"] * 20


class TestErrorHandling:
    @respx.mock
    async def test_auth_failure_is_not_retried(
        self, make_crawler: Callable[..., ApolloCrawler]
    ) -> None:
        # Retrying a rejected key only delays the error.
        route = respx.post(f"{BASE_URL}{SEARCH_PATH}").mock(
            return_value=httpx.Response(401, json={"error": "invalid key"})
        )
        with pytest.raises(SourceAuthError, match="rejected our credentials"):
            await make_crawler().crawl(1)
        assert len(route.calls) == 1

    @pytest.mark.parametrize("status", [400, 404, 422])
    @respx.mock
    async def test_client_errors_are_not_retried(
        self, make_crawler: Callable[..., ApolloCrawler], status: int
    ) -> None:
        route = respx.post(f"{BASE_URL}{SEARCH_PATH}").mock(
            return_value=httpx.Response(status, text="bad request")
        )
        with pytest.raises(CrawlerError, match=f"HTTP {status}"):
            await make_crawler().crawl(1)
        assert len(route.calls) == 1

    @respx.mock
    async def test_transient_server_errors_are_retried(
        self, make_crawler: Callable[..., ApolloCrawler]
    ) -> None:
        route = respx.post(f"{BASE_URL}{SEARCH_PATH}").mock(
            side_effect=[
                httpx.Response(503),
                httpx.Response(200, json={"people": [{"id": "1"}]}),
            ]
        )
        leads = await make_crawler().crawl(1)
        assert [lead.external_id for lead in leads] == ["1"]
        assert len(route.calls) == 2

    @respx.mock
    async def test_rate_limit_is_retried(self, make_crawler: Callable[..., ApolloCrawler]) -> None:
        route = respx.post(f"{BASE_URL}{SEARCH_PATH}").mock(
            side_effect=[
                httpx.Response(429, headers={"Retry-After": "0"}, json={}),
                httpx.Response(200, json={"people": [{"id": "1"}]}),
            ]
        )
        assert len(await make_crawler().crawl(1)) == 1
        assert len(route.calls) == 2

    @respx.mock
    async def test_retries_are_bounded_and_the_last_error_surfaces(
        self, make_crawler: Callable[..., ApolloCrawler]
    ) -> None:
        route = respx.post(f"{BASE_URL}{SEARCH_PATH}").mock(return_value=httpx.Response(500))
        with pytest.raises(SourceUnavailableError, match="HTTP 500"):
            await make_crawler().crawl(1)
        assert len(route.calls) == 3  # http_max_attempts

    @respx.mock
    async def test_timeouts_are_reported_as_source_unavailable(
        self, make_crawler: Callable[..., ApolloCrawler]
    ) -> None:
        respx.post(f"{BASE_URL}{SEARCH_PATH}").mock(side_effect=httpx.ConnectTimeout("too slow"))
        with pytest.raises(SourceUnavailableError, match="timed out"):
            await make_crawler().crawl(1)

    @respx.mock
    async def test_non_json_body_is_a_crawler_error(
        self, make_crawler: Callable[..., ApolloCrawler]
    ) -> None:
        respx.post(f"{BASE_URL}{SEARCH_PATH}").mock(
            return_value=httpx.Response(200, text="<html>maintenance</html>")
        )
        with pytest.raises(CrawlerError, match="non-JSON body"):
            await make_crawler().crawl(1)

    @pytest.mark.parametrize("payload", [[], "text", 42, {"people": "nope"}])
    @respx.mock
    async def test_unexpected_payload_shapes_are_rejected(
        self, make_crawler: Callable[..., ApolloCrawler], payload: Any
    ) -> None:
        respx.post(f"{BASE_URL}{SEARCH_PATH}").mock(return_value=httpx.Response(200, json=payload))
        with pytest.raises(CrawlerError, match="apollo"):
            await make_crawler().crawl(1)

    @respx.mock
    async def test_non_dict_entries_are_skipped(
        self, make_crawler: Callable[..., ApolloCrawler]
    ) -> None:
        respx.post(f"{BASE_URL}{SEARCH_PATH}").mock(
            return_value=httpx.Response(200, json={"people": ["junk", {"id": "1"}]})
        )
        leads = await make_crawler().crawl(10)
        assert [lead.external_id for lead in leads] == ["1"]


class TestRetryAfterParsing:
    """Direct checks on the Retry-After handling that the crawler tests can only
    observe indirectly."""

    @staticmethod
    def _state(exc: BaseException | None, attempt: int = 1) -> RetryCallState:
        state = RetryCallState(retry_object=AsyncRetrying(), fn=None, args=(), kwargs={})
        state.attempt_number = attempt
        if exc is not None:
            # tenacity wants an exc_info triple, not a bare exception.
            state.set_exception((type(exc), exc, exc.__traceback__))
        return state

    def test_server_supplied_delay_is_honoured(self) -> None:
        wait = _SourceAwareWait(RetryPolicy())
        state = self._state(SourceRateLimitError("apollo", "429", retry_after=2.0))
        assert wait(state) == 2.0

    def test_absurd_delays_are_capped(self) -> None:
        # A misconfigured source must not be able to park the whole run.
        wait = _SourceAwareWait(RetryPolicy())
        state = self._state(SourceRateLimitError("apollo", "429", retry_after=3600.0))
        assert wait(state) == MAX_HONORED_RETRY_AFTER

    def test_rate_limit_without_a_delay_falls_back_to_backoff(self) -> None:
        wait = _SourceAwareWait(RetryPolicy(initial_backoff=0.5, max_backoff=20.0))
        state = self._state(SourceRateLimitError("apollo", "429"), attempt=1)
        assert 0.5 <= wait(state) <= 0.625

    def test_falls_back_to_exponential_backoff(self) -> None:
        wait = _SourceAwareWait(RetryPolicy(initial_backoff=0.5, max_backoff=20.0))
        state = self._state(SourceUnavailableError("apollo", "503"), attempt=3)
        # 0.5 * 2^2 = 2.0, plus up to 25% jitter.
        assert 2.0 <= wait(state) <= 2.5

    def test_backoff_is_capped(self) -> None:
        wait = _SourceAwareWait(RetryPolicy(initial_backoff=1.0, max_backoff=4.0))
        state = self._state(SourceUnavailableError("apollo", "503"), attempt=10)
        assert 4.0 <= wait(state) <= 5.0

    @pytest.mark.parametrize(
        ("header", "expected"),
        [("5", 5.0), (" 7 ", 7.0), ("0", 0.0), ("-3", 0.0), ("soon", None), (None, None)],
    )
    @respx.mock
    async def test_retry_after_header_forms(
        self, header: str | None, expected: float | None
    ) -> None:
        headers = {"Retry-After": header} if header is not None else {}
        respx.post(f"{BASE_URL}{SEARCH_PATH}").mock(
            return_value=httpx.Response(429, headers=headers)
        )
        client = AsyncHttpClient(
            provider="apollo",
            base_url=BASE_URL,
            retry=RetryPolicy(max_attempts=1),
        )
        try:
            with pytest.raises(SourceRateLimitError) as excinfo:
                await client.post_json(SEARCH_PATH, json={})
        finally:
            await client.aclose()
        assert excinfo.value.retry_after == expected


class TestLifecycle:
    async def test_aclose_is_idempotent(self) -> None:
        crawler = ApolloCrawler(apollo_settings())
        await crawler.aclose()
        await crawler.aclose()

    @respx.mock
    async def test_context_manager_releases_the_client(self) -> None:
        respx.post(f"{BASE_URL}{SEARCH_PATH}").mock(
            return_value=httpx.Response(200, json={"people": []})
        )
        async with ApolloCrawler(apollo_settings()) as crawler:
            await crawler.crawl(1)

    @respx.mock
    async def test_client_is_rebuilt_after_close(self) -> None:
        respx.post(f"{BASE_URL}{SEARCH_PATH}").mock(
            return_value=httpx.Response(200, json={"people": []})
        )
        crawler = ApolloCrawler(apollo_settings())
        await crawler.crawl(1)
        await crawler.aclose()
        assert await crawler.crawl(1) == []
        await crawler.aclose()

    def test_class_metadata_is_complete(self) -> None:
        assert ApolloCrawler.display_name
        assert ApolloCrawler.description
        assert ApolloCrawler.requires_credentials is True


def test_raw_payload_is_retained_for_debugging() -> None:
    """The mapped fields are a projection; the original record must survive."""
    crawler = ApolloCrawler(apollo_settings())
    person = {"id": "1", "unknown_field": "kept"}
    lead: RawLead = crawler._map_person(person)
    assert lead.raw == person
