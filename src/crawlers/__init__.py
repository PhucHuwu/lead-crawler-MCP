"""Data-source adapters.

Importing this package registers every built-in crawler with the registry, which
is what lets the CLI resolve ``--source apollo`` without importing it directly.

To add a source:

1. Create ``src/crawlers/<name>.py`` with a :class:`~src.crawlers.base.BaseCrawler`
   subclass decorated with ``@register_crawler``.
2. Import it below.

No other module needs to change.
"""

from src.crawlers.base import BaseCrawler
from src.crawlers.csv_source import CsvCrawler
from src.crawlers.mock import MockCrawler
from src.crawlers.registry import (
    available_providers,
    build_crawler,
    get_crawler_class,
    register_crawler,
    registered_crawlers,
)

# Imported for its registration side effect.
from src.crawlers.apollo import ApolloCrawler  # isort: skip
from src.crawlers.website import WebsiteCrawler  # isort: skip

__all__ = [
    "ApolloCrawler",
    "BaseCrawler",
    "CsvCrawler",
    "MockCrawler",
    "WebsiteCrawler",
    "available_providers",
    "build_crawler",
    "get_crawler_class",
    "register_crawler",
    "registered_crawlers",
]
