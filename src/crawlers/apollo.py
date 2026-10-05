"""Apollo.io people-search source.

Calls the ``mixed_people/search`` endpoint and maps each result onto
:class:`RawLead`. Everything specific to Apollo's response shape lives in
:meth:`ApolloCrawler._map_person`; nothing downstream knows this source exists.

Note on emails: people *search* returns contact records but generally **not**
email addresses — those require a separate reveal call that consumes credits.
Leads from this source therefore usually arrive without an email and are
qualified on name, title and company instead. If email coverage matters, run the
result through an enrichment step in a later phase rather than widening this
adapter.

Search criteria come from three layers, weakest first::

    LEAD_APOLLO__* settings  <  a named search profile  <  explicit CLI flags

The profile is the one described in :mod:`src.search_profiles`; ``None`` means
"no profile requested", which leaves the environment values alone.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

from src.crawlers.base import BaseCrawler
from src.crawlers.registry import register_crawler
from src.models.lead import RawLead
from src.search_profiles import SearchProfile, get_search_profile
from src.utils.errors import ConfigError, CrawlerError, SourceAuthError
from src.utils.http import AsyncHttpClient, RetryPolicy

if TYPE_CHECKING:
    from src.config import ApolloSettings, Settings

#: Apollo's own seniority vocabulary. Deliberately not
#: :class:`~src.models.enums.SeniorityLevel`: Apollo distinguishes ``head`` from
#: ``director``, and coercing to our coarser levels would silently search for
#: directors instead. Kept here because it is Apollo's vocabulary, not ours.
APOLLO_SENIORITIES: frozenset[str] = frozenset(
    {
        "owner",
        "founder",
        "c_suite",
        "partner",
        "vp",
        "head",
        "director",
        "manager",
        "senior",
        "entry",
        "intern",
    }
)


@dataclass(frozen=True, slots=True)
class ApolloSearch:
    """One fully-resolved Apollo query, independent of where the values came from.

    Freezing the merge result keeps the layers (settings, profile, flags) from
    being re-applied at request time, where a bug would be much harder to see.
    """

    titles: tuple[str, ...] = ()
    seniorities: tuple[str, ...] = ()
    person_locations: tuple[str, ...] = ()
    organization_locations: tuple[str, ...] = ()
    industries: tuple[str, ...] = ()
    #: ``"min,max"`` bands, already canonical.
    employee_ranges: tuple[str, ...] = ()
    keywords: str | None = None
    similar_titles: bool = True

    def to_query(self, *, page: int, per_page: int) -> dict[str, Any]:
        """Assemble the request body, omitting empty filters.

        Apollo treats an explicitly empty list differently from an absent key,
        so empty filters are dropped rather than sent as ``[]`` — sending an
        empty ``person_titles`` would constrain the search to nobody.
        """
        query: dict[str, Any] = {"page": page, "per_page": per_page}

        optional: dict[str, Any] = {
            "person_titles": list(self.titles),
            "person_seniorities": list(self.seniorities),
            "person_locations": list(self.person_locations),
            "organization_locations": list(self.organization_locations),
            "organization_industries": list(self.industries),
            "organization_num_employees_ranges": list(self.employee_ranges),
            "q_keywords": self.keywords,
        }
        for key, value in optional.items():
            if value:
                query[key] = value

        # Sent whenever a title filter is present: without titles there is
        # nothing to widen, and Apollo ignores it anyway.
        if self.titles:
            query["include_similar_titles"] = self.similar_titles
        return query

    def summary(self) -> dict[str, Any]:
        """Log-friendly view. Contains no credential and no request body."""
        return {
            "titles": list(self.titles),
            "seniorities": list(self.seniorities),
            "person_locations": list(self.person_locations),
            "organization_locations": list(self.organization_locations),
            "industries": list(self.industries),
            "employee_ranges": list(self.employee_ranges),
            "keywords": self.keywords,
            "similar_titles": self.similar_titles,
        }


def _validated_seniorities(values: list[str], *, source: str) -> tuple[str, ...]:
    """Check seniority values against Apollo's vocabulary.

    Raises:
        ConfigError: naming the rejected value and the whole allowed set, since
            the caller is usually looking at a YAML file rather than a schema.
    """
    cleaned: list[str] = []
    for value in values:
        candidate = value.strip().casefold().replace("-", "_").replace(" ", "_")
        if candidate not in APOLLO_SENIORITIES:
            allowed = ", ".join(sorted(APOLLO_SENIORITIES))
            raise ConfigError(
                f"{source}: {value!r} is not an Apollo seniority; use one of: {allowed}"
            )
        if candidate not in cleaned:
            cleaned.append(candidate)
    return tuple(cleaned)


def _pick(profile_value: list[str] | None, fallback: list[str]) -> tuple[str, ...]:
    """The profile's value when it set one, otherwise the configured value."""
    return tuple(profile_value) if profile_value else tuple(fallback)


def resolve_search(
    config: ApolloSettings, profile: SearchProfile | None = None, *, source: str = "apollo"
) -> ApolloSearch:
    """Merge a search profile over the environment settings.

    A profile *replaces* the corresponding settings field rather than adding to
    it: a profile is a complete statement of intent, and unioning lists from two
    places produces a search nobody wrote. Fields the profile leaves unset keep
    their configured value.
    """
    if profile is None:
        return ApolloSearch(
            titles=tuple(config.person_titles),
            seniorities=_validated_seniorities(
                config.person_seniorities, source="LEAD_APOLLO__PERSON_SENIORITIES"
            ),
            person_locations=tuple(config.person_locations),
            organization_locations=tuple(config.organization_locations),
            industries=tuple(config.organization_industries),
            employee_ranges=tuple(config.employee_count_ranges),
            keywords=config.q_keywords,
            similar_titles=config.include_similar_titles,
        )

    return ApolloSearch(
        titles=_pick(profile.titles, config.person_titles),
        seniorities=_validated_seniorities(
            profile.seniorities or config.person_seniorities,
            source=f"search profile {source!r}",
        ),
        person_locations=_pick(profile.person_locations, config.person_locations),
        organization_locations=_pick(profile.locations, config.organization_locations),
        industries=_pick(profile.industries, config.organization_industries),
        employee_ranges=_pick(profile.employee_ranges, config.employee_count_ranges),
        keywords=profile.keywords or config.q_keywords,
        # Only a profile that states a preference overrides the setting; `False`
        # is a real choice, so this cannot be a truthiness test.
        similar_titles=(
            config.include_similar_titles
            if profile.similar_titles is None
            else profile.similar_titles
        ),
    )


@register_crawler
class ApolloCrawler(BaseCrawler):
    """Searches the Apollo.io people database."""

    provider = "apollo"
    display_name = "Apollo.io"
    description = "Search the Apollo.io B2B contact database (requires an API key)."
    requires_credentials = True
    #: Apollo's per-page ceiling, as documented by the API.
    max_per_page: ClassVar[int] = 100

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        config = settings.apollo
        self._config = config
        self._max_pages = config.max_pages

        profile_name = settings.search_profile
        profile = get_search_profile(profile_name, path=settings.search_profiles_path)
        self._search = resolve_search(config, profile, source=profile_name or "apollo")

        headers = {
            "Content-Type": "application/json",
            "Cache-Control": "no-cache",
            "Accept": "application/json",
        }
        # The key is injected here rather than per-request so it can never be
        # echoed into a log line or an error message by the request path.
        if config.is_configured and config.api_key is not None:
            headers["x-api-key"] = config.api_key.get_secret_value()

        self._http = AsyncHttpClient(
            provider=self.provider,
            base_url=config.base_url,
            headers=headers,
            timeout=settings.http_timeout,
            retry=RetryPolicy(
                max_attempts=settings.http_max_attempts,
                initial_backoff=settings.http_initial_backoff,
                max_backoff=settings.http_max_backoff,
            ),
        )

    @property
    def search(self) -> ApolloSearch:
        """The resolved search, exposed so a caller can log or test it."""
        return self._search

    def is_available(self) -> tuple[bool, str]:
        if not self._config.is_configured:
            return False, "set LEAD_APOLLO__API_KEY to use this source"
        return True, ""

    async def aclose(self) -> None:
        await self._http.aclose()

    async def crawl(self, limit: int) -> list[RawLead]:
        if not self._config.is_configured:
            raise SourceAuthError(self.provider, "no API key configured")

        per_page = min(self._config.per_page, self.max_per_page, limit)
        collected: list[RawLead] = []
        unmappable = 0
        self.logger.debug("apollo search resolved", extra=self._search.summary())

        for page in range(1, self._max_pages + 1):
            if len(collected) >= limit:
                break

            payload = await self._http.post_json(
                "/mixed_people/search",
                json=self._search.to_query(page=page, per_page=per_page),
            )
            people = self._extract_people(payload)
            if not people:
                self.logger.info(
                    "apollo returned no further results",
                    extra={"page": page, "collected": len(collected)},
                )
                break

            # One person Apollo describes in a way we cannot map costs that
            # person, not the page — let alone the run.
            page_leads, failed = self.map_records(
                people,
                self._map_person,
                kind="Apollo person",
                label=_person_label,
            )
            collected.extend(page_leads)
            unmappable += failed

            if len(people) < per_page:
                # A short page means we have reached the end of the result set.
                break

        result = collected[:limit]
        self.logger.info(
            "collected leads from apollo",
            extra={"count": len(result), "requested": limit, "unmappable": unmappable},
        )
        return result

    @staticmethod
    def _extract_people(payload: Any) -> list[dict[str, Any]]:
        """Pull the people array out of a response, tolerating shape drift."""
        if not isinstance(payload, dict):
            raise CrawlerError("apollo", f"expected a JSON object, got {type(payload).__name__}")
        people = payload.get("people") or payload.get("contacts") or []
        if not isinstance(people, list):
            raise CrawlerError("apollo", "response field 'people' was not a list")
        return [person for person in people if isinstance(person, dict)]

    # ------------------------------------------------------------------ #
    # Response mapping
    # ------------------------------------------------------------------ #
    def _map_person(self, person: dict[str, Any]) -> RawLead:
        """Map one Apollo person record onto :class:`RawLead`.

        Written defensively: Apollo omits keys freely and nests organization
        data, so every access goes through :func:`_get`.
        """
        organization = person.get("organization")
        organization = organization if isinstance(organization, dict) else {}

        external_id = _as_str(_get(person, "id"))
        phone = _first_phone(person)

        return RawLead(
            provider=self.provider,
            external_id=external_id,
            source_url=f"https://app.apollo.io/#/contacts/{external_id}" if external_id else None,
            first_name=_as_str(_get(person, "first_name")),
            last_name=_as_str(_get(person, "last_name")),
            # Apollo's `name` is a display name and is the most reliable single
            # field; the normalizer reconciles it with the components.
            full_name=_as_str(_get(person, "name")),
            job_title=_as_str(_get(person, "title")),
            seniority=_as_str(_get(person, "seniority")),
            email=_as_str(_get(person, "email")),
            phone=phone,
            linkedin_url=_as_str(_get(person, "linkedin_url")),
            company_name=_as_str(_get(organization, "name")),
            company_domain=_as_str(
                _get(organization, "primary_domain") or _get(organization, "domain")
            ),
            company_website=_as_str(_get(organization, "website_url")),
            company_industry=_as_str(_get(organization, "industry")),
            company_employee_count=_get(organization, "estimated_num_employees"),
            company_country=_as_str(_get(organization, "country")),
            company_city=_as_str(_get(organization, "city")),
            company_linkedin_url=_as_str(_get(organization, "linkedin_url")),
            raw=person,
        )


def _person_label(person: dict[str, Any]) -> str:
    """Identify one Apollo person in a log line.

    Prefers the stable id, then the email, then the name — the same order of
    certainty as :meth:`RawLead.label`, but readable off the *unmapped* payload,
    which is the only thing available when mapping is what failed.
    """
    for key in ("id", "email", "name"):
        if (value := _as_str(_get(person, key))) is not None:
            return value
    return "<unidentified>"


def _get(mapping: dict[str, Any], key: str) -> Any:
    """Read a key that may be absent, null or an empty string."""
    value = mapping.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return value


def _as_str(value: Any) -> str | None:
    """Coerce a scalar to ``str`` without stringifying containers."""
    if value is None or isinstance(value, (dict, list)):
        return None
    return str(value)


def _first_phone(person: dict[str, Any]) -> str | None:
    """Extract a phone number from Apollo's several possible representations."""
    if (direct := _as_str(_get(person, "sanitized_phone"))) is not None:
        return direct

    phone_numbers = person.get("phone_numbers")
    if isinstance(phone_numbers, list):
        for entry in phone_numbers:
            if isinstance(entry, dict):
                for key in ("sanitized_number", "raw_number", "number"):
                    if (value := _as_str(_get(entry, key))) is not None:
                        return value
    return None
