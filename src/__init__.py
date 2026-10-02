"""Tinasoft Phase 1 — standalone B2B lead crawler.

Package layout::

    src/
    ├── crawlers/    data-source adapters (one per provider)
    ├── models/      typed domain model
    ├── processors/  normalize -> validate -> filter -> deduplicate
    ├── exporters/   CSV / JSON / JSONL writers
    ├── utils/       dependency-free helpers (text, urls, http, logging)
    ├── config.py    environment-driven settings
    └── main.py      CLI entry point

Run with ``python -m src.main``.
"""

__version__ = "0.1.0"
