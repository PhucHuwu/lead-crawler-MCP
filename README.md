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

Output lands in `data/exports/` as `<prefix>_<source>_<UTC timestamp>.<ext>` plus a
matching `_report.json` run report; `--output` overrides that with an exact path
(see [Where the output goes](#where-the-output-goes)). Logs go to **stderr**, the
run summary to **stdout**, so `--log-format json` composes cleanly with a shell
pipeline.

---

## Architecture

```text
src/
├── crawlers/      one module per data source
│   ├── base.py        BaseCrawler ABC + the adapter contract
│   ├── registry.py    slug → class registry, build_crawler()
│   ├── apollo.py      Apollo.io people search via the official API
│   ├── website.py     company-site enrichment: description, emails, socials
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
│   ├── validator.py    rejects records with no usable identity, flags the rest
│   ├── filters.py      the opt-in qualification rules
│   ├── deduplicator.py identity-ladder matching + field-level merging
│   ├── pipeline.py     wires the stages, isolates source failures, counts
│   ├── seniority.py    job title → SeniorityLevel
│   ├── geo.py          country / region canonicalization
│   └── …
├── exporters/     CsvExporter / JsonExporter / JsonLinesExporter + run report
├── utils/         text, urls, numbers, http (retrying client), logging, io
├── config.py      pydantic-settings tree, environment-driven
├── search_profiles.py  named, version-controlled search criteria (YAML)
└── main.py        CLI: flags → settings → pipeline → exporters → exit code
```

### The design decisions that matter

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
`None`). Two layers of named YAML profiles sit on top for the things that are
really campaign assets rather than per-operator knobs — *what to search for*
(`config/search_profiles.yaml`) and *which results to keep*
(`config/filters.yaml`). Neither is read unless a profile is named, so both are
inert by default.

The split is deliberate: **secrets and environment-specific values go in the
environment; crawler behaviour goes in YAML.** A credential must never be
committed, and a targeting rule must be reviewable in a diff — so the two never
share a file. `--profile NAME` ties the halves together by resolving one name
across both files at once.

The `LEAD_` prefix is enforced in both directions. Because settings are read by
prefix, `APOLLO_API_KEY=sk-live-…` would otherwise be accepted and silently
ignored — indistinguishable from having no key at all, which is the worst
possible way for a run to fail. Any name the tool recognises in a spelling it
does not read (the bare field name `LOG_LEVEL`, the single-underscore nested form
`LEAD_APOLLO_API_KEY`, the bare section form `APOLLO_API_KEY`) stops the run with
exit code 2 and names the variable to use instead.

**3. Failures are contained and attributed.** Sources are crawled concurrently.
A source that raises — even an untyped `RuntimeError` from a third-party library
— is recorded in `stats.source_errors` and the run continues with the survivors.
The same containment applies per record: an exception from normalization,
validation or filtering drops *that* lead and nothing else, so one malformed row
can never cost you the other 999. Every dropped lead is attributed to the stage
that dropped it (`normalization_failed` / `processing_failed` /
`validation_failed` / `filtered_out` / `duplicate`) with a reason, so "why is
this lead missing?" is answerable from the run report.

**4. One record per run.** When the crawl finishes, the pipeline logs a single
`run summary` record carrying the source list, start and end times, records
discovered, parsed, invalid, filtered, de-duplicated and exported, plus any
per-source errors. With `--log-format json` each of those is a top-level key, so
a log pipeline can index a whole run without parsing prose.

**5. Determinism where it is free.** Lead IDs are SHA-256 digests of the
identity keys, so the same input yields the same IDs across runs. That is what
makes a later phase able to diff two runs and see what changed. The `mock`
source is seeded for the same reason.

`stable_id()` deliberately omits `source_id` from its basis even though the dedup
ladder leads with it: a source id identifies a *record at a source*, whereas
`lead_id` identifies a *person*, and the same person found through two sources
has two different source ids. Feeding one into the other would give two ids to one
person, which is the thing the id exists to prevent. Deduplication walks the full
ladder; the id walks it minus that one key.

**6. Atomic writes.** Exports go to a sibling temp file and are renamed into
place, so a crash or a full disk never leaves a half-written file that looks
valid.

**7. Source knowledge stays inside its adapter.** Where a source has an official
API, the adapter calls the API rather than scraping the site behind it — Apollo
is read through `api.apollo.io` with a key, never through its web UI. Everything
provider-specific (Apollo's field names, its seniority vocabulary, the JSON-LD
and `<meta>` shapes of a company homepage) lives in that one module and is
translated to the shared `RawLead` at the boundary. Nothing downstream can tell
which source a lead came from except by reading `lead.source.provider`.

That cuts both ways: `config/search_profiles.yaml` stores seniorities as plain
strings rather than the internal `SeniorityLevel` enum, because that enum maps
`head` onto `DIRECTOR` — routing a profile through it would quietly search for
the wrong people. The Apollo adapter validates the strings against Apollo's own
vocabulary instead, and the rest of the application never sees them.

---

## CLI usage

Run `python -m src.main --help` for the full surface. The common cases:

```bash
# Built-in demo data, default formats (csv + json) into data/exports/.
python -m src.main --source mock --limit 100

# The full example: one source, one file, one format.
python -m src.main --source apollo --limit 100 --format json --output ./output/leads.json

# CSV in, CSV out, timestamped into a directory.
python -m src.main -s csv --csv-path leads.csv --output-dir out/ -f csv

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

# Why did this run drop leads? --verbose adds a per-record reason.
python -m src.main -s apollo --limit 500 --require-email --verbose

# Machine-readable logs for a pipeline.
python -m src.main -s apollo --log-format json --log-level DEBUG

# Apollo search criteria live in the environment …
LEAD_APOLLO__PERSON_TITLES="CTO,VP Engineering,Head of Engineering" \
LEAD_APOLLO__PERSON_SENIORITIES=c_suite,vp,head \
LEAD_APOLLO__ORGANIZATION_LOCATIONS=Singapore,Japan,Australia \
  python -m src.main -s apollo --limit 300

# … or in a profile file, which is the same thing but version-controlled.
python -m src.main -s apollo --search-profile default --limit 300
python -m src.main -s apollo --search-profile sea_fintech --limit 300
python -m src.main -s apollo --search-profile --search-profiles-path ./team.yaml

# One name for a whole acquisition strategy: --profile looks the name up in the
# search file AND the filter file, so it selects who to look for and which of
# them to keep. A name defined in only one of the two files is fine and applies
# in only one place. Separators are forgiven, so the hyphenated spelling works.
python -m src.main -s apollo --profile singapore_tech --limit 300
python -m src.main -s apollo --profile singapore-tech --limit 300

# Which results to KEEP is a separate profile, and one flag replaces the whole
# set of --countries/--min-employees/--titles flags.
python -m src.main -s apollo --search-profile default --filter-profile default
python -m src.main -s csv --csv-path leads.csv --filter-profile enterprise_na
python -m src.main -s csv --csv-path leads.csv --list-filter-profiles
python -m src.main --list-profiles        # both files, and the halves each covers

# A profile is the base; an explicit flag still overrides one rule of it — and an
# explicitly named half overrides the umbrella for that half only.
python -m src.main -s csv --csv-path leads.csv --filter-profile --min-employees 5
python -m src.main -s apollo --profile singapore_tech --filter-profile enterprise_na

# Whole-title match: "CTO" here, not "Assistant to the CTO". Paths under
# --required-fields are validated against the model, so a typo fails loudly.
python -m src.main -s csv --csv-path leads.csv \
  --titles "CTO,VP Engineering" --required-fields company.name,person.email

# Enrich companies from their own public websites — one record per site.
python -m src.main -s website --website-url acme.com --website-url beta.example
python -m src.main -s website --website-url acme.com,beta.example --format jsonl

# Apollo's people plus their employers' public pages, merged and de-duplicated.
python -m src.main -s apollo,website --search-profile default \
  --website-url acme.com --limit 200
```

### Where the output goes

`--output` and `--output-dir` are mutually exclusive, and the difference matters:

| Flag | Writes | Use it when |
| ---- | ------ | ----------- |
| `--output PATH` (`-o`) | Exactly `PATH`, plus `PATH` with `_report` appended | A downstream job knows the path it wants to read |
| `--output-dir DIR` | `<prefix>_<source>_<UTC timestamp>.<ext>` per format | You want to keep every run's output side by side |

A `--source apollo` run at 14:03:22 UTC on 2026-10-05 therefore writes:

```
data/exports/leads_apollo_2026-10-05_14-03-22.csv
data/exports/leads_apollo_2026-10-05_14-03-22.json
data/exports/leads_apollo_2026-10-05_14-03-22_report.json
```

Every format of one run shares a stem, so a file's name alone says which run and
which source produced it. The source component is the run's providers, sorted and
hyphen-joined (`leads_apollo-website_...`) so the same set of sources always
yields the same name regardless of the order they were listed; a source that
failed before returning anything is still named. `LEAD_OUTPUT_PREFIX` replaces the
leading `leads`. The timestamp is UTC and uses `-` rather than `:` in the time
because a colon is not a legal filename character on Windows.

`--output` requires a single `--format` — one path cannot hold two
serializations — and a missing parent directory is created for you. Because the
default is `csv,json`, `--output` on its own is a configuration error; pass
`--format` explicitly. With `--output` the run report lands next to your file as
`<stem>_report.json` rather than being timestamped.

### What the exports contain

**Encoding.** Every file is UTF-8. CSV additionally carries a BOM by default
(`LEAD_OUTPUT_ENCODING=utf-8-sig`) because Excel otherwise guesses the codepage
and mangles non-ASCII names; set `LEAD_OUTPUT_ENCODING=utf-8` for a consumer that
would treat the BOM as data. JSON and JSONL are UTF-8 without a BOM and are
written with `ensure_ascii=False`, so Vietnamese, CJK and other non-Latin text
appears literally rather than as `\uXXXX` escapes — the files stay greppable and
readable. Text is NFKC-normalized on the way in, so the same name arriving
composed from one source and decomposed from another lands on one string instead
of two.

**Column names.** CSV flattens the nested model into one row per lead, prefixed by
the object each field came from — `person_*`, `company_*`, `source_*` — with
`lead_id` and `completeness` unprefixed because they describe the record as a
whole. `LEAD_COLUMNS` in `src/exporters/csv_exporter.py` is the canonical order,
and a test asserts it still matches the model, so a field cannot be silently
dropped. JSON keeps the nested structure (`person` / `company` / `source`) and is
the lossless format; CSV is the spreadsheet one.

**The run report.** A JSON sidecar with a flat `summary` first —
`source`, `started_at`, `finished_at`, `discovered`, `valid`, `filtered`,
`duplicates_removed`, `exported`, `errors` — followed by the detail: the full
counter set, the deduplication breakdown, per-source counts and errors, and a
sample of which filter rule rejected what. It holds no credentials and is meant
to be attached to a ticket when a run returns fewer leads than expected.

`--verbose` (`-v`) is shorthand for `--log-level DEBUG`. It adds the effective
configuration (secrets excluded) and one `lead rejected` line per dropped
record, which is usually the fastest way to find out why a filter removed more
than you expected. It and `--log-level` are mutually exclusive.

**Exit codes** — for a scheduler or a shell pipeline to branch on:

| Code | Meaning |
| ---- | ------- |
| 0 | Run completed (the result set may still be empty) |
| 1 | Unexpected internal failure |
| 2 | Configuration error (bad flag, unknown source, missing credentials) |
| 3 | Every requested source failed |
| 4 | `--fail-on-empty` was set and no leads were produced |

Discoverability: `--list-sources`, `--list-formats`, `--list-profiles` and
`--list-filter-profiles`. Each prints what is available and exits 0 without
crawling.

---

## Environment variables

All optional; the defaults are production-sane. Copy `.env.example` to `.env` to
start. Nested settings use a double underscore (`LEAD_<SECTION>__<FIELD>`), and
list-valued settings accept either `US,CA` or `["US","CA"]`.

The prefix is not advisory. A variable this tool recognises in a spelling it does
not read stops the run with exit code 2 and names the correct variable — the
bare name (`APOLLO_API_KEY`), the single-underscore nested form
(`LEAD_APOLLO_API_KEY`) and the bare section form all count. Ignoring a
mistyped secret would be indistinguishable from having none. Names that are not
this tool's business (`AWS_PROFILE`, `HTTP_PROXY`) are left alone, and a stray
name is tolerated when the correct one is also set. Secrets belong only in the
environment; `config/*.yaml` holds behaviour and never a credential.

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
| `LEAD_OUTPUT_ENCODING` | `utf-8-sig` | UTF-8 with a BOM so Excel detects it |
| `LEAD_WRITE_RUN_REPORT` | `true` | Write the `_report.json` beside the leads |
| `LEAD_MAX_RECORDED_REJECTIONS` | `1000` | Sample size; counters stay exact |
| `LEAD_PROFILE` | *(none)* | Umbrella profile: one name, both halves below |
| `LEAD_SEARCH_PROFILE` | *(none)* | Named Apollo search profile; unset loads no file |
| `LEAD_SEARCH_PROFILES_PATH` | `config/search_profiles.yaml` | Where search profiles live |
| `LEAD_FILTER_PROFILE` | *(none)* | Named qualification profile; unset loads no file |
| `LEAD_FILTER_PROFILES_PATH` | `config/filters.yaml` | Where filter profiles live |

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
| `LEAD_FILTERS__INCLUDE_TITLES` / `EXCLUDE_TITLES` | *(empty)* | Whole-title match, case-insensitive |
| `LEAD_FILTERS__INCLUDE_TITLE_KEYWORDS` | *(empty)* | Title must contain one |
| `LEAD_FILTERS__EXCLUDE_TITLE_KEYWORDS` | *(empty)* | Title must contain none |
| `LEAD_FILTERS__REQUIRED_FIELDS` | *(empty)* | Dotted paths that must carry a value |
| `LEAD_FILTERS__EXCLUDE_EMAIL_DOMAINS` | *(empty)* | Drop these email domains |
| `LEAD_FILTERS__ROLE_BASED_EMAIL_PREFIXES` | ~30 common inboxes | Override the shared-inbox list |

### Sources

| Variable | Default | Purpose |
| -------- | ------- | ------- |
| `LEAD_APOLLO__API_KEY` | *(none)* | **Required** for `--source apollo` |
| `LEAD_APOLLO__BASE_URL` | `https://api.apollo.io/api/v1` | Override for a proxy |
| `LEAD_APOLLO__PER_PAGE` | `25` | Results per request (max 100) |
| `LEAD_APOLLO__MAX_PAGES` | `20` | Pagination safety valve (1–500) |
| `LEAD_APOLLO__PERSON_TITLES` | *(empty)* | Search filter, e.g. `CTO,VP Engineering` |
| `LEAD_APOLLO__PERSON_SENIORITIES` | *(empty)* | Apollo's vocabulary — see below |
| `LEAD_APOLLO__PERSON_LOCATIONS` | *(empty)* | e.g. `Vietnam,Singapore` |
| `LEAD_APOLLO__ORGANIZATION_LOCATIONS` | *(empty)* | HQ locations |
| `LEAD_APOLLO__ORGANIZATION_INDUSTRIES` | *(empty)* | Apollo industry tags |
| `LEAD_APOLLO__EMPLOYEE_COUNT_RANGES` | *(empty)* | Headcount bands, e.g. `11-50,201-500` |
| `LEAD_APOLLO__INCLUDE_SIMILAR_TITLES` | `true` | Let Apollo widen title matches |
| `LEAD_APOLLO__Q_KEYWORDS` | *(none)* | Free-text keyword search |
| `LEAD_SEARCH_PROFILES_PATH` | `config/search_profiles.yaml` | Profile file |
| `LEAD_SEARCH_PROFILE` | *(none)* | Profile to apply when none is named |
| `LEAD_WEBSITE__URLS` | *(empty)* | **Required** for `--source website` |
| `LEAD_WEBSITE__MAX_PAGES_PER_SITE` | `3` | Homepage included (1–10) |
| `LEAD_WEBSITE__REQUEST_DELAY` | `1.0` | Seconds between hits on one host |
| `LEAD_WEBSITE__RESPECT_ROBOTS` | `true` | Off only for a site you own |
| `LEAD_WEBSITE__SAME_HOST_ONLY` | `true` | Never follow off-site links |
| `LEAD_WEBSITE__MAX_EMAILS` | `3` | Addresses kept per site |
| `LEAD_WEBSITE__USER_AGENT` | identifying UA | Sent with every request |
| `LEAD_CSV_SOURCE__PATH` | *(none)* | **Required** for `--source csv` |
| `LEAD_CSV_SOURCE__DELIMITER` | `,` | Or `auto` to sniff |
| `LEAD_CSV_SOURCE__ENCODING` | `utf-8-sig` | Raise a clear error on mismatch |
| `LEAD_CSV_SOURCE__COLUMN_MAP` | *(empty)* | Header overrides, `target=Column` |
| `LEAD_MOCK__SEED` | `1337` | Fix for reproducible output |
| `LEAD_MOCK__MESSY_RATIO` | `0.25` | Fraction deliberately left dirty |
| `LEAD_MOCK__DUPLICATE_RATIO` | `0.15` | Fraction given duplicate identities |

`LEAD_APOLLO__PERSON_SENIORITIES` uses Apollo's own vocabulary, not the internal
`SeniorityLevel` enum: `owner`, `founder`, `c_suite`, `partner`, `vp`, `head`,
`director`, `manager`, `senior`, `entry`, `intern`. Case, spaces and hyphens are
forgiven (`"C-Suite"` → `c_suite`), a value that is not in that list is a
configuration error naming the offending value, and duplicates collapse.

`LEAD_APOLLO__EMPLOYEE_COUNT_RANGES` bands are written with a hyphen
(`11-50,201-500`), because a comma inside a comma-separated value would be read
as the next band. A JSON array may still use the real Apollo spelling,
`["201,500"]`.

### Named search profiles

Search criteria are a campaign's data, not a shell incantation, so they can live
in a YAML file next to the code. `config/search_profiles.yaml` ships four
profiles — `default`, `singapore_tech`, `sea_fintech` and `enterprise_apac`:

```yaml
default:
  titles: [CTO, VP Engineering, Head of Engineering, Founder]
  seniorities: [c_suite, vp, head, founder]
  locations: [Singapore, Japan, Australia]

sea_fintech:
  titles: [Chief Technology Officer, Head of Platform, VP of Engineering]
  seniorities: [c_suite, vp, head]
  locations: [Singapore, Indonesia, Vietnam, Malaysia, Thailand]
  industries: [financial services, banking]
  employee_ranges: ["11,50", "51,200", "201,500"]
  keywords: [payments]
  similar_titles: true
```

Every key is optional and maps onto one Apollo search parameter:
`titles` → `person_titles`, `seniorities` → `person_seniorities`,
`locations` → `organization_locations`, `person_locations` → `person_locations`,
`industries` → `organization_industries`, `employee_ranges` →
`organization_num_employees_ranges`, `keywords` → `q_keywords`, and
`similar_titles` → `include_similar_titles`.

Selection order, weakest to strongest:

1. `LEAD_APOLLO__*` — the environment defaults
2. the named profile — overrides only the fields it sets
3. CLI flags (`--limit`, `--search-profile`, `--search-profiles-path`)

A field the profile omits keeps its environment value, so a profile can be a
narrow delta rather than a full restatement. `--search-profile` with no value
means `default`. An unknown profile name, an unknown field, or an unparseable
band is a configuration error (exit 2) raised *before* any request is made — a
typo costs you a message, not a quota.

### Named filter profiles

*What to search for* lives in a search profile; *which results to keep* lives in
a **filter profile**. Same idea, different file: an ICP is a campaign's shared,
reviewed asset, so `config/filters.yaml` holds it rather than a shell history
full of `--min-employees`:

```yaml
default:
  description: Decision makers at established companies.
  allowed_titles: [CTO, VP Engineering, Head of Engineering, Founder]
  seniority: [founder, c_suite, vp]
  minimum_employee_count: 10
  maximum_employee_count: 1000
  required_fields: [company.name]

enterprise_na:
  allowed_countries: [US, CA]
  blocked_titles: [Intern, Assistant]
  seniority: [c_suite, vp, director]
  minimum_employee_count: 200
  exclude_free_email: true
  exclude_role_based_email: true
  required_fields: [company.name, person.email]
```

The file reads as a policy document, so its vocabulary is plainer than the
internal one — both spellings describe the same rule:

| Profile key | Setting |
| ----------- | ------- |
| `allowed_titles` / `blocked_titles` | `include_titles` / `exclude_titles` |
| `allowed_countries` / `blocked_countries` | `include_countries` / `exclude_countries` |
| `seniority` / `blocked_seniority` | `include_seniority` / `exclude_seniority` |
| `industries` / `blocked_industries` | `include_industries` / `exclude_industries` |
| `minimum_employee_count` / `maximum_employee_count` | `min_employees` / `max_employees` |
| `blocked_domains` | `exclude_domains` |
| `required_fields`, `exclude_free_email`, `exclude_role_based_email` | unchanged |

Selection order, weakest to strongest:

1. `LEAD_FILTERS__*` — the environment defaults
2. the named profile — a **complete** rule set, not a delta
3. CLI flags — override individual rules

Two deliberate differences from search profiles:

- **A profile replaces the environment's rules rather than layering onto them.**
  A search profile is a query delta; a filter profile is a statement of which
  leads we keep. Merging it with whatever happened to be exported in the shell
  would make the same named profile mean different things on different machines,
  which defeats the point of naming it. Explicit flags still win, so a one-off
  run can narrow a shared profile without editing the file.
- **The file is inert unless named.** With no `--filter-profile` and no
  `LEAD_FILTER_PROFILE`, nothing is read at all — a missing or malformed file
  cannot break a run that never asked for it. Naming a profile resolves it
  eagerly, so a typo fails before the crawl rather than after.

`--filter-profile` with no value means `default`. `--list-filter-profiles` prints
what a file defines; `--filter-profiles-path` (or `LEAD_FILTER_PROFILES_PATH`)
points somewhere else. An unknown profile name, an unknown key, a bad dotted
path, a headcount band that runs backwards or a seniority outside the shared
vocabulary is exit 2.

### One name, both halves

The two files answer two halves of one question. `--profile NAME` is the umbrella
that selects both at once:

```bash
python -m src.main --source apollo --profile singapore_tech
```

`singapore_tech` is defined in *both* files, so that one command searches for
Singapore engineering leaders and keeps only those that fit the ICP. The two
halves stay in their own files — a targeting definition needs to be readable to
whoever owns the targeting, a qualification rule to whoever owns the ICP — and a
profile is not a third document to keep in sync: it is the same name looked up in
both.

A name defined in only one file is a legitimate strategy, not a broken one. Such
a name contributes only that half and leaves the other exactly as configured, and
a note is logged so the asymmetry is visible:

```
profile 'sea_fintech' defines no filters half; filters settings are left as configured
```

Resolution order, weakest first:

```
LEAD_PROFILE  <  --profile  <  --search-profile / --filter-profile
```

An explicitly named half is the more specific request and wins, so a one-off run
can keep a strategy's search and swap its qualification rules without editing a
file. `--list-profiles` shows what both files define and which halves each name
covers — the point being to see at a glance that `sea_fintech` is search-only:

```
Profiles in config/search_profiles.yaml and config/filters.yaml:

  default              search+filters
  enterprise_apac      search
  enterprise_na        filters
  sea_fintech          search
  singapore_tech       search+filters
```

Names are matched loosely: case, surrounding whitespace and the joining
character are all forgiven, so `singapore_tech`, `Singapore Tech` and
`singapore-tech` reach the same profile. Only the *joining* character — `de-fault`
is not `default` and will not silently become it. A file that defines two names
differing only in punctuation is rejected, because they would collapse to one key
and the second would silently replace the first.

Unknown names fail before any request is made, and the message carries the
alternatives and the files searched, since the usual cause is a typo and neither
file is open in front of whoever reads the error:

```
configuration error: unknown profile 'singapore-techh'; searched
config/search_profiles.yaml and config/filters.yaml. Available profiles:
default (search+filters), singapore_tech (search+filters), sea_fintech (search),
enterprise_apac (search), enterprise_na (filters)
```

### The website source

`website` is an enrichment adapter, not a directory: you give it company domains
and it reads their public pages for a description, contact page, published email
addresses and social links. One record per site.

It is deliberately polite. It reads the homepage and at most one contact-style
page per site (`MAX_PAGES_PER_SITE`, hard-capped at 10), waits
`REQUEST_DELAY` seconds between requests to the same host, identifies itself in
the `User-Agent`, and obeys `robots.txt` — failing *open* when a site has none,
which is the common case, and skipping a path it is asked not to fetch. Links to
other hosts are not followed unless `SAME_HOST_ONLY` is turned off. A site whose
pages contain nothing beyond a domain produces no record at all rather than an
empty one.

All of the HTML and JSON-LD understanding is private to `src/crawlers/website.py`;
it uses the standard library parser and adds no scraping dependency. The
company's first published mailbox is placed in the person email field — the
schema's only address slot — and every address found is preserved verbatim in
the record's `raw` payload.

---

## The processing layer

Every source, whatever its shape, is funneled into one schema by four stages
that run in a fixed order: **normalize → validate → filter → deduplicate**.

### Normalization

`src/processors/normalizer.py` turns a `RawLead` (whatever the adapter scraped
or fetched) into a `StandardizedLead`. Adapters deliberately return values
un-cleaned; cleaning is this stage's job, not theirs.

| Field | What normalization does |
| ----- | ----------------------- |
| Person names | Trims, collapses internal whitespace, NFKC-folds and removes zero-width characters, then title-cases only what looks shouted (`ADA` → `Ada`, `McDonald` and `eBay` left alone) |
| Name parts | Reconciles `full_name` against `first`/`last` in both directions, so either representation can be the one the source supplied |
| Emails | Lowercased, trimmed, `mailto:` and `Name <addr>` wrappers stripped, syntax-checked. No mailbox is ever contacted — Phase 1 does not verify deliverability |
| Domains | `https://www.example.com/`, `http://example.com/path` and `www.example.com` all become `example.com`. Subdomains survive (`careers.acme.com`), ports and paths do not |
| URLs | Canonical `https://host/path`, with campaign parameters stripped (see below) but meaningful query parameters kept |
| Job titles | Whitespace normalized and a trailing employer clause removed (`CTO at Acme Corp` → `CTO`) |
| Countries | Aliases and native spellings mapped to ISO 3166-1 alpha-2 (`usa`, `United States`, `Deutschland` → `US`, `DE`). An unrecognised country passes through rather than being dropped |
| Phones | E.164-shaped: punctuation stripped, `00` prefix converted to `+`. Not a full libphonenumber port |
| Headcount | Bands take the **lower** bound (`201-500` → `201`) so a company is never overstated |

**Tracking parameters are dropped, not the whole query.** `utm_*`, `fbclid`,
`gclid`, `msclkid`, `mc_*`, `_hs*`, `pk_*` and their relatives exist only to
attribute a click, so two exports of the same page would otherwise differ. The
deliberately narrow list lives in `src/utils/urls.py` (`TRACKING_PARAMS` /
`TRACKING_PARAM_PREFIXES`). Parameters that merely *look* like tracking — `ref`,
`source`, `si` — are **kept**, because they are just as often real page
selectors (`?source=careers`) and a wrong guess points a lead at the wrong page.

**Both raw and normalized values are kept where the difference matters.** A job
title is normalized into `person.job_title`, and the original is preserved as
`person.job_title_raw` — but *only when cleaning actually changed something*, so
a plain `VP Engineering` leaves `job_title_raw` empty instead of duplicating the
column beside it. `CTO at Acme Corp` exports as `person_job_title=CTO`,
`person_job_title_raw=CTO at Acme Corp`; the stripped clause may still be evidence
about the employer.

### Validation: reject *or* mark

`src/processors/validator.py` returns a `ValidationOutcome` carrying individual
`ValidationIssue`s, each with a `severity`:

- `ERROR` — the record cannot be used and is dropped. Absent identity, an
  implausible or department name, a malformed address, an email that contradicts
  the company domain.
- `WARNING` — the record is usable, so it is **kept and flagged**.

Warnings come from a specific place: a value the source sent that the normalizer
could not read. Those are recorded on the lead as `normalization_issues` (rule,
field, offending value) and converted into warnings by the validator. This
matters because normalization can mask a loss — a garbled `company_domain` plus
a readable `company_website` still yields a good lead, and without the issue list
the garbled field would be invisible.

Absent values are **not** issues. `None`, `""`, `"n/a"` and `"-"` mean "the
source had nothing", which is different from "the source sent something broken",
and conflating the two would bury the real problems.

`normalization_issues` is intentionally excluded from `flatten()`, from
`completeness` and from `identity_keys()`: it is a quality note *about* the
record, not part of it, and it must never change how a lead ranks or what it
matches on.

Warnings are never silent. A warned record stays out of `validation_failed`,
`records_invalid` and `total_rejected` — nothing was dropped — and is counted
instead in:

| Signal | Where |
| ------ | ----- |
| `stats.validation_warned` | Count of kept-but-flagged records (a subset of `normalized`) |
| `stats.per_validation_reason` | Rule name → records that raised it, errors and warnings alike |
| `run summary` log | `records_warned` and `validation_rules` keys, JSON-serializable |
| CLI summary | A `flagged (kept)` line and a `validation rules:` breakdown |
| `--verbose` | One `lead flagged` DEBUG line per record, naming the lead and why |

Attribution is per *record*, not per firing: two unreadable URLs on one lead is
one lead with a URL problem, so `per_validation_reason` answers "which checks are
noisy?" rather than counting raw issue objects.

### Filtering

Filters are opt-in qualification rules covering geography, firmographics,
seniority, job titles and contactability — see the `LEAD_FILTERS__*` table above.
With every default in place nothing is filtered, because filtering is a
deliberate narrowing and never a surprise.

Two families of rule are worth telling apart, because both look like "filter by
title" and they mean different things:

| Rule | Match | `CTO` selects | Use it when |
| ---- | ----- | ------------- | ----------- |
| `include_titles` / `exclude_titles` | whole normalized title | `CTO`, `cto` | You know the exact titles you want |
| `include_title_keywords` / `exclude_title_keywords` | substring | `CTO`, `Assistant to the CTO`, `CTO/Founder` | You want a family of titles |

Whole-title matching is case-insensitive and collapses runs of whitespace, but
keeps punctuation — so `C++ Developer` and `C Developer` stay distinct entries.
The cost is that `VP, Engineering` and `VP Engineering` are two entries, which is
visible in the file you edit; the alternative, a wrong match, is not.

`required_fields` takes dotted paths resolved against the lead model, so any
field is addressable without a code change: `company.name`, `person.email`,
`person.linkedin_url`. Each path is attributed separately in the run report
(`company.name` → `require_company_name`), so a multi-requirement profile says
*which* requirement emptied the run. A path that does not exist on the model is a
**configuration error, not a rule that never fires** — a filter that silently
keeps everything would produce a clean-looking run you would trust.

Allow-lists and deny-lists treat missing data differently, on purpose: a lead
with no country fails `include_countries` (it cannot be shown to be inside the
list) but survives `exclude_countries` (you cannot rule out a country you cannot
see). The same asymmetry applies to industries and titles.

### Deduplication

Deduplication matches on an **identity ladder**, and that order is fixed:

```
source_id → email → linkedin → phone_name → name_domain → name_company
```

1. `source_id` — `provider:external_id`. The source itself says these are one
   record, so this is proof and it is scoped by provider: two sources using
   overlapping id spaces is normal, not a duplicate.
2. `email`, `linkedin` — unique to a person; also proof.
3. `phone_name`, `name_domain`, `name_company` — inference, confirmed by a name.

Which keys the active strategy may use is what `dedup_strategy` selects:

| Strategy | Keys | Catches |
| -------- | ---- | ------- |
| `none` | — | Nothing; keeps every record |
| `email` | `source_id`, `email` | Same record re-crawled, same address from two sources |
| `identity` *(default)* | `+ linkedin`, `phone_name` | Someone the second source knows by profile or phone |
| `aggressive` | `+ name_domain`, `name_company` | A name at a company, with no shared address |

**A bare name is never an identity.** There is no surname-only key, so two
different people named Smith at one employer do not collapse. Under `aggressive`,
the same *full* name at the same company domain does match — and is reported as a
probable duplicate, not a proven one.

That distinction is a first-class field. Every collapse is classified as
`exact` (matched on `source_id`, email or LinkedIn — proof) or `probable`
(matched on a name-anchored key — inference), and the run reports all four
statistics:

| Statistic | Meaning |
| --------- | ------- |
| `records_before_deduplication` | Leads that entered the stage |
| `exact_duplicates` | Collapsed on proof |
| `probable_duplicates` | Collapsed on inference |
| `records_after_deduplication` | `before − exact − probable` |

```text
  before dedup       40
  duplicates merged  5
    exact            5
    probable         0
  after dedup        35
```

The split is what tells a reviewer whether a merge is worth checking by hand: a
non-zero `probable` count means some records were combined because two names
looked alike, and `--verbose` names each one and the key that linked it.

**Duplicates are merged, not discarded.** With `dedup_merge_fields` on (the
default), missing fields on the surviving record are filled from the duplicate —
"prefer non-empty", in its simplest and most predictable form — while the
first-seen record wins wherever both carry a value. `lead_id` is left untouched,
so a merged lead keeps a stable id across runs.

Provenance is the exception to "first-seen wins". A `LeadSource` carries a
`sources` list, and merging unions it:

```json
"source": { "provider": "apollo", "external_id": "abc", "sources": ["apollo", "company_website"] }
```

`provider`, `external_id`, `source_url` and `collected_at` stay the primary's —
they describe that record's own retrieval, and overwriting them would
misattribute it — but collapsing two records must not erase the fact that a
second source contributed. The duplicate pairs in the run report name each
absorbed record's provider, and the CSV export carries `sources` as a
semicolon-separated column.

Determinism is structural rather than incidental: matches are looked up in the
fixed ladder order rather than in dictionary order, and a key always resolves to
the *first* lead that claimed it, so a run over the same input always produces
the same output.

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
pytest                      # 714 tests
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

Two of those properties get stated as tests rather than as prose, because both
are the kind that would otherwise decay quietly:

- `tests/test_filter_profiles.py` reads the **shipped** `config/filters.yaml`, so
  the documented example cannot drift into being unloadable without a test
  noticing, and asserts that naming no profile loads no file.
- `tests/test_exporters.py` asserts `make_lead().flatten()` matches
  `LEAD_COLUMNS` exactly, so a new field cannot be added to the model and
  silently left out of the CSV. It also pins the filename a run writes and the
  summary keys, and round-trips Vietnamese both composed and decomposed —
  a mangled export still opens, so only an assertion on the bytes catches it.

---

## Known limitations

Phase 1 is deliberately narrow. What it does **not** do:

- **No Apollo writes, no enrichment.** Only the people-search read path is
  implemented; the key must have access to it. Apollo's people search does not
  return email addresses, so `--source apollo` on its own will be emptied by
  `--require-email` — pair it with `website` or a CSV export that carries
  contacts.
- **Apollo pagination is bounded** by `LEAD_APOLLO__MAX_PAGES` (default 20). At
  the default `per_page=25` that is 500 records; up to 2,000 with
  `LEAD_APOLLO__PER_PAGE=100`. A larger pull needs either a narrower search or a
  later phase with query partitioning.
- **The `website` source is enrichment, not discovery.** It only ever reads the
  domains you hand it; it does not find companies for you. It also reports one
  record per site, not per person — it can describe an employer but cannot tell
  you who works there. Emails found on a site are frequently shared inboxes
  (`info@`), which `--exclude-role-based-email` will then drop.
- **Website extraction is heuristic.** Open Graph tags, JSON-LD `Organization`
  blocks and `<title>` taglines cover the common shapes; a site that renders
  everything client-side, or that publishes none of them, yields little. Nothing
  is executed — only the HTML that is served is read, and no JavaScript is run.
- **A website seed is normalized to `https://<host>`**, dropping any scheme and
  port you typed. That is right for a public company site and wrong for a
  staging box on `http://localhost:8080`, which this source cannot reach.
- **Email deliverability is never checked.** Normalization validates syntax only
  and contacts no mailbox — verification is an external service and out of scope
  for Phase 1. A syntactically perfect address may still bounce.
- **Query strings are preserved except for known tracking parameters.** Only the
  narrow list in `src/utils/urls.py` is removed; a parameter that *looks* like
  tracking but is not on that list (`?ref=`, `?source=`) survives, deliberately,
  because dropping a real page selector would point a lead at the wrong page.
  Add to `TRACKING_PARAMS` if a source you use carries a distinct click-id.
- **`mailto:`, `tel:` and `javascript:` links are rejected as URLs** rather than
  coerced into a host. A contact page recorded from a site's markup can
  therefore be empty when the site's only contact link is an email address —
  correct, since that is not a page, but worth knowing when a site looks like it
  should have yielded one.
- **Only one original value is preserved alongside its normalized form.** Job
  titles keep `job_title_raw` because trimming a trailing employer clause is
  lossy and the clause is often useful. Other fields keep only the normalized
  value; the pre-normalization record survives solely in the `raw` payload of a
  JSON/JSONL export.
- **`aggressive` dedup can still over-merge.** It matches on full name + company
  domain, which collapses two genuinely different people who share a full name at
  one employer (`John Smith` twice at `acme.com`). Surname-only matching was
  removed precisely because it merged unrelated colleagues, but a shared full
  name at one company is indistinguishable from a duplicate without a stronger
  key. Such merges are reported as `probable_duplicates` rather than
  `exact_duplicates`, so the run tells you how many are worth a look. `identity`
  (the default) never takes this risk.
- **Whole-title matching requires you to enumerate the titles.** `include_titles`
  matches the normalized title exactly, so it will not admit a spelling you did
  not list — which is the intent, but it means an ICP driven by `allowed_titles`
  needs the variants (`VP Engineering` *and* `VP, Engineering`) spelled out. The
  run report's `include_titles` count is the signal that the list is too narrow.
- **A filter profile replaces the environment's filter rules rather than merging
  with them.** That is deliberate — it makes a named profile mean the same thing
  everywhere — but it means introducing `--filter-profile` into a workflow that
  relied on `LEAD_FILTERS__*` variables silently drops those variables. Flags
  still override individual rules on top of the profile.
- **Deduplication is single-key, not scored.** A record matches on the strongest
  key available to it, so two records linked only by a weak key merge even when a
  closer look would separate them. There is no confidence threshold below which a
  match is refused; the `exact`/`probable` split reports the risk rather than
  preventing it.
- **Merging cannot arbitrate a disagreement.** Where both records carry a value
  for the same field, the first-seen one wins — the pipeline has no way to tell
  which source is more trustworthy. `dedup_merge_fields=false` makes that
  explicit by refusing to merge at all.
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

1. **More sources** — Crunchbase, Hunter, Clearbit, or a generic "any JSON API"
   adapter. Each is one module. The `website` adapter is also the natural place
   to add per-site extraction rules when a high-value domain needs them.
2. **A persistence layer** — write runs into SQLite/Postgres keyed by the
   deterministic `lead_id`, so runs can be diffed (new / changed / gone) instead
   of overwritten.
3. **XLSX export** — the CSV exporter already centralizes the column list; an
   openpyxl writer is a small addition.
4. **A second-pass enrichment stage** — a processor that takes an already
   standardized lead set and fills gaps from a second provider. The `website`
   adapter already has the right shape for this: pointing it at the domains a
   first pass discovered, rather than at domains typed on a command line.
5. **Scheduling** — only once the run and its outputs are persisted, so a
   scheduled run has somewhere to record what it did.
