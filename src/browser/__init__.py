"""Browser layer: the seam between the crawler and whatever drives Chromium.

Adapters import from here and nowhere else. ``playwright`` and ``cloakbrowser``
are reachable only through :mod:`src.browser.cloak`, which is what makes the
MCP conversion later a new provider rather than a rewrite of both crawlers.
"""

from src.browser.auth import AuthGuard, AuthVerdict, LoginWall, auth_error
from src.browser.base import (
    DEFAULT_TIMEOUT_MS,
    BrowserConnection,
    BrowserElement,
    BrowserPage,
    BrowserProvider,
    ProxyConfig,
    SessionSpec,
    Viewport,
)
from src.browser.debug import DebugRecorder, new_run_id, prune_debug_dirs
from src.browser.helpers import (
    attribute_of,
    count_matches,
    is_fatal_for_source,
    text_of,
    wait_for,
    wait_for_required,
)
from src.browser.redact import assert_no_secrets, redact_url, scrub_html
from src.browser.session import (
    BrowserManager,
    BrowserSession,
    NavigationOutcome,
    build_session_spec,
    get_browser_manager,
    reset_browser_manager,
)

__all__ = [
    "DEFAULT_TIMEOUT_MS",
    "AuthGuard",
    "AuthVerdict",
    "BrowserConnection",
    "BrowserElement",
    "BrowserManager",
    "BrowserPage",
    "BrowserProvider",
    "BrowserSession",
    "DebugRecorder",
    "LoginWall",
    "NavigationOutcome",
    "ProxyConfig",
    "SessionSpec",
    "Viewport",
    "assert_no_secrets",
    "attribute_of",
    "auth_error",
    "build_session_spec",
    "count_matches",
    "get_browser_manager",
    "is_fatal_for_source",
    "new_run_id",
    "prune_debug_dirs",
    "redact_url",
    "reset_browser_manager",
    "scrub_html",
    "text_of",
    "wait_for",
    "wait_for_required",
]
