"""Numeric parsing helpers for messy upstream values."""

from __future__ import annotations

import re

from src.utils.text import clean_text

# "1.2k", "3M", "1,200", "201-500", "1000+", "approx. 250"
_MAGNITUDE_RE = re.compile(r"^(?P<number>\d+(?:\.\d+)?)\s*(?P<suffix>[kmb])?$", re.IGNORECASE)
_MAGNITUDES = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}

# Separators used to express a headcount band.
_RANGE_SPLIT_RE = re.compile(r"\s*(?:-|–|—|to)\s*", re.IGNORECASE)


def _parse_scalar(token: str) -> int | None:
    token = token.strip().rstrip("+").strip()
    if not token:
        return None
    token = token.replace(",", "").replace("_", "")
    match = _MAGNITUDE_RE.match(token)
    if match is None:
        return None
    value = float(match.group("number"))
    suffix = match.group("suffix")
    if suffix:
        value *= _MAGNITUDES[suffix.casefold()]
    if value < 0:
        return None
    return int(value)


def parse_employee_count(value: object) -> int | None:
    """Parse a headcount from the many shapes upstream sources use.

    Understands plain ints, thousands separators, magnitude suffixes
    (``1.2k``), open-ended values (``1000+``) and bands (``201-500``).

    For a band, the **lower bound** is returned. That is a deliberate,
    conservative choice: ``min_employees``/``max_employees`` filters then never
    overstate a company's size. See the README's "Known limitations".
    """
    if value is None:
        return None

    if isinstance(value, bool):  # bool is an int subclass; reject explicitly.
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        return int(value) if value >= 0 else None

    text = clean_text(value)
    if text is None:
        return None

    # Drop surrounding prose that some sources include ("201-500 employees",
    # "approx. 250"). The trailing `\.?` absorbs abbreviations such as "approx."
    # whose period would otherwise defeat a trailing word boundary.
    text = re.sub(
        r"(?i)\b(?:employees|employee|staff|people|headcount|approx|about)\b\.?", " ", text
    )
    text = text.replace("~", " ").replace(",", "").strip()
    if not text:
        return None

    if (parts := [part for part in _RANGE_SPLIT_RE.split(text) if part.strip()]) and len(parts) > 1:
        # A range such as "201-500" only bounds the headcount, so the lower end
        # is the honest answer: the company has *at least* that many people.
        return _parse_scalar(parts[0])

    return _parse_scalar(text)
