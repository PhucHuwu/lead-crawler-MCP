"""Pure text-normalization helpers.

These functions are intentionally dependency-free and side-effect-free so they
can be unit-tested in isolation and reused by any stage of the pipeline.
Everything returns ``None`` for input that carries no information, which keeps
"missing" and "empty" indistinguishable downstream (they are equivalent).
"""

from __future__ import annotations

import re
import unicodedata

# Characters that upstream exports use as "empty" markers.
_NULL_TOKENS = frozenset(
    {"", "-", "--", "n/a", "na", "none", "null", "nil", "unknown", "undefined"}
)

_WHITESPACE_RE = re.compile(r"\s+")

# Unicode category "C" == control/format/surrogate/private-use/unassigned.
_IGNORABLE_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn"})

# "Name <email@host>" and bare "mailto:email@host"
_ANGLE_EMAIL_RE = re.compile(r"<([^<>@\s]+@[^<>\s]+)>")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s.]+(\.[^@\s.]+)+$")
_MAILTO_RE = re.compile(r"^mailto:", re.IGNORECASE)

_PHONE_ALLOWED_RE = re.compile(r"[^\d+]")

# Common credential suffixes we strip from job titles ("CEO at Acme" -> "CEO").
_TITLE_AT_RE = re.compile(r"\s+(?:at|@)\s+.+$", re.IGNORECASE)


def strip_ignorable(value: str) -> str:
    """Remove zero-width and other invisible formatting characters."""
    return "".join(ch for ch in value if unicodedata.category(ch) not in _IGNORABLE_CATEGORIES)


def clean_text(value: object) -> str | None:
    """Normalize free text: NFKC, strip invisible chars, collapse whitespace.

    Returns ``None`` when the result is empty or a known "no value" token.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)

    # NFKC folds full-width characters and ligatures into their canonical form,
    # which matters for names and company names pasted from non-Latin sources.
    text = unicodedata.normalize("NFKC", value)
    text = strip_ignorable(text)
    text = _WHITESPACE_RE.sub(" ", text).strip()

    if text.casefold() in _NULL_TOKENS:
        return None
    return text


def titlecase_name(value: object) -> str | None:
    """Title-case a person or company name while preserving intentional casing.

    Names that already contain lowercase letters after the first character
    (``McDonald``, ``van der Berg``, ``eBay``) are left untouched so we do not
    destroy information the source gave us. Only shouting segments are folded, and
    they are folded per sub-token so ``O'NEILL`` becomes ``O'Neill`` and
    ``MARY-JANE`` becomes ``Mary-Jane``.
    """
    text = clean_text(value)
    if text is None:
        return None

    def case_token(token: str) -> str:
        """Fold an ALL-CAPS token to Title case; leave anything else alone."""
        if not token or not token.isupper():
            return token
        return token[:1].upper() + token[1:].lower()

    def case_word(word: str) -> str:
        # Apostrophes split a word so "O'NEILL" folds on both sides.
        return "'".join(case_token(part) for part in word.split("'"))

    return " ".join(
        "-".join(case_word(part) for part in word.split("-")) for word in text.split(" ")
    )


def split_full_name(full_name: str | None) -> tuple[str | None, str | None]:
    """Best-effort split of a full name into ``(first, last)``.

    Single-token names become the first name; multi-token names split on the
    final token. This is deliberately naive — it is a fallback used only when a
    source supplies a full name but no components.
    """
    text = clean_text(full_name)
    if text is None:
        return None, None
    parts = text.split(" ")
    if len(parts) == 1:
        return parts[0], None
    return " ".join(parts[:-1]), parts[-1]


def join_full_name(first: str | None, last: str | None) -> str | None:
    """Compose a full name from components, tolerating either being missing."""
    parts = [part for part in (clean_text(first), clean_text(last)) if part]
    return " ".join(parts) if parts else None


def normalize_email(value: object) -> str | None:
    """Normalize an email address to lowercase, or ``None`` if unusable.

    Handles ``mailto:`` prefixes and ``Display Name <addr>`` forms, which are
    common in scraped and CSV-sourced data.
    """
    text = clean_text(value)
    if text is None:
        return None

    text = _MAILTO_RE.sub("", text).strip()

    # Prefer the address inside angle brackets when present.
    if (match := _ANGLE_EMAIL_RE.search(text)) is not None:
        text = match.group(1)
    elif "<" in text or ">" in text:
        # Looked like a display-name form but had no parsable address.
        return None

    text = text.strip().strip(".,;:").casefold()
    if not _EMAIL_RE.match(text):
        return None
    return text


def normalize_phone(value: object) -> str | None:
    """Reduce a phone number to ``+``/digits form.

    This is *not* full E.164: without a country context we cannot reliably infer
    a missing country code, so we only remove formatting and normalize the
    international prefix. See the README "Known limitations" section.
    """
    text = clean_text(value)
    if text is None:
        return None

    had_plus = text.strip().startswith("+") or text.strip().startswith("00")
    digits = _PHONE_ALLOWED_RE.sub("", text)
    digits = digits.lstrip("+")

    # "00" is the international access prefix in much of the world; treat it as "+".
    if digits.startswith("00"):
        digits = digits[2:]
        had_plus = True

    digits = digits.lstrip("0") if had_plus else digits

    # A real number needs at least 7 digits; anything shorter is noise
    # (extension fragments, "N/A" leftovers, single-digit typos).
    if len(digits) < 7:
        return None

    return f"+{digits}" if had_plus else digits


def strip_title_suffix(title: str | None) -> str | None:
    """Drop a trailing ``at <company>`` clause from a job title."""
    text = clean_text(title)
    if text is None:
        return None
    return clean_text(_TITLE_AT_RE.sub("", text))


def truncate(value: str, limit: int) -> str:
    """Truncate for log lines without splitting mid-word where avoidable."""
    if len(value) <= limit:
        return value
    return value[: max(limit - 1, 0)].rstrip() + "…"
