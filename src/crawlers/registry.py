"""Crawler plugin registry.

Sources self-register with the :func:`register_crawler` decorator, so the CLI and
the pipeline can resolve a source by name without importing it explicitly. This
is the extension point that keeps new sources out of the core.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from src.crawlers.base import BaseCrawler, BrowserCrawler
from src.utils.errors import ConfigError, SourceNotFoundError

if TYPE_CHECKING:
    from src.config import Settings

_REGISTRY: dict[str, type[BaseCrawler]] = {}


def _normalize(name: str) -> str:
    return name.strip().casefold()


def register_crawler(cls: type[BaseCrawler]) -> type[BaseCrawler]:
    """Class decorator that adds a crawler to the registry.

    Raises:
        ConfigError: if the class does not declare a ``provider`` slug or that
            slug is already taken. Both are programming errors worth failing
            loudly at import time rather than at run time.
    """
    slug = _normalize(cls.provider)
    if not slug:
        raise ConfigError(f"{cls.__name__} must declare a non-empty `provider` slug")
    if slug in _REGISTRY and _REGISTRY[slug] is not cls:
        existing = _REGISTRY[slug].__name__
        raise ConfigError(f"provider {slug!r} is already registered by {existing}")
    _REGISTRY[slug] = cls
    return cls


def get_crawler_class(name: str) -> type[BaseCrawler]:
    """Look up a crawler class by provider slug.

    Raises:
        SourceNotFoundError: if no crawler is registered under that name.
    """
    slug = _normalize(name)
    if slug not in _REGISTRY:
        known = ", ".join(available_providers()) or "<none>"
        raise SourceNotFoundError(slug, f"unknown source {name!r}; available sources: {known}")
    return _REGISTRY[slug]


def build_crawler(
    name: str, settings: Settings, *, browser: Any = None
) -> BaseCrawler:
    """Instantiate the crawler registered under ``name``.

    ``browser`` optionally injects a :class:`~src.browser.session.BrowserManager`.
    It exists so a test can supply a fake at the seam rather than reaching into
    module globals, and it is a keyword-only argument with a ``None`` default so
    the contract every existing source relies on is unchanged.

    Raises:
        SourceNotFoundError: if the source is unknown.
        ConfigError: if the source needs credentials that are not configured.
    """
    cls = get_crawler_class(name)
    accepts_browser = issubclass(cls, BrowserCrawler)
    crawler = cls(settings, browser=browser) if accepts_browser else cls(settings)
    ok, reason = crawler.is_available()
    if not ok:
        raise ConfigError(f"source {crawler.provider!r} is not usable: {reason}")
    return crawler


def available_providers() -> list[str]:
    """Registered provider slugs, sorted for stable CLI output."""
    return sorted(_REGISTRY)


def registered_crawlers() -> dict[str, type[BaseCrawler]]:
    """Snapshot of the registry, for introspection and tests."""
    return dict(_REGISTRY)


def clear_registry() -> None:
    """Empty the registry. Test-only helper."""
    _REGISTRY.clear()
