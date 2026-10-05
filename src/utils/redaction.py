"""Secret redaction for log output.

Credentials are declared as ``SecretStr`` so pydantic keeps them out of ``repr``
and out of serialized settings. That covers the *declared* ways a secret could
escape; it does not cover a secret that reaches a log line by some other route —
an adapter that logs a whole request, a library that traces headers, an error
message built from a URL that carries a token in its query string. Those are the
paths that turn a live key into a support ticket.

This module is the safety net rather than the primary defence. The primary
defence is unchanged: a credential is injected in exactly one place
(``ApolloCrawler.__init__``) and never logged. But "never" is a property a later
edit can quietly break, so :func:`register_secrets` records the values this
process actually holds and :func:`redact` removes them from every rendered log
record — see :func:`~src.utils.logging.configure_logging`, which installs the
filter.

Nothing here is specific to Apollo, or to any one credential: the values come
from walking the settings model for ``SecretStr`` fields, so a secret added to
any section is covered the moment it is declared.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator

from pydantic import BaseModel, SecretStr

#: What a secret is replaced with. Deliberately not empty: an operator reading
#: the log needs to see that a value *was* there and was withheld, otherwise a
#: redaction is indistinguishable from a field that was never populated.
REDACTED = "***"

#: Shortest value worth redacting. A one- or two-character "secret" would match
#: half the log and destroy the record it is meant to protect, so short values
#: are left alone — a real API key is far longer than this.
MIN_SECRET_LENGTH = 8

_secrets: set[str] = set()


def register_secrets(values: Iterable[str | None]) -> None:
    """Remember values that must never appear in a log record.

    Idempotent, so calling it on every settings load is harmless. Blank values
    and values shorter than :data:`MIN_SECRET_LENGTH` are ignored.

    Args:
        values: Plaintext secrets. ``None`` entries are skipped so a caller can
            pass an optional credential without guarding it first.
    """
    for value in values:
        if value and len(value) >= MIN_SECRET_LENGTH:
            _secrets.add(value)


def redact(value: object) -> object:
    """Replace every registered secret inside ``value`` with :data:`REDACTED`.

    Only strings are rewritten: a secret can only travel as text, and rendering
    any other type is the formatter's job. Everything else is returned
    unchanged, which makes this safe to apply indiscriminately to a log record's
    message, its arguments and every ``extra`` field.

    Longer secrets are replaced first, so a secret that contains another secret
    is not left partially rewritten.
    """
    if not isinstance(value, str) or not _secrets:
        return value
    for secret in sorted(_secrets, key=len, reverse=True):
        if secret in value:
            value = value.replace(secret, REDACTED)
    return value


def registered_secret_count() -> int:
    """How many secrets are currently registered. Diagnostics only."""
    return len(_secrets)


def clear_secrets() -> None:
    """Forget every registered secret.

    Test-only: the registry is process-wide, so a test that registers a fake key
    must clear it rather than leaving it to redact unrelated output later.
    """
    _secrets.clear()


def iter_secret_values(model: object) -> Iterator[str]:
    """Yield every ``SecretStr`` value found anywhere inside a pydantic model.

    Walks the model's validated fields recursively so a secret declared in any
    settings section is found without a hand-maintained list — the kind of list
    that goes stale silently, at the cost of a credential in a log file.
    """
    if isinstance(model, SecretStr):
        yield model.get_secret_value()
    elif isinstance(model, BaseModel):
        for value in model.__dict__.values():
            yield from iter_secret_values(value)
    elif isinstance(model, dict):
        for value in model.values():
            yield from iter_secret_values(value)
    elif isinstance(model, (list, tuple, set, frozenset)):
        for value in model:
            yield from iter_secret_values(value)
