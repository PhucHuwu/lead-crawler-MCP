"""Shared record fixtures.

A package rather than a module so tests can import the corpus by name
(``from tests.fixtures.records import complete_lead``) without every test file
depending on one another's import order. No ``__init__`` side effects: importing
this package must not build records, register exporters, or touch the filesystem.
"""

from __future__ import annotations

from tests.fixtures.records import (
    HOSTILE_URLS,
    INTERNATIONAL_COMPANIES,
    INTERNATIONAL_NAMES,
    INVALID_EMAILS,
    MALFORMED_DOMAINS,
    MALFORMED_PAGE_URLS,
    NON_FETCHABLE_URLS,
    UNICODE_HOSTS,
    VIETNAMESE_CITIES,
    VIETNAMESE_CSV,
    VIETNAMESE_NAMES,
    VIETNAMESE_NAMES_NFD,
    UnicodeForm,
    complete_lead,
    missing_company,
    missing_email,
    sparse_lead,
    vietnamese_lead,
)

__all__ = [
    "HOSTILE_URLS",
    "INTERNATIONAL_COMPANIES",
    "INTERNATIONAL_NAMES",
    "INVALID_EMAILS",
    "MALFORMED_DOMAINS",
    "MALFORMED_PAGE_URLS",
    "NON_FETCHABLE_URLS",
    "UNICODE_HOSTS",
    "VIETNAMESE_CITIES",
    "VIETNAMESE_CSV",
    "VIETNAMESE_NAMES",
    "VIETNAMESE_NAMES_NFD",
    "UnicodeForm",
    "complete_lead",
    "missing_company",
    "missing_email",
    "sparse_lead",
    "vietnamese_lead",
]
