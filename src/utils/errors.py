"""Typed exception hierarchy.

Every error raised deliberately by the application derives from
:class:`LeadCrawlerError`, so callers can catch a single base class at the CLI
boundary while still being able to react to specific failure modes
(e.g. retry on :class:`SourceRateLimitError`).
"""

from __future__ import annotations


class LeadCrawlerError(Exception):
    """Base class for every deliberate error raised by this application."""


class ConfigError(LeadCrawlerError):
    """Invalid or missing configuration (environment variables, CLI flags)."""


# --------------------------------------------------------------------------- #
# Crawler / data source errors
# --------------------------------------------------------------------------- #
class CrawlerError(LeadCrawlerError):
    """A data source failed to produce leads.

    Raising this from a crawler is a *contained* failure: the pipeline records
    it against that provider and continues with the remaining sources.
    """

    def __init__(self, provider: str, message: str) -> None:
        self.provider = provider
        super().__init__(f"[{provider}] {message}")


class SourceAuthError(CrawlerError):
    """Authentication/authorization with the source failed. Not retryable."""


class SourceRateLimitError(CrawlerError):
    """The source throttled us. Retryable after ``retry_after`` seconds."""

    def __init__(self, provider: str, message: str, retry_after: float | None = None) -> None:
        self.retry_after = retry_after
        super().__init__(provider, message)


class SourceUnavailableError(CrawlerError):
    """Transient network/5xx failure that survived the retry budget."""


class SourceNotFoundError(CrawlerError):
    """The requested source or input file does not exist."""


# --------------------------------------------------------------------------- #
# Processing / output errors
# --------------------------------------------------------------------------- #
class NormalizationError(LeadCrawlerError):
    """A raw record could not be mapped onto the standardized schema."""


class ExportError(LeadCrawlerError):
    """Writing collected leads to disk failed."""
