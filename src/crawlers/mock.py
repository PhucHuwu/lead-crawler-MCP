"""Deterministic synthetic source.

Exists so the pipeline can be demonstrated, smoke-tested and benchmarked without
network access or credentials. It deliberately emits a configurable share of
messy records and duplicate identities, which means ``--source mock`` exercises
normalization, validation *and* deduplication rather than a clean happy path.
"""

from __future__ import annotations

import random

from src.crawlers.base import BaseCrawler
from src.crawlers.registry import register_crawler
from src.models.lead import RawLead

_FIRST_NAMES = (
    "Ada",
    "Bao",
    "Chidi",
    "Daniela",
    "Emeka",
    "Farah",
    "Gustavo",
    "Hana",
    "Ivan",
    "Jing",
    "Karim",
    "Lucia",
    "Marco",
    "Nadia",
    "Omar",
    "Priya",
    "Quan",
    "Rosa",
    "Sofia",
    "Tomas",
    "Uma",
    "Viktor",
    "Wen",
    "Yara",
    "Zane",
)

_LAST_NAMES = (
    "Alvarez",
    "Bennett",
    "Chen",
    "Dubois",
    "Eriksen",
    "Fischer",
    "Gupta",
    "Haddad",
    "Ibrahim",
    "Jensen",
    "Kowalski",
    "Larsen",
    "Moreau",
    "Nakamura",
    "Okafor",
    "Petrov",
    "Quintero",
    "Rossi",
    "Silva",
    "Tanaka",
)

_TITLES = (
    "Chief Executive Officer",
    "Chief Technology Officer",
    "VP of Sales",
    "VP Marketing",
    "Head of Growth",
    "Director of Engineering",
    "Sales Manager",
    "Senior Software Engineer",
    "Product Manager",
    "Marketing Intern",
)

_INDUSTRIES = (
    "Software",
    "Information Technology",
    "Financial Services",
    "Manufacturing",
    "Healthcare",
    "Logistics",
    "Retail",
    "Telecommunications",
)

_COUNTRIES = (
    ("United States", ("San Francisco", "Austin", "New York")),
    ("Germany", ("Berlin", "Munich")),
    ("Singapore", ("Singapore",)),
    ("Australia", ("Sydney", "Melbourne")),
    ("United Kingdom", ("London", "Manchester")),
    ("Vietnam", ("Ho Chi Minh City", "Hanoi")),
)

_COMPANY_STEMS = (
    "Northwind",
    "Contoso",
    "Globex",
    "Initech",
    "Umbrella",
    "Soylent",
    "Vandelay",
    "Hooli",
    "Aperture",
    "Cyberdyne",
)

_COMPANY_SUFFIXES = ("Labs", "Systems", "Group", "Technologies", "Partners", "Digital")


@register_crawler
class MockCrawler(BaseCrawler):
    """Generates reproducible fake B2B leads."""

    provider = "mock"
    display_name = "Mock generator"
    description = "Deterministic synthetic leads for demos, tests and dry runs."

    async def crawl(self, limit: int) -> list[RawLead]:
        rng = random.Random(self.settings.mock.seed)
        config = self.settings.mock

        leads: list[RawLead] = []
        emitted: list[dict[str, str]] = []

        for index in range(limit):
            # Recycle an earlier identity to create a realistic duplicate.
            if emitted and rng.random() < config.duplicate_ratio:
                record = dict(rng.choice(emitted))
            else:
                record = self._make_record(rng, index)
                emitted.append(record)

            messy = rng.random() < config.messy_ratio
            leads.append(self._to_raw_lead(record, index, messy))

        self.logger.info(
            "generated synthetic leads",
            extra={"count": len(leads), "seed": config.seed},
        )
        return leads

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #
    def _make_record(self, rng: random.Random, index: int) -> dict[str, str]:
        first = rng.choice(_FIRST_NAMES)
        last = rng.choice(_LAST_NAMES)
        stem = rng.choice(_COMPANY_STEMS)
        suffix = rng.choice(_COMPANY_SUFFIXES)
        company = f"{stem} {suffix}"
        domain = f"{stem.casefold()}{index}.example.com"
        country, cities = rng.choice(_COUNTRIES)

        return {
            "first_name": first,
            "last_name": last,
            "job_title": rng.choice(_TITLES),
            "email": f"{first.casefold()}.{last.casefold()}@{domain}",
            "phone": f"+1{rng.randint(2, 9)}{rng.randint(10**8, 10**9 - 1)}",
            "linkedin_url": f"https://www.linkedin.com/in/{first.casefold()}-{last.casefold()}-{index}",
            "company_name": company,
            "company_domain": domain,
            "company_industry": rng.choice(_INDUSTRIES),
            "company_employee_count": str(rng.choice([12, 45, 120, 340, 900, 2500, 8000])),
            "company_country": country,
            "company_city": rng.choice(cities),
            "company_linkedin_url": f"https://www.linkedin.com/company/{stem.casefold()}-{suffix.casefold()}",
        }

    def _to_raw_lead(self, record: dict[str, str], index: int, messy: bool) -> RawLead:
        """Wrap a generated record, optionally degrading it the way real data is."""
        fields = dict(record)

        if messy:
            # Shouting case, spreadsheet phones, and placeholder nulls are the
            # three things every real export has in common.
            fields["first_name"] = fields["first_name"].upper()
            fields["last_name"] = fields["last_name"].upper()
            fields["company_name"] = fields["company_name"].upper()
            fields["email"] = f"  {fields['email'].upper()} "
            fields["phone"] = (
                f"({fields['phone'][:2]}) {fields['phone'][2:5]}-{fields['phone'][5:]}"
            )
            if index % 3 == 0:
                fields["company_employee_count"] = "N/A"
            if index % 5 == 0:
                fields["company_industry"] = "n/a"

        return RawLead(
            provider=self.provider,
            external_id=f"mock-{index:06d}",
            source_url=f"https://example.invalid/leads/{index}",
            first_name=fields["first_name"],
            last_name=fields["last_name"],
            job_title=fields["job_title"],
            email=fields["email"],
            phone=fields["phone"],
            linkedin_url=fields["linkedin_url"],
            company_name=fields["company_name"],
            company_domain=fields["company_domain"],
            company_industry=fields["company_industry"],
            company_employee_count=fields["company_employee_count"],
            company_country=fields["company_country"],
            company_city=fields["company_city"],
            company_linkedin_url=fields["company_linkedin_url"],
            raw=dict(fields),
        )
