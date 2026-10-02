# Lead Crawler

**Tinasoft — Phase 1.** A standalone command-line application that collects B2B
lead records from one or more sources, normalizes and validates them, applies
opt-in qualification filters, collapses duplicates, and exports a standardized
lead set as CSV / JSON / JSONL.

Phase 1 deliberately does one thing: turn messy source records into a clean,
de-duplicated, exportable lead file. Everything else — scheduling, outreach, CRM
sync — is out of scope and lives in later phases.

```text
Data source → Crawler adapter → RawLead → Normalize → Validate → Filter → Deduplicate → StandardizedLead → CSV/JSON/JSONL
```

---

## Quick start

Requires **Python 3.11+**. The project was developed against CPython 3.13 and a
`uv.lock` is committed.

```bash
# With uv (what the lockfile is for):
uv sync --extra dev
source .venv/bin/activate

# …or with plain pip:
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
```

`uv sync` on its own installs only the runtime dependencies — the test and lint
tools live in the `dev` extra, so `--extra dev` is not optional if you intend to
run the suite.

Every command below works as either `python -m src.main …` (from the repo root)
or `lead-crawler …` (the installed console script).

```bash
# Demo run against the built-in deterministic generator.
python -m src.main --source mock --limit 50

# Real run against a CSV export.
python -m src.main --source csv --csv-path samples/leads_sample.csv --format csv,jsonl

# Live source (needs a key — see Environment variables).
export LEAD_APOLLO__API_KEY=...
lead-crawler --source apollo --limit 100
```

Output lands in `data/exports/` as `<prefix>_<UTC timestamp>.<ext>` plus a
`_report.json` run report. Logs go to **stderr**, the run summary to **stdout**,
so `--log-format json` composes cleanly with a shell pipeline.

---

## Architecture

```text
src/
├── crawlers/      one module per data source
│   ├── base.py        BaseCrawler ABC + the adapter contract
│   ├── registry.py    slug → class registry, build_crawler()
│   ├── apollo.py      Apollo.io people search (paginated, retried)
│   ├── csv_source.py  local CSV/TSV with header aliasing and sniffing
│   └── mock.py        deterministic synthetic leads for demos and CI
├── models/
│   ├── person.py      Person   (name, title, seniority, email, phone, linkedin)
│   ├── company.py     Company  (name, domain, industry, headcount, geo)
│   ├── lead.py        RawLead → StandardizedLead, plus LeadSource provenance
│   ├── results.py     CrawlResult, CrawlStats, RejectedLead
│   └── enums.py       SeniorityLevel, DedupStrategy, RejectionReason, …
├── processors/
│   ├── normalizer.py   raw record → canonical types (phones, URLs, countries…)
│   ├── validator.py    drops records with no usable identity
│   ├── filters.py      the opt-in qualification rules
│   ├── deduplicator.py identity-ladder matching + field-level merging
│   ├── pipeline.py     wires the stages, isolates source failures, counts
│   ├── seniority.py    job title → SeniorityLevel
│   ├── geo.py          country / region canonicalization
│   └── …
├── exporters/     CsvExporter / JsonExporter / JsonLinesExporter + run report
├── utils/         text, urls, numbers, http (retrying client), logging, io
├── config.py      pydantic-settings tree, environment-driven
└── main.py        CLI: flags → settings → pipeline → exporters → exit code
```

### The five design decisions that matter

**1. One interface, many sources.** Every adapter implements the same contract:

```python
class BaseCrawler(ABC):
    provider: ClassVar[str]  # the slug used by --source
    display_name: ClassVar[str]
    requires_credentials: ClassVar[bool] = False

    @abstractmethod
    async def crawl(self, limit: int) -> list[RawLead]: ...
    async def aclose(self) -> None: ...
    def is_available(self) -> tuple[bool, str]: ...  # (ok, why-not)
```

The pipeline only ever sees `BaseCrawler`, so **adding a source touches no
processing code** — one new module plus one `@register_crawler` decorator. That
is enforced by a test (`test_a_new_source_needs_only_a_registration`) rather than
by convention.

**2. Configuration is data, not code.** Every tunable lives in `src/config.py`
and is read from the environment with a `LEAD_` prefix. No credential is ever
hardcoded; the Apollo key is a `SecretStr` that cannot be logged or serialized by
accident. CLI flags override individual values for one run, and an *omitted* flag
never clobbers an environment value (`--flag` / `--no-flag` pairs default to
`None`).

**3. Failures are contained and attributed.** Sources are crawled concurrently.
A source that raises — even an untyped `RuntimeError` from a third-party library
— is recorded in `stats.source_errors` and the run continues with the survivors.
Every dropped lead is attributed to the stage that dropped it
(`validation_failed` / `filtered_out` / `duplicate`) with a reason, so "why is
this lead missing?" is answerable from the run report.

**4. Determinism where it is free.** Lead IDs are SHA-256 digests of the
identity keys, so the same input yields the same IDs across runs. That is what
makes a later phase able to diff two runs and see what changed. The `mock`
source is seeded for the same reason.

**5. Atomic writes.** Exports go to a sibling temp file and are renamed into
place, so a crash or a full disk never leaves a half-written file that looks
valid.

---

## CLI usage

Run `python -m src.main --help` for the full surface. The common cases:

```bash
# Built-in demo data, default formats (csv + json) into data/exports/.
python -m src.main --source mock --limit 100

# CSV in, CSV out, output elsewhere.
python -m src.main -s csv --csv-path leads.csv -o out/ -f csv

# Several sources in one run; results are merged and de-duplicated across them.
python -m src.main -s csv,mock --csv-path leads.csv --limit 200

# Narrow the result set: US software companies, 50+ staff, contactable, no gmail.
python -m src.main -s csv --csv-path leads.csv \
  --countries US --industries Software --min-employees 50 \
  --require-email --exclude-free-email

# Senior decision makers only, most complete first, capped.
python -m src.main -s apollo --limit 500 --seniority c_suite,vp,director --max-leads 100

# Inspect without writing anything.
python -m src.main -s csv --csv-path leads.csv --dry-run

# Machine-readable logs for a pipeline.
python -m src.main -s apollo --log-format json --log-level DEBUG
```

**Exit codes** — for a scheduler or a shell pipeline to branch on:

| Code | Meaning |
| ---- | ------- |
| 0 | Run completed (the result set may still be empty) |
| 1 | Unexpected internal failure |
| 2 | Configuration error (bad flag, unknown source, missing credentials) |
| 3 | Every requested source failed |
| 4 | `--fail-on-empty` was set and no leads were produced |

Discoverability: `--list-sources` and `--list-formats`.

---

## Environment variables

All optional; the defaults are production-sane. Copy `.env.example` to `.env` to
start. Nested settings use a double underscore (`LEAD_<SECTION>__<FIELD>`), and
list-valued settings accept either `US,CA` or `["US","CA"]`.

### Runtime and output

| Variable | Default | Purpose |
| -------- | ------- | ------- |
| `LEAD_LOG_LEVEL` | `INFO` | `DEBUG`…`CRITICAL` |
| `LEAD_LOG_FORMAT` | `console` | `console` or `json` |
| `LEAD_MAX_CONCURRENCY` | `4` | Parallel sources per run |
| `LEAD_DEFAULT_SOURCES` | `mock` | Sources used when `--source` is omitted |
| `LEAD_DEFAULT_LIMIT` | `100` | Raw leads requested per source |
| `LEAD_OUTPUT_DIR` | `data/exports` | Where exports are written |
| `LEAD_OUTPUT_FORMATS` | `csv,json` | `csv`, `json`, `jsonl` |
| `LEAD_OUTPUT_PREFIX` | `leads` | Filename prefix |
| `LEAD_OUTPUT_CSV_DELIMITER` | `,` | `;` for European locales |
| `LEAD_OUTPUT_ENCODING` | `utf-8-sig` | BOM included so Excel detects UTF-8 |
| `LEAD_WRITE_RUN_REPORT` | `true` | Write the `_report.json` beside the leads |
| `LEAD_MAX_RECORDED_REJECTIONS` | `1000` | Sample size; counters stay exact |

### Processing

| Variable | Default | Purpose |
| -------- | ------- | ------- |
| `LEAD_DEDUP_STRATEGY` | `identity` | `none`, `email`, `identity`, `aggressive` |
| `LEAD_DEDUP_MERGE_FIELDS` | `true` | Fill gaps on the kept lead from the duplicate |
| `LEAD_MIN_COMPLETENESS` | `0.0` | Drop leads scoring below this (0 disables) |
| `LEAD_SORT_BY_COMPLETENESS` | `true` | Emit best-first before capping |
| `LEAD_HTTP_TIMEOUT` | `30.0` | Per-request timeout, seconds |
| `LEAD_HTTP_MAX_ATTEMPTS` | `3` | Retries for retryable failures |
| `LEAD_HTTP_INITIAL_BACKOFF` | `0.5` | First backoff, seconds |
| `LEAD_HTTP_MAX_BACKOFF` | `20.0` | Backoff ceiling, seconds |

### Filters (`LEAD_FILTERS__*`)

Every rule is opt-in; with defaults the pipeline keeps everything that validates.

| Variable | Default | Purpose |
| -------- | ------- | ------- |
| `LEAD_FILTERS__REQUIRE_EMAIL` | `false` | Keep only leads with an email |
| `LEAD_FILTERS__REQUIRE_LINKEDIN` | `false` | Keep only leads with a LinkedIn URL |
| `LEAD_FILTERS__REQUIRE_COMPANY_DOMAIN` | `false` | Keep only leads with a company domain |
| `LEAD_FILTERS__EXCLUDE_FREE_EMAIL` | `false` | Drop gmail/outlook/etc. |
| `LEAD_FILTERS__EXCLUDE_ROLE_BASED_EMAIL` | `false` | Drop `info@`, `sales@`, `noreply@`, … |
| `LEAD_FILTERS__INCLUDE_COUNTRIES` | *(empty)* | Keep only these countries (ISO or name) |
| `LEAD_FILTERS__EXCLUDE_COUNTRIES` | *(empty)* | Drop these countries |
| `LEAD_FILTERS__INCLUDE_INDUSTRIES` | *(empty)* | Keep only these industries |
| `LEAD_FILTERS__EXCLUDE_INDUSTRIES` | *(empty)* | Drop these industries |
| `LEAD_FILTERS__MIN_EMPLOYEES` / `MAX_EMPLOYEES` | *(none)* | Company headcount bounds |
| `LEAD_FILTERS__EXCLUDE_DOMAINS` | *(empty)* | Competitors / existing customers |
| `LEAD_FILTERS__INCLUDE_SENIORITY` | *(empty)* | `c_suite,vp,director,manager,senior,entry` |
| `LEAD_FILTERS__EXCLUDE_SENIORITY` | *(empty)* | Inverse of the above |
| `LEAD_FILTERS__INCLUDE_TITLE_KEYWORDS` | *(empty)* | Title must contain one |
| `LEAD_FILTERS__EXCLUDE_TITLE_KEYWORDS` | *(empty)* | Title must contain none |
| `LEAD_FILTERS__EXCLUDE_EMAIL_DOMAINS` | *(empty)* | Drop these email domains |
| `LEAD_FILTERS__ROLE_BASED_EMAIL_PREFIXES` | ~30 common inboxes | Override the shared-inbox list |

### Sources

| Variable | Default | Purpose |
| -------- | ------- | ------- |
| `LEAD_APOLLO__API_KEY` | *(none)* | **Required** for `--source apollo` |
| `LEAD_APOLLO__BASE_URL` | `https://api.apollo.io/api/v1` | Override for a proxy |
| `LEAD_APOLLO__PER_PAGE` | `25` | Results per request (max 100) |
| `LEAD_APOLLO__PERSON_TITLES` | *(empty)* | Search filter, e.g. `CTO,VP Engineering` |
| `LEAD_APOLLO__PERSON_LOCATIONS` | *(empty)* | e.g. `Vietnam,Singapore` |
| `LEAD_APOLLO__ORGANIZATION_LOCATIONS` | *(empty)* | HQ locations |
| `LEAD_APOLLO__Q_KEYWORDS` | *(none)* | Free-text keyword search |
| `LEAD_CSV_SOURCE__PATH` | *(none)* | **Required** for `--source csv` |
| `LEAD_CSV_SOURCE__DELIMITER` | `,` | Or `auto` to sniff |
| `LEAD_CSV_SOURCE__ENCODING` | `utf-8-sig` | Raise a clear error on mismatch |
| `LEAD_CSV_SOURCE__COLUMN_MAP` | *(empty)* | Header overrides, `target=Column` |
| `LEAD_MOCK__SEED` | `1337` | Fix for reproducible output |
| `LEAD_MOCK__MESSY_RATIO` | `0.25` | Fraction deliberately left dirty |
| `LEAD_MOCK__DUPLICATE_RATIO` | `0.15` | Fraction given duplicate identities |

---

## Adding a data source

The whole point of the adapter layer: a new source is **one module plus one
decorator**, with no edit to the pipeline, the models, or the CLI.

```python
# src/crawlers/mysource.py
from src.crawlers.base import BaseCrawler
from src.crawlers.registry import register_crawler
from src.models.lead import RawLead


@register_crawler
class MySourceCrawler(BaseCrawler):
    provider = "mysource"
    display_name = "My Source"
    description = "What this source is, in one line."
    requires_credentials = True

    async def crawl(self, limit: int) -> list[RawLead]:
        # Return at most `limit` records. Leave values un-normalized — cleaning
        # up is the pipeline's job, not the adapter's.
        return [RawLead(provider=self.provider, first_name=..., ...)]
```

Import the module in `src/crawlers/__init__.py` and it appears in
`--list-sources` automatically. Return raw values; the normalizer, validator,
filters and deduplicator apply unchanged.

---

## Tests

```bash
pytest                      # 451 tests
pytest tests/test_cli.py -v # one module
pytest -k dedup             # by name

ruff check . && ruff format --check .
mypy src tests              # strict
```

The suite runs entirely offline. HTTP behaviour is exercised through `respx`,
which defaults to `assert_all_mocked=True` — a request no test has routed raises
rather than silently reaching the network. There are no live API calls and no
credentials needed to run the tests.

Coverage is organized by the property each module is responsible for rather than
by line count: registry extensibility, normalizer/validator edge cases, dedup
identity ladders, exporter header/format integrity, per-source failure isolation,
stage accounting, and the CLI exit-code contract.

---

## Known limitations

Phase 1 is deliberately narrow. What it does **not** do:

- **No Apollo writes, no enrichment.** Only the people-search read path is
  implemented; the key must have access to it.
- **No pagination beyond 20 pages** per source per run (`MAX_PAGES` in
  `src/crawlers/apollo.py`). At the default `per_page=25` that is 500 records; up
  to 2,000 with `LEAD_APOLLO__PER_PAGE=100`. A larger pull needs either a
  narrower search or a later phase with query partitioning.
- **`aggressive` dedup can over-merge.** It matches on name + company domain,
  which collapses two genuinely different people who share a name at one
  employer. `identity` (the default) is the conservative choice.
- **Seniority inference is keyword-based**, not a classifier. Unusual titles fall
  back to `UNKNOWN`; that is a safe default, but a title like "Growth Hacker"
  will not be classified.
- **Phone normalization is E.164-shaped, not a full libphonenumber port.** It
  handles country codes and the common punctuation real exports contain, but a
  number without a country code and without `LEAD`-level context stays ambiguous.
- **CSV only for local files** — no XLSX, no Google Sheets.
- **Everything is single-process and in-memory.** Fine at tens of thousands of
  leads; there is no streaming path for millions.
- **No persistence.** Each run is independent; there is no state between runs, by
  design (scheduling belongs to a later phase).

---

## Recommended next steps

Natural follow-ons, roughly in dependency order:

1. **More sources** — LinkedIn Sales Navigator exports, Crunchbase, Hunter,
   Clearbit, or a generic "any JSON API" adapter. Each is one module.
2. **A persistence layer** — write runs into SQLite/Postgres keyed by the
   deterministic `lead_id`, so runs can be diffed (new / changed / gone) instead
   of overwritten.
3. **XLSX export** — the CSV exporter already centralizes the column list; an
   openpyxl writer is a small addition.
4. **A second-pass enrichment stage** — a processor that takes an already
   standardized lead set and fills gaps from a second provider.
5. **Scheduling** — only once the run and its outputs are persisted, so a
   scheduled run has somewhere to record what it did.
