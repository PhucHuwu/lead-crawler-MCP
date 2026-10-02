"""Shared pytest fixtures.

Every test that touches settings or the filesystem goes through these fixtures so
that no test can accidentally read the developer's real ``.env`` or write into the
repository's ``data/`` directory.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from src.config import FilterSettings, Settings, load_settings
from src.models.company import Company
from src.models.lead import LeadSource, RawLead, StandardizedLead
from src.models.person import Person


@pytest.fixture(autouse=True)
def _isolate_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Stop the ambient environment from leaking into tests.

    Any ``LEAD_*`` variable set by the developer's shell or CI would otherwise
    change behaviour mid-suite. The default output directory is also redirected
    into the test's tmp_path.
    """
    for key in list(os.environ):
        if key.startswith("LEAD_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Default settings writing into a per-test temporary directory."""
    return load_settings(output_dir=tmp_path / "out")


@pytest.fixture
def empty_filters() -> FilterSettings:
    """Filter criteria with every rule at its default (nothing filtered)."""
    return FilterSettings()


def make_raw_lead(**overrides: object) -> RawLead:
    """Build a RawLead with sane defaults, overridable per field."""
    defaults: dict[str, object] = {
        "provider": "test",
        "external_id": "ext-1",
        "first_name": "Ada",
        "last_name": "Lovelace",
        "job_title": "VP of Engineering",
        "email": "ada@acme.com",
        "company_name": "Acme Corp",
        "company_domain": "acme.com",
    }
    defaults.update(overrides)
    return RawLead(**defaults)


#: Distinguishes "caller omitted this" from "caller passed None" in fixtures.
#: ``make_raw_lead`` deliberately does not do this: a RawLead with components that
#: disagree with ``full_name`` is *valid input*, since reconciling them is the
#: normalizer's job. A StandardizedLead is post-normalization, so the same
#: disagreement there would be a fixture bug rather than a test case.
_UNSET: Any = object()


def _resolve_name_parts(
    first_name: Any, last_name: Any, full_name: str | None
) -> tuple[str | None, str | None]:
    """Keep name components coherent with ``full_name`` unless set explicitly."""
    if first_name is _UNSET and last_name is _UNSET:
        if not full_name:
            return None, None
        head, _, tail = full_name.rpartition(" ")
        return head or None, tail or None
    return (
        "Ada" if first_name is _UNSET else first_name,
        "Lovelace" if last_name is _UNSET else last_name,
    )


def make_lead(
    *,
    first_name: str | None | Any = _UNSET,
    last_name: str | None | Any = _UNSET,
    full_name: str | None = "Ada Lovelace",
    email: str | None = "ada@acme.com",
    job_title: str | None = "VP of Engineering",
    linkedin_url: str | None = None,
    phone: str | None = None,
    company_name: str | None = "Acme Corp",
    company_domain: str | None = "acme.com",
    employee_count: int | None = 250,
    country: str | None = "US",
    industry: str | None = "Software",
    provider: str = "test",
    external_id: str | None = "ext-1",
) -> StandardizedLead:
    """Build a StandardizedLead directly, bypassing the normalizer.

    Name components follow an overridden ``full_name`` unless the caller sets
    them too. Otherwise ``make_lead(full_name="Alan Turing")`` would carry Ada
    Lovelace's components alongside Alan's name — a record describing two people,
    which is precisely what the deduplicator matches on.
    """
    resolved_first, resolved_last = _resolve_name_parts(first_name, last_name, full_name)
    return StandardizedLead(
        person=Person(
            first_name=resolved_first,
            last_name=resolved_last,
            full_name=full_name,
            job_title=job_title,
            email=email,
            phone=phone,
            linkedin_url=linkedin_url,
        ),
        company=Company(
            name=company_name,
            domain=company_domain,
            employee_count=employee_count,
            country=country,
            industry=industry,
        ),
        source=LeadSource(provider=provider, external_id=external_id),
    )


@pytest.fixture
def lead_factory() -> object:
    """Expose :func:`make_lead` as a fixture for tests that prefer injection."""
    return make_lead


@pytest.fixture
def raw_lead_factory() -> object:
    """Expose :func:`make_raw_lead` as a fixture."""
    return make_raw_lead


@pytest.fixture
def sample_csv(tmp_path: Path) -> Iterator[Path]:
    """A small CSV covering clean rows, messy casing and a junk row."""
    path = tmp_path / "leads.csv"
    path.write_text(
        "First Name,Last Name,Position,Email Address,Company,Website,Employees,Country\n"
        "Ada,Lovelace,VP of Engineering,ada@acme.com,Acme Corp,acme.com,201-500,United States\n"
        "  GRACE ,HOPPER,COO,GRACE@NAVY.EXAMPLE.COM,Navy Systems,navy.example.com,1000+,usa\n"
        "Alan,Turing,,alan@bletchley.example.com,Bletchley Park,bletchley.example.com,N/A,UK\n"
        ",,,,Ghost Corp,,,,,\n",
        encoding="utf-8",
    )
    yield path
