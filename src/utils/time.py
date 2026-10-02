"""Timezone-aware timestamp helpers.

All timestamps in the system are UTC and timezone-aware. Naive datetimes are
treated as UTC, which is what every upstream API we consume actually returns.
"""

from __future__ import annotations

from datetime import UTC, datetime


def utcnow() -> datetime:
    """Current time as a timezone-aware UTC datetime."""
    return datetime.now(UTC)


def ensure_utc(value: datetime | None) -> datetime | None:
    """Coerce a datetime to timezone-aware UTC.

    Naive datetimes are assumed to already be UTC (the convention used by the
    APIs we ingest) rather than local time, which avoids host-dependent output.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def to_iso(value: datetime | None) -> str | None:
    """Render a datetime as an ISO-8601 UTC string, or ``None``.

    Uses the ``Z`` suffix rather than ``+00:00`` so that timestamps written by
    hand-built views (CSV) are byte-identical to those produced by Pydantic's
    JSON serialization — downstream parsers should never have to handle two
    spellings of the same instant.
    """
    normalized = ensure_utc(value)
    if normalized is None:
        return None
    return normalized.isoformat().replace("+00:00", "Z")
