"""Structured logging setup.

Two output shapes are supported, selected by ``LOG_FORMAT``:

* ``console`` — compact, human-readable lines for interactive CLI runs.
* ``json``    — one JSON object per line for shipping into a log pipeline.

Both render the ``extra={...}` fields passed to the logger, so pipeline stages
can attach context (provider, counts, lead ids) without reformatting messages.

Every handler also carries a redaction filter, so a credential that reaches a
record by any route — a message, an argument, an ``extra`` field, a traceback —
is withheld rather than printed. See :mod:`src.utils.redaction`.
"""

from __future__ import annotations

import json
import logging
import sys
import traceback
from datetime import UTC, datetime
from typing import Any

from src.utils.redaction import redact

# Attributes present on every LogRecord. Anything outside this set came from
# `extra=` and is therefore application context worth emitting.
_RESERVED_RECORD_ATTRS = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "message",
        "module",
        "msecs",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)

_CONFIGURED = False


def _extras(record: logging.LogRecord) -> dict[str, Any]:
    return {
        key: value
        for key, value in record.__dict__.items()
        if key not in _RESERVED_RECORD_ATTRS and not key.startswith("_")
    }


def _render_value(value: Any) -> str:
    if isinstance(value, str):
        return value if " " not in value else json.dumps(value)
    return json.dumps(value, default=str)


class SecretRedactingFilter(logging.Filter):
    """Scrub registered credentials out of a record before it is rendered.

    Attached to the handler rather than baked into either formatter: it then
    runs once per record and covers every route a value can travel by — the
    message, its ``args``, each ``extra`` field, and the traceback — so the two
    output shapes cannot drift apart in what they protect.

    The traceback is rendered here into ``exc_text`` because a formatter builds
    it from ``exc_info`` later, by which point the filter has already run; both
    formatters prefer ``exc_text`` when it is set.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.msg)
        record.args = self._redact_args(record.args)
        for key in [key for key in record.__dict__ if key not in _RESERVED_RECORD_ATTRS]:
            if key.startswith("_"):
                continue
            setattr(record, key, redact(record.__dict__[key]))
        if record.exc_info is not None and not record.exc_text:
            record.exc_text = redact(_render_exception(record.exc_info))
        return True

    @staticmethod
    def _redact_args(args: object) -> object:
        """Redact ``args``, which logging accepts as a tuple or a mapping."""
        if isinstance(args, dict):
            return {key: redact(value) for key, value in args.items()}
        if isinstance(args, tuple):
            return tuple(redact(value) for value in args)
        return redact(args)


def _render_exception(exc_info: Any) -> str:
    """Render ``exc_info`` the way :mod:`logging` would, for the filter to scrub."""
    kind, value, tb = exc_info
    return "".join(traceback.format_exception(kind, value, tb))


class ConsoleFormatter(logging.Formatter):
    """``15:04:05 INFO     processors.pipeline  message  key=value``"""

    def format(self, record: logging.LogRecord) -> str:
        timestamp = datetime.fromtimestamp(record.created, tz=UTC).astimezone().strftime("%H:%M:%S")
        line = f"{timestamp} {record.levelname:<8} {record.name}  {record.getMessage()}"
        if extras := _extras(record):
            line += "  " + " ".join(
                f"{key}={_render_value(value)}" for key, value in extras.items()
            )
        if record.exc_info and record.exc_text is None:
            line += "\n" + self.formatException(record.exc_info)
        elif record.exc_text:
            line += "\n" + str(record.exc_text)
        return line


class JsonFormatter(logging.Formatter):
    """One JSON object per line, including exception tracebacks as a field."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        payload.update(_extras(record))
        if record.exc_info and record.exc_text is None:
            payload["exception"] = self.formatException(record.exc_info)
        elif record.exc_text:
            payload["exception"] = record.exc_text
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO", fmt: str = "console", *, stream: Any = None) -> None:
    """Install a single stderr handler on the root logger.

    Idempotent: calling it twice replaces the previous handler rather than
    stacking duplicates, which keeps CLI re-entry and tests predictable.
    """
    global _CONFIGURED

    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(JsonFormatter() if fmt.casefold() == "json" else ConsoleFormatter())
    # On the handler, so it applies to whichever formatter was chosen above and
    # to every record that reaches it — see :class:`SecretRedactingFilter`.
    handler.addFilter(SecretRedactingFilter())

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(logging.getLevelName(level.upper()))

    # httpx is chatty at INFO and its per-request lines duplicate our own
    # request logging; only surface it when the caller explicitly wants DEBUG.
    logging.getLogger("httpx").setLevel(
        logging.DEBUG if root.level <= logging.DEBUG else logging.WARNING
    )
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    # asyncio's DEBUG records ("Using selector: ...") describe its own setup and
    # say nothing about the crawl, so they stay hidden even under --verbose.
    logging.getLogger("asyncio").setLevel(logging.WARNING)

    _CONFIGURED = True


def is_configured() -> bool:
    """True once :func:`configure_logging` has run in this process."""
    return _CONFIGURED


def get_logger(name: str) -> logging.Logger:
    """Return a module logger, configuring a sane default if the app has not."""
    if not _CONFIGURED:
        configure_logging()
    return logging.getLogger(name)
