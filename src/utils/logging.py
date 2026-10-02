"""Structured logging setup.

Two output shapes are supported, selected by ``LOG_FORMAT``:

* ``console`` — compact, human-readable lines for interactive CLI runs.
* ``json``    — one JSON object per line for shipping into a log pipeline.

Both render the ``extra={...}` fields passed to the logger, so pipeline stages
can attach context (provider, counts, lead ids) without reformatting messages.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

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


class ConsoleFormatter(logging.Formatter):
    """``15:04:05 INFO     processors.pipeline  message  key=value``"""

    def format(self, record: logging.LogRecord) -> str:
        timestamp = datetime.fromtimestamp(record.created, tz=UTC).astimezone().strftime("%H:%M:%S")
        line = f"{timestamp} {record.levelname:<8} {record.name}  {record.getMessage()}"
        if extras := _extras(record):
            line += "  " + " ".join(
                f"{key}={_render_value(value)}" for key, value in extras.items()
            )
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
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
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO", fmt: str = "console", *, stream: Any = None) -> None:
    """Install a single stderr handler on the root logger.

    Idempotent: calling it twice replaces the previous handler rather than
    stacking duplicates, which keeps CLI re-entry and tests predictable.
    """
    global _CONFIGURED

    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(JsonFormatter() if fmt.casefold() == "json" else ConsoleFormatter())

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
