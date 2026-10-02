"""Job-title to seniority inference.

Sources give titles as free text ("VP, Demand Gen (EMEA)"), so seniority is
derived by matching the highest-ranking keyword present. Order matters: the
check runs from the most senior band down, so "Regional Director of Sales"
resolves to *director* rather than matching "sales".
"""

from __future__ import annotations

import re

from src.models.enums import SeniorityLevel
from src.utils.text import clean_text

#: Ordered most-senior-first. The first band with a matching keyword wins.
_SENIORITY_KEYWORDS: tuple[tuple[SeniorityLevel, tuple[str, ...]], ...] = (
    (
        SeniorityLevel.FOUNDER,
        (
            "founder",
            "co-founder",
            "cofounder",
            "proprietor",
            "owner",
            "managing partner",
        ),
    ),
    (
        SeniorityLevel.C_SUITE,
        (
            "chief",
            "ceo",
            "cto",
            "cfo",
            "coo",
            "cmo",
            "cro",
            "cio",
            "cpo",
            "cdo",
            "ciso",
            "chro",
            "cso",
            "president",
            "managing director",
            "general manager",
        ),
    ),
    (
        SeniorityLevel.VP,
        (
            "vice president",
            "vice-president",
            "svp",
            "evp",
            "avp",
            "vp",
        ),
    ),
    (
        SeniorityLevel.DIRECTOR,
        (
            "director",
            "head of",
        ),
    ),
    (
        SeniorityLevel.MANAGER,
        (
            "manager",
            "supervisor",
            "team lead",
            "tech lead",
            "leader",
        ),
    ),
    (
        SeniorityLevel.SENIOR,
        (
            "senior",
            "sr.",
            "sr ",
            "principal",
            "staff ",
            "lead ",
            "architect",
            "distinguished",
        ),
    ),
    (
        SeniorityLevel.INTERN,
        (
            "intern",
            "internship",
            "trainee",
            "apprentice",
            "working student",
        ),
    ),
    (
        SeniorityLevel.ENTRY,
        (
            "junior",
            "jr.",
            "jr ",
            "associate",
            "assistant",
            "graduate",
            "entry level",
        ),
    ),
)

#: Token-ish patterns (single abbreviations) that need word-boundary matching so
#: "vp" does not match inside "developer" and "sr" does not match "sri".
_WORD_BOUNDED = frozenset(
    {
        "vp",
        "svp",
        "evp",
        "avp",
        "ceo",
        "cto",
        "cfo",
        "coo",
        "cmo",
        "cro",
        "cio",
        "cpo",
        "cdo",
        "ciso",
        "chro",
        "cso",
    }
)

#: Keywords that must not match when preceded by one of these words. "President"
#: is a C-suite title, but "Vice President" is a VP — the qualifier changes the
#: band, so the more specific reading wins.
_BLOCKED_PRECEDERS: dict[str, tuple[str, ...]] = {
    "president": ("vice", "deputy", "assistant"),
}


def _matches(title: str, keyword: str) -> bool:
    """Whether a keyword appears in an already casefolded, space-padded title."""
    if blockers := _BLOCKED_PRECEDERS.get(keyword):
        # Separate lookbehinds rather than an alternation: Python requires every
        # branch of a lookbehind to be the same width.
        prefix = "".join(rf"(?<!{re.escape(word)} )" for word in blockers)
        return re.search(rf"{prefix}\b{re.escape(keyword)}\b", title) is not None
    if keyword in _WORD_BOUNDED:
        return re.search(rf"\b{re.escape(keyword)}\b", title) is not None
    return keyword in title


def infer_seniority(job_title: str | None) -> SeniorityLevel:
    """Infer a seniority band from a free-text job title.

    Returns :attr:`SeniorityLevel.UNKNOWN` when the title is empty or matches no
    known keyword — an honest "we don't know" rather than a guess.
    """
    title = clean_text(job_title)
    if title is None:
        return SeniorityLevel.UNKNOWN

    # Normalize separators so "VP / Sales" and "VP | Sales" both hit "vp".
    # Hyphens are preserved because several keywords are hyphenated
    # ("co-founder", "vice-president").
    haystack = f" {title.casefold().replace('/', ' ').replace('|', ' ').replace(',', ' ')} "

    for level, keywords in _SENIORITY_KEYWORDS:
        if any(_matches(haystack, keyword) for keyword in keywords):
            return level
    return SeniorityLevel.UNKNOWN
