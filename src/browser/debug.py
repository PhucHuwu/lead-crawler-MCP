"""Diagnostic artifacts for browser runs.

When a selector stops matching, the only useful question is "what was actually on
the page?", and the only useful answer is the page. This module writes that
answer to disk — bounded, and scrubbed.

Three properties matter more than the layout:

**Bounded.** A crawl can visit hundreds of pages and any one of them can be
enormous, so there is a per-file cap, a per-run byte cap, a per-run file cap, and
a retention count that prunes old runs. Exceeding a cap stops payload writes and
records why in ``manifest.json`` — a diagnostic system that fills the disk is a
worse failure than the one it was diagnosing.

**Scrubbed.** These files quote page content from inside a signed-in session, so
they are the highest-risk artifact the project produces. They go through
:mod:`src.browser.redact`, and the assertion there runs *before* the write: a
payload that still carries a credential is withheld, never written and flagged
afterwards. See that module for the layering.

**Private.** Directories are ``0700`` and files ``0600``. :func:`~src.utils.io.atomic_write`
publishes at ``0644`` by default because exports are meant to be read by
colleagues; these are not, so the mode is passed explicitly.
"""

from __future__ import annotations

import json
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.browser.base import BrowserPage
from src.browser.redact import assert_no_secrets, redact_url, scrub_html
from src.utils.errors import SecretLeakError
from src.utils.io import atomic_write_bytes, atomic_write_text, ensure_directory
from src.utils.logging import get_logger

#: Directory names this module is willing to delete. A run directory is a
#: timestamp plus eight hex characters; anything else is left alone. This guard
#: is not decoration — a pruning bug that walked into ``output/`` or ``data/``
#: would destroy the run's actual product.
RUN_ID_RE = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{8}$")

#: Largest HTML artifact written for one page. Beyond this the payload stops
#: being a diagnostic and starts being a file transfer.
DEFAULT_PER_FILE_BYTES = 2 * 1024 * 1024

#: Placeholder written in place of a payload the scrubber could not clear.
_WITHHELD = (
    "<!-- withheld: scrubbing left credential-shaped content in this artifact.\n"
    "     The payload was not written. See the run log for the scrubber finding. -->\n"
)


def new_run_id() -> str:
    """A run identifier: sortable timestamp plus a random suffix.

    The suffix is not paranoia. Two runs started in the same second — which the
    test suite does constantly, and a scheduled invocation could do — would
    otherwise share a directory and interleave their artifacts.
    """
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{secrets.token_hex(4)}"


@dataclass
class RunCounters:
    """Browser counters for one source.

    Kept beside the diagnostics because that is where every one of them is
    already observed: a page is visited by a navigation, and the three failure
    counts are exactly the kinds of error the recorder already distinguishes.
    Counting them a second time somewhere else would be a second source of truth.
    """

    pages_visited: int = 0
    browser_errors: int = 0
    selector_failures: int = 0
    auth_failures: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "pages_visited": self.pages_visited,
            "browser_errors": self.browser_errors,
            "selector_failures": self.selector_failures,
            "auth_failures": self.auth_failures,
        }

    def merge(self, other: RunCounters) -> None:
        self.pages_visited += other.pages_visited
        self.browser_errors += other.browser_errors
        self.selector_failures += other.selector_failures
        self.auth_failures += other.auth_failures


#: Which counter an error kind increments. Kinds not listed here are recorded but
#: not counted — a page error is already counted by the navigation that produced
#: it, and counting it twice would overstate the failure rate.
_COUNTER_BY_KIND = {
    "selector_timeout": "selector_failures",
    "selector_missing": "selector_failures",
    "navigation_failed": "browser_errors",
    "auth_expired": "auth_failures",
}


def prune_debug_dirs(root: Path, *, keep: int) -> list[Path]:
    """Delete all but the newest ``keep`` run directories under ``root``.

    Returns the directories removed. Only direct children whose name matches
    :data:`RUN_ID_RE` are candidates, so a stray ``.gitkeep`` or a file the
    operator put there by hand survives.
    """
    if keep < 0 or not root.is_dir():
        return []
    candidates = sorted(
        (child for child in root.iterdir() if child.is_dir() and RUN_ID_RE.match(child.name)),
        key=lambda path: path.name,
    )
    removed: list[Path] = []
    for path in candidates[: max(len(candidates) - keep, 0)]:
        try:
            for artifact in sorted(path.rglob("*"), reverse=True):
                if artifact.is_file():
                    artifact.unlink(missing_ok=True)
                elif artifact.is_dir():
                    artifact.rmdir()
            path.rmdir()
        except OSError as exc:
            get_logger("browser.debug").warning(
                "could not prune debug directory", extra={"path": str(path), "error": str(exc)}
            )
            continue
        removed.append(path)
    return removed


class DebugRecorder:
    """Writes bounded, scrubbed diagnostics for one run.

    Shared by every source in the run so they land under one run directory, and
    so the byte and file caps apply to the run rather than to each source.
    """

    def __init__(
        self,
        root: Path,
        *,
        run_id: str | None = None,
        enabled: bool = False,
        max_bytes: int = 50 * 1024 * 1024,
        max_files: int = 200,
        per_file_bytes: int = DEFAULT_PER_FILE_BYTES,
        screenshot: bool = True,
    ) -> None:
        self.run_id = run_id or new_run_id()
        self.root = root
        self.enabled = enabled
        self.max_bytes = max_bytes
        self.max_files = max_files
        self.per_file_bytes = per_file_bytes
        self.screenshot = screenshot
        self.logger = get_logger("browser.debug")

        self._root_dir = root / self.run_id
        self._bytes = 0
        self._files = 0
        self._counters: dict[str, int] = {}
        self._errors: dict[str, list[dict[str, Any]]] = {}
        self._run_counters: dict[str, RunCounters] = {}
        self._truncated_reason: str | None = None

    # ------------------------------------------------------------------ #
    # Counters
    # ------------------------------------------------------------------ #
    def counters(self, provider: str) -> RunCounters:
        """The mutable counter block for ``provider``, created on first use."""
        return self._run_counters.setdefault(provider, RunCounters())

    def all_counters(self) -> dict[str, RunCounters]:
        return dict(self._run_counters)

    # ------------------------------------------------------------------ #
    # Paths
    # ------------------------------------------------------------------ #
    @property
    def dir(self) -> Path:
        """The run directory. Created on first use, not at construction."""
        return self._root_dir

    def provider_dir(self, provider: str) -> Path:
        return self._root_dir / provider

    def _next_index(self, provider: str) -> int:
        index = self._counters.get(provider, 0) + 1
        self._counters[provider] = index
        return index

    # ------------------------------------------------------------------ #
    # Recording
    # ------------------------------------------------------------------ #
    async def record_page(
        self,
        page: BrowserPage,
        *,
        provider: str,
        label: str,
        kind: str = "page_error",
        error: str | None = None,
        force: bool = False,
    ) -> Path | None:
        """Capture the current page as HTML, URL and screenshot.

        ``force`` writes even when the recorder is disabled. An authentication
        failure uses it: the operator needs that evidence regardless of whether
        diagnostics were switched on, and it is the one page whose content
        explains the failure.

        Returns the HTML artifact's path, or ``None`` if nothing was written.
        """
        if not (self.enabled or force):
            return None
        if (reason := self._cap_reason()) is not None:
            self._truncated_reason = reason
            return None

        provider_dir = ensure_directory(self.provider_dir(provider), mode=0o700)
        index = self._next_index(provider)
        stem = f"{index:04d}-{_slug(label)}"

        html_path = await self._write_html(
            page, provider=provider, path=provider_dir / f"{stem}.html"
        )
        self._write_text(provider_dir / f"{stem}.url", redact_url(page.url) + "\n")
        if self.screenshot:
            await self._write_screenshot(page, path=provider_dir / f"{stem}.png")
        if error:
            self.record_error(
                provider=provider,
                label=label,
                kind=kind,
                message=error,
                url=page.url,
            )
        return html_path

    async def _write_html(self, page: BrowserPage, *, provider: str, path: Path) -> Path | None:
        try:
            raw = await page.content()
        except Exception as exc:
            self.logger.warning(
                "could not read page HTML for diagnostics",
                extra={"provider": provider, "error": str(exc)},
            )
            return None
        scrubbed = scrub_html(raw, max_bytes=self.per_file_bytes)
        try:
            assert_no_secrets(scrubbed, what=str(path.name))
        except SecretLeakError as exc:
            # Fail closed. The alternative — writing the payload and noting the
            # finding — leaves the credential on disk, which is the one outcome
            # this whole module exists to prevent.
            self.logger.warning(
                "withheld a diagnostic artifact that scrubbing could not clear",
                extra={"provider": provider, "artifact": path.name, "reason": str(exc)},
            )
            self._write_text(path, _WITHHELD)
            return None
        self._write_text(path, scrubbed)
        return path

    async def _write_screenshot(self, page: BrowserPage, *, path: Path) -> None:
        """Capture a viewport screenshot.

        Viewport-only by default, never full-page: a results list is an infinite
        scroller and ``full_page`` on one produces an unbounded image. A
        screenshot cannot be scrubbed after the fact, so it is kept private by
        permissions and it only ever shows a page the crawler was already
        entitled to read.
        """
        try:
            data = await page.screenshot(full_page=False)
        except Exception as exc:
            self.logger.debug("screenshot failed", extra={"error": str(exc)})
            return
        self._write_bytes(path, data)

    def record_error(
        self,
        *,
        provider: str,
        label: str,
        kind: str,
        message: str,
        url: str,
        selector: str | None = None,
        waited_ms: int | None = None,
        found_count: int | None = None,
    ) -> None:
        """Append a structured failure record.

        ``found_count`` is the field that makes a selector failure diagnosable:
        ``0`` with a timeout means the selector never matched anything, while a
        non-zero count means it matched and the adapter misread the result. Those
        are different bugs with different fixes, and a bare
        ``TimeoutError`` cannot tell them apart.

        The kind also drives the run counters (see :data:`_COUNTER_BY_KIND`), so
        a failure is counted exactly where it is first observed rather than at
        each call site.
        """
        if (field := _COUNTER_BY_KIND.get(kind)) is not None:
            counters = self.counters(provider)
            setattr(counters, field, getattr(counters, field) + 1)
        record: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(),
            "provider": provider,
            "label": label,
            "kind": kind,
            "message": str(message),
            "url": redact_url(url),
        }
        if selector is not None:
            record["selector"] = selector
        if waited_ms is not None:
            record["waited_ms"] = waited_ms
        if found_count is not None:
            record["found_count"] = found_count
        self._errors.setdefault(provider, []).append(record)

    # ------------------------------------------------------------------ #
    # Writing
    # ------------------------------------------------------------------ #
    def _cap_reason(self) -> str | None:
        if self._files >= self.max_files:
            return f"file cap reached ({self.max_files} files)"
        if self._bytes >= self.max_bytes:
            return f"size cap reached ({self.max_bytes} bytes)"
        return None

    def _write_text(self, path: Path, text: str) -> None:
        try:
            self._bytes += atomic_write_text(path, text, mode=0o600)
            self._files += 1
        except OSError as exc:
            self.logger.warning(
                "could not write diagnostic artifact",
                extra={"path": str(path), "error": str(exc)},
            )

    def _write_bytes(self, path: Path, data: bytes) -> None:
        try:
            self._bytes += atomic_write_bytes(path, data, mode=0o600)
            self._files += 1
        except OSError as exc:
            self.logger.warning(
                "could not write diagnostic artifact",
                extra={"path": str(path), "error": str(exc)},
            )

    # ------------------------------------------------------------------ #
    # Finalisation
    # ------------------------------------------------------------------ #
    def flush(self) -> None:
        """Write the error and manifest files. Safe to call more than once."""
        if not (self.enabled or self._errors):
            return
        ensure_directory(self._root_dir, mode=0o700)
        providers = sorted(set(self._errors) | set(self._counters))
        for provider in providers:
            provider_dir = ensure_directory(self.provider_dir(provider), mode=0o700)
            self._flush_errors(provider, provider_dir)
            self._write_text(
                provider_dir / "manifest.json",
                _dumps(
                    {
                        "run_id": self.run_id,
                        "provider": provider,
                        "artifacts": self._counters.get(provider, 0),
                        "errors": len(self._errors.get(provider, [])),
                    }
                ),
            )
        self._write_text(
            self._root_dir / "manifest.json",
            _dumps(
                {
                    "run_id": self.run_id,
                    "generated_at": datetime.now(UTC).isoformat(),
                    "providers": providers,
                    "artifacts": self._files,
                    "bytes": self._bytes,
                    "errors": sum(len(records) for records in self._errors.values()),
                    "truncated": self._truncated_reason is not None,
                    "truncated_reason": self._truncated_reason,
                }
            ),
        )

    def _flush_errors(self, provider: str, provider_dir: Path) -> None:
        records = self._errors.get(provider, [])
        if not records:
            return
        payload = _dumps(records)
        try:
            assert_no_secrets(payload, what=f"{provider}/errors.json")
        except SecretLeakError as exc:
            self.logger.warning(
                "withheld errors.json: scrubbing could not clear it",
                extra={"provider": provider, "reason": str(exc)},
            )
            payload = _dumps({"withheld": "credential-shaped content survived scrubbing"})
        self._write_text(provider_dir / "errors.json", payload)


def _slug(label: str) -> str:
    """Filesystem-safe label: lowercase, dashes, nothing else."""
    cleaned = re.sub(r"[^a-z0-9]+", "-", label.casefold()).strip("-")
    return cleaned[:60] or "page"


def _dumps(payload: Any) -> str:
    return json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n"
