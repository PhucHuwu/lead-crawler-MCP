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


class AuthExpiredError(SourceAuthError):
    """A saved browser session is no longer signed in.

    A subclass of :class:`SourceAuthError` so it inherits the "not retryable"
    contract: retrying a navigation that landed on a login wall can only ever
    land on the login wall again. It is a distinct type because it is the one
    auth failure the operator can fix without touching configuration — the
    remedy is to sign in again, and the message says so.
    """


class SourceNotFoundError(CrawlerError):
    """The requested source or input file does not exist."""


class SelectorNotFoundError(CrawlerError):
    """A selector the adapter requires did not appear.

    Distinct from a merely absent *field*: every adapter is expected to record a
    lead with a field it could not read rather than fail. This is raised only for
    a selector without which no records can be produced at all — the results
    container, the next-page control — which means the page structure has
    changed and continuing would yield nothing.
    """


# --------------------------------------------------------------------------- #
# Processing / output errors
# --------------------------------------------------------------------------- #
class NormalizationError(LeadCrawlerError):
    """A raw record could not be mapped onto the standardized schema."""


class ExportError(LeadCrawlerError):
    """Writing collected leads to disk failed."""


class SecretLeakError(LeadCrawlerError):
    """Scrubbing a diagnostic artifact left a credential-looking value in it.

    Raised by :func:`src.browser.redact.assert_no_secrets` *before* the artifact
    is written, so the failure mode is "the file was withheld", not "the file
    was written and then noticed". Browser diagnostics quote page content, and
    page content can carry a session token; refusing to write is the only safe
    response to a scrubber that did not finish the job.
    """
