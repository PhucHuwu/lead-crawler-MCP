"""Shared pytest fixtures.

Every test that touches settings or the filesystem goes through these fixtures so
that no test can accidentally read the developer's real ``.env`` or write into the
repository's ``data/`` directory.
"""

from __future__ import annotations

import os
import socket
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from src.config import FilterSettings, Settings, load_settings, misnamed_env_vars
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
    # `misnamed_env_vars` is the other half of the same problem: those names are
    # not read, but their presence stops a run, so a stray `LOG_LEVEL` or
    # `APOLLO_API_KEY` in the developer's shell would fail unrelated tests.
    # Derived from the settings models rather than repeated here, so a new
    # setting cannot make the suite non-hermetic again.
    guarded = misnamed_env_vars()
    for key in list(os.environ):
        if key.startswith("LEAD_") or key in guarded:
            monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)


class NetworkAccessAttempted(RuntimeError):
    """Raised when a test tries to open a real outbound connection."""


def _blocked(what: str) -> NetworkAccessAttempted:
    return NetworkAccessAttempted(
        f"a test attempted a real network call ({what}). "
        "Outbound HTTP must be mocked with respx; see tests/test_apollo.py. "
        "If a test genuinely needs a socket, opt out with "
        "@pytest.mark.allow_network."
    )


@pytest.fixture(autouse=True)
def _forbid_network(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail any test that reaches for the network.

    "Tests must never call the real Apollo API" is a property worth *enforcing*
    rather than trusting: respx's ``assert_all_mocked`` only guards a test that
    happens to make a request through a mocked router, so a future edit that
    called ``crawl()`` outside a respx context would quietly hit the live API —
    and with a key in the environment, would spend real credits and leak real
    contact data into an assertion.

    The chokepoints are :func:`socket.getaddrinfo` and ``socket.socket.connect``,
    which every outbound TCP path goes through: httpx, requests, and anything
    built on them. Blocking name resolution as well as connection means the
    failure message arrives before any DNS traffic leaves the machine.

    Opt out per-test with ``@pytest.mark.allow_network`` for the rare case that
    needs a real socket (a local server, say).
    """
    if request.node.get_closest_marker("allow_network"):
        return

    def deny_getaddrinfo(*args: Any, **kwargs: Any) -> Any:
        raise _blocked(f"DNS lookup of {args[0] if args else '?'!r}")

    def deny_connect(self: socket.socket, address: Any, *args: Any, **kwargs: Any) -> Any:
        raise _blocked(f"connection to {address!r}")

    def deny_connect_ex(self: socket.socket, address: Any, *args: Any, **kwargs: Any) -> Any:
        raise _blocked(f"connection to {address!r}")

    def deny_create_connection(address: Any, *args: Any, **kwargs: Any) -> Any:
        raise _blocked(f"connection to {address!r}")

    monkeypatch.setattr(socket, "getaddrinfo", deny_getaddrinfo)
    monkeypatch.setattr(socket.socket, "connect", deny_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", deny_connect_ex)
    # ``create_connection`` is the higher-level helper urllib3 uses when it is
    # handed a hostname rather than an address, so it needs closing too.
    monkeypatch.setattr(socket, "create_connection", deny_create_connection)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Default settings writing into a per-test temporary directory."""
    return load_settings(output_dir=tmp_path / "out")


@pytest.fixture
def empty_filters() -> FilterSettings:
    """Filter criteria with every rule at its default (nothing filtered)."""
    return FilterSettings()


def log_record(caplog: pytest.LogCaptureFixture, message: str) -> Any:
    """The one captured record whose message is exactly ``message``.

    The return type is ``Any`` because the fields worth asserting on are the ones
    passed through ``extra=``, which :class:`logging.LogRecord` does not declare —
    an attribute a test can read but a type checker cannot see. Asserting that
    exactly one record matched also keeps a test from passing on a line it did
    not mean to inspect.
    """
    matches = [record for record in caplog.records if record.getMessage() == message]
    assert len(matches) == 1, f"expected one {message!r} record, found {len(matches)}"
    return matches[0]


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
    external_id: str | None = None,
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
