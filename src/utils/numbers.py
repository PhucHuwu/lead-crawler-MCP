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


#: A headcount band written as ``min,max``. The comma is the separator some
#: sources use; the hyphen and the word "to" are handled by ``_RANGE_SPLIT_RE``.
_RANGE_COMMA_RE = re.compile(r"^\s*(\d+)\s*,\s*(\d+)\s*$")


def parse_employee_range(value: object) -> tuple[int, int] | None:
    """Parse a *search* band such as ``201-500`` into ``(201, 500)``.

    Distinct from :func:`parse_employee_count`, which reads a band found in
    upstream data and keeps only its lower bound. This one is for bands a user
    writes in configuration, where both ends matter — Apollo searches by band.

    Accepts ``201-500``, ``201 – 500``, ``201 to 500`` and ``201,500``. The
    comma form is unambiguous in YAML and inside a JSON array, but *not* in a
    comma-separated environment value, where the comma already means "next
    item"; configuration using the plain CSV form should write ``201-500``.

    Returns:
        ``(low, high)``, or ``None`` when the text is not a band. A malformed
        band is reported by the caller, which can name the offending setting.
    """
    text = clean_text(value)
    if text is None:
        return None

    if (comma := _RANGE_COMMA_RE.match(text.replace("–", "-").replace("—", "-"))) is not None:
        start, end = int(comma.group(1)), int(comma.group(2))
        return (start, end) if start <= end else None

    parts = [part for part in _RANGE_SPLIT_RE.split(text) if part.strip()]
    if len(parts) != 2:
        return None
    low, high = _parse_scalar(parts[0]), _parse_scalar(parts[1])
    if low is None or high is None or low > high:
        return None
    return low, high
