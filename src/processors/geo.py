"""Country canonicalization.

Sources express location as ISO codes (``US``), English names (``United States``)
or local names (``Deutschland``). Country filters only work if those collapse to
one form, so recognized values are normalized to ISO 3166-1 alpha-2.

The table below is deliberately a curated subset of the markets Tinasoft targets,
not a full ISO dump: unrecognized values are passed through unchanged rather than
guessed at. Extend :data:`COUNTRY_ALIASES` as new markets appear.
"""

from __future__ import annotations

import re

from src.utils.text import clean_text

#: ISO alpha-2 -> English name. This is the canonical output form.
COUNTRY_NAMES: dict[str, str] = {
    "AE": "United Arab Emirates",
    "AR": "Argentina",
    "AT": "Austria",
    "AU": "Australia",
    "BE": "Belgium",
    "BR": "Brazil",
    "CA": "Canada",
    "CH": "Switzerland",
    "CL": "Chile",
    "CN": "China",
    "CO": "Colombia",
    "CZ": "Czechia",
    "DE": "Germany",
    "DK": "Denmark",
    "EE": "Estonia",
    "EG": "Egypt",
    "ES": "Spain",
    "FI": "Finland",
    "FR": "France",
    "GB": "United Kingdom",
    "GR": "Greece",
    "HK": "Hong Kong",
    "HU": "Hungary",
    "ID": "Indonesia",
    "IE": "Ireland",
    "IL": "Israel",
    "IN": "India",
    "IT": "Italy",
    "JP": "Japan",
    "KR": "South Korea",
    "LT": "Lithuania",
    "LU": "Luxembourg",
    "LV": "Latvia",
    "MX": "Mexico",
    "MY": "Malaysia",
    "NG": "Nigeria",
    "NL": "Netherlands",
    "NO": "Norway",
    "NZ": "New Zealand",
    "PH": "Philippines",
    "PK": "Pakistan",
    "PL": "Poland",
    "PT": "Portugal",
    "RO": "Romania",
    "SA": "Saudi Arabia",
    "SE": "Sweden",
    "SG": "Singapore",
    "SK": "Slovakia",
    "TH": "Thailand",
    "TR": "Turkey",
    "TW": "Taiwan",
    "UA": "Ukraine",
    "US": "United States",
    "VN": "Vietnam",
    "ZA": "South Africa",
}

#: Alias (casefolded, punctuation-stripped) -> ISO alpha-2.
COUNTRY_ALIASES: dict[str, str] = {
    "america": "US",
    "aus": "AU",
    "britain": "GB",
    "can": "CA",
    "chn": "CN",
    "czech republic": "CZ",
    "deu": "DE",
    "deutschland": "DE",
    "espana": "ES",
    "gbr": "GB",
    "ger": "DE",
    "great britain": "GB",
    "holland": "NL",
    "india": "IN",
    "jpn": "JP",
    "kor": "KR",
    "mex": "MX",
    "nederland": "NL",
    "new zealand": "NZ",
    "nzl": "NZ",
    "prc": "CN",
    "rfa": "DE",
    "sgp": "SG",
    "singapore": "SG",
    "south korea": "KR",
    "the netherlands": "NL",
    "uae": "AE",
    "uk": "GB",
    "united states of america": "US",
    "usa": "US",
    "viet nam": "VN",
    "england": "GB",
    "scotland": "GB",
    "wales": "GB",
    "northern ireland": "GB",
    "ukraine": "UA",
}

_NON_ALNUM_RE = re.compile(r"[^a-z0-9 ]+")

# Reverse index: canonical English name -> ISO alpha-2.
_NAME_TO_CODE = {name.casefold(): code for code, name in COUNTRY_NAMES.items()}
_CODE_SET = frozenset(COUNTRY_NAMES)


def _key(value: str) -> str:
    return _NON_ALNUM_RE.sub(" ", value.casefold()).strip()


def normalize_country(value: object) -> str | None:
    """Canonicalize a country to its ISO alpha-2 code when recognized.

    Args:
        value: A country code, English or common local name.

    Returns:
        The ISO alpha-2 code (``"US"``), or the cleaned input unchanged when it
        is not recognized — pass-through beats a wrong guess.
    """
    text = clean_text(value)
    if text is None:
        return None

    upper = text.upper()
    if upper in _CODE_SET:
        return upper

    key = _key(text)
    if key in COUNTRY_ALIASES:
        return COUNTRY_ALIASES[key]
    if key in _NAME_TO_CODE:
        return _NAME_TO_CODE[key]
    return text


def country_matches(candidate: str | None, expected: str) -> bool:
    """Whether a normalized country satisfies a filter term.

    Compares on the canonical code *and* the display name, so a filter written as
    ``United States`` still matches a lead stored as ``US``.
    """
    if not candidate:
        return False

    normalized = normalize_country(candidate)
    if normalized is None:
        return False

    target_upper = expected.strip().upper()
    target_key = _key(expected)

    # Direct match against the stored or canonicalized value.
    if normalized.upper() == target_upper or _key(normalized) == target_key:
        return True

    # Match through the code table in both directions.
    expected_code = (
        target_upper
        if target_upper in _CODE_SET
        else COUNTRY_ALIASES.get(target_key) or _NAME_TO_CODE.get(target_key)
    )
    if expected_code is None:
        return _key(normalized) == target_key

    candidate_code = (
        normalized.upper()
        if normalized.upper() in _CODE_SET
        else COUNTRY_ALIASES.get(_key(normalized)) or _NAME_TO_CODE.get(_key(normalized))
    )
    return candidate_code == expected_code
