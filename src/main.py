"""Command-line entry point.

    python -m src.main
    python -m src.main --source apollo --limit 100
    python -m src.main --source apollo --profile singapore_tech
    python -m src.main --source csv --csv-path leads.csv --format csv,jsonl

``--profile NAME`` is the umbrella over a whole acquisition strategy: the same
name is looked up in the search profiles file and the filter profiles file, so
one word selects who to look for and which of them to keep. ``--search-profile``
and ``--filter-profile`` name one half each and win over it.

Logs go to stderr; the run summary goes to stdout, so ``--log-format json``
composes cleanly with shell pipelines.

Exit codes:
    0  run completed (the result set may still be empty)
    1  unexpected internal failure
    2  configuration error (bad flag, unknown source, missing credentials)
    3  every requested source failed
    4  ``--fail-on-empty`` was set and no leads were produced
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from src import __version__
from src.config import FilterSettings, load_settings
from src.filter_profiles import (
    DEFAULT_PROFILE_NAME,
    DEFAULT_PROFILES_PATH,
    get_filter_profile,
    load_filter_profiles,
)
from src.models.enums import DedupStrategy, LogFormat
from src.profiles import HALVES, profile_names, resolve_profile
from src.utils.errors import ConfigError, ExportError, LeadCrawlerError
from src.utils.io import ensure_directory
from src.utils.logging import configure_logging, get_logger
from src.utils.time import utcnow

if TYPE_CHECKING:
    from collections.abc import Sequence

    from src.config import Settings
    from src.filter_profiles import FilterProfile
    from src.models.results import CrawlResult


class _SupportsAddArgument(Protocol):
    """Anything flags can be attached to.

    ``ArgumentParser`` and the groups returned by ``add_argument_group`` share no
    useful base class in the public API, but both can take an argument.
    """

    def add_argument(self, *args: Any, **kwargs: Any) -> Any: ...

    def add_mutually_exclusive_group(self, *, required: bool = False) -> _SupportsAddArgument: ...


EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CONFIG = 2
EXIT_ALL_SOURCES_FAILED = 3
EXIT_EMPTY = 4

_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


# --------------------------------------------------------------------------- #
# Argument parsing
# --------------------------------------------------------------------------- #
def _add_bool_pair(
    parser: _SupportsAddArgument, name: str, help_text: str, *, dest: str | None = None
) -> None:
    """Add ``--flag`` / ``--no-flag``.

    Both default to ``None`` so an omitted pair leaves any environment value
    untouched; without this, argparse's ``store_true`` default of ``False`` would
    silently override ``LEAD_FILTERS__REQUIRE_EMAIL=true``.
    """
    target = dest or name.replace("-", "_")
    group = parser.add_mutually_exclusive_group()
    group.add_argument(f"--{name}", dest=target, action="store_true", default=None, help=help_text)
    group.add_argument(
        f"--no-{name}",
        dest=target,
        action="store_false",
        default=None,
        help=f"Explicitly disable {name} (overrides the environment).",
    )


def build_parser() -> argparse.ArgumentParser:
    """Construct the argument parser."""
    parser = argparse.ArgumentParser(
        prog="lead-crawler",
        description=(
            "Tinasoft Phase 1 lead crawler: collect, normalize, validate, filter, "
            "deduplicate and export B2B leads."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        epilog=(
            "Exit codes: 0 ok, 1 error, 2 config error, 3 all sources failed, "
            "4 empty result with --fail-on-empty."
        ),
    )

    parser.add_argument("--version", action="version", version=f"lead-crawler {__version__}")

    # --- Sources ------------------------------------------------------- #
    sources = parser.add_argument_group("sources")
    sources.add_argument(
        "-s",
        "--source",
        action="append",
        metavar="NAME",
        help=(
            "Source to crawl. Repeat or comma-separate for several "
            "(e.g. -s csv -s mock, or -s csv,mock). Defaults to LEAD_DEFAULT_SOURCES."
        ),
    )
    sources.add_argument(
        "-l",
        "--limit",
        type=int,
        metavar="N",
        help="Maximum raw leads to request from EACH source.",
    )
    sources.add_argument(
        "--max-leads",
        type=int,
        metavar="N",
        help="Cap the final result set after deduplication (keeps the most complete leads).",
    )
    sources.add_argument(
        "--csv-path",
        type=Path,
        metavar="PATH",
        help="Input file for the 'csv' source.",
    )
    sources.add_argument(
        "--website-url",
        action="append",
        metavar="URL",
        help=(
            "Company site for the 'website' source, e.g. acme.com. "
            "Repeat or comma-separate for several."
        ),
    )
    sources.add_argument(
        "--profile",
        metavar="NAME",
        help=(
            "Acquisition strategy: the profile of this name in BOTH profile files. "
            "A name defined in only one of them applies in only one place. "
            "Overrides --search-profile/--filter-profile from the environment; "
            "those flags on the command line override this."
        ),
    )
    sources.add_argument(
        "--list-profiles",
        action="store_true",
        help="List the profiles defined across both profile files and exit.",
    )
    sources.add_argument(
        "--search-profile",
        nargs="?",
        const="default",
        metavar="NAME",
        help=(
            "Named filter set from the search profiles file for the 'apollo' source. "
            "Bare --search-profile means the 'default' profile."
        ),
    )
    sources.add_argument(
        "--search-profiles-path",
        type=Path,
        metavar="PATH",
        help="Search profiles file (default: LEAD_SEARCH_PROFILES_PATH).",
    )
    sources.add_argument(
        "--list-sources", action="store_true", help="List available sources and exit."
    )

    # --- Output -------------------------------------------------------- #
    output = parser.add_argument_group("output")
    destination = output.add_mutually_exclusive_group()
    destination.add_argument(
        "-o",
        "--output",
        type=Path,
        metavar="PATH",
        help=(
            "Write the export to this exact file. Requires a single --format; "
            "use --output-dir for the timestamped multi-format layout."
        ),
    )
    destination.add_argument(
        "--output-dir",
        type=Path,
        metavar="DIR",
        help="Directory for timestamped exports (default: LEAD_OUTPUT_DIR).",
    )
    output.add_argument(
        "-f",
        "--format",
        action="append",
        metavar="FMT",
        help="Export format(s): csv, json, jsonl. Repeat or comma-separate.",
    )
    output.add_argument("--output-prefix", metavar="NAME", help="Filename prefix (default: leads).")
    output.add_argument(
        "--dry-run",
        action="store_true",
        help="Run the pipeline but write nothing; prints the summary only.",
    )
    output.add_argument(
        "--list-formats", action="store_true", help="List available export formats and exit."
    )

    # --- Processing ---------------------------------------------------- #
    processing = parser.add_argument_group("processing")
    processing.add_argument(
        "--dedup",
        choices=[strategy.value for strategy in DedupStrategy],
        help="Duplicate-collapsing strategy.",
    )
    processing.add_argument(
        "--min-completeness",
        type=float,
        metavar="0..1",
        help="Drop leads scoring below this completeness.",
    )
    processing.add_argument(
        "--sort-by-completeness",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Emit leads best-first.",
    )

    # --- Filters ------------------------------------------------------- #
    filters = parser.add_argument_group("filters")
    filters.add_argument("--countries", metavar="LIST", help="Keep only these countries.")
    filters.add_argument("--exclude-countries", metavar="LIST", help="Drop these countries.")
    filters.add_argument("--industries", metavar="LIST", help="Keep only these industries.")
    filters.add_argument("--exclude-industries", metavar="LIST", help="Drop these industries.")
    filters.add_argument("--min-employees", type=int, metavar="N", help="Minimum company size.")
    filters.add_argument("--max-employees", type=int, metavar="N", help="Maximum company size.")
    filters.add_argument("--exclude-domains", metavar="LIST", help="Company domains to drop.")
    filters.add_argument("--seniority", metavar="LIST", help="Keep only these seniority levels.")
    filters.add_argument("--exclude-seniority", metavar="LIST", help="Drop these seniority levels.")
    filters.add_argument(
        "--titles", metavar="LIST", help="Keep only these exact job titles (whole-title match)."
    )
    filters.add_argument("--exclude-titles", metavar="LIST", help="Drop these exact job titles.")
    filters.add_argument("--title-keywords", metavar="LIST", help="Job title must contain one.")
    filters.add_argument(
        "--exclude-title-keywords", metavar="LIST", help="Job title must contain none."
    )
    filters.add_argument(
        "--required-fields",
        metavar="PATHS",
        help="Drop leads missing any of these dotted paths, e.g. company.name,person.email.",
    )
    filters.add_argument(
        "--filter-profile",
        nargs="?",
        const=DEFAULT_PROFILE_NAME,
        metavar="NAME",
        help=(
            "Named qualification profile from the filter profiles file. "
            "Bare --filter-profile means the 'default' profile. Profile rules are "
            "the base; explicit filter flags below override them."
        ),
    )
    filters.add_argument(
        "--filter-profiles-path",
        type=Path,
        metavar="PATH",
        help="Filter profiles file (default: LEAD_FILTER_PROFILES_PATH).",
    )
    filters.add_argument(
        "--list-filter-profiles",
        action="store_true",
        help="List the profiles defined in the filter profiles file and exit.",
    )
    _add_bool_pair(filters, "require-email", "Keep only leads with an email address.")
    _add_bool_pair(filters, "require-linkedin", "Keep only leads with a LinkedIn URL.")
    _add_bool_pair(filters, "require-company-domain", "Keep only leads with a company domain.")
    _add_bool_pair(filters, "exclude-free-email", "Drop consumer mailbox providers.")
    _add_bool_pair(filters, "exclude-role-based-email", "Drop shared inboxes (info@, sales@).")

    # --- Runtime ------------------------------------------------------- #
    runtime = parser.add_argument_group("runtime")
    runtime.add_argument(
        "--config", type=Path, metavar="PATH", help="Path to a .env file (default: ./.env)."
    )
    verbosity = runtime.add_mutually_exclusive_group()
    verbosity.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Shorthand for --log-level DEBUG.",
    )
    verbosity.add_argument("--log-level", choices=_LOG_LEVELS, help="Logging verbosity.")
    runtime.add_argument(
        "--log-format", choices=[fmt.value for fmt in LogFormat], help="Log rendering style."
    )
    runtime.add_argument(
        "--fail-on-empty", action="store_true", help="Exit 4 when no leads are produced."
    )
    return parser


def _split_csv_arg(values: Sequence[str] | None) -> list[str]:
    """Flatten repeated and comma-separated option values into a list."""
    if not values:
        return []
    items: list[str] = []
    for value in values:
        items.extend(part.strip() for part in value.split(",") if part.strip())
    return items


def _split_opt(value: str | None) -> list[str]:
    """Split a single optional comma-separated value, empty when unset."""
    return _split_csv_arg([value] if value else None)


# --------------------------------------------------------------------------- #
# Settings assembly
# --------------------------------------------------------------------------- #
def _filter_overrides(args: argparse.Namespace) -> dict[str, Any]:
    """Collect filter settings the user explicitly passed.

    Only values the user actually supplied are returned, so an omitted flag never
    overrides an environment value.
    """
    mapping: dict[str, Any] = {
        "include_countries": _split_opt(args.countries),
        "exclude_countries": _split_opt(args.exclude_countries),
        "include_industries": _split_opt(args.industries),
        "exclude_industries": _split_opt(args.exclude_industries),
        "min_employees": args.min_employees,
        "max_employees": args.max_employees,
        "exclude_domains": _split_opt(args.exclude_domains),
        "include_seniority": _split_opt(args.seniority),
        "exclude_seniority": _split_opt(args.exclude_seniority),
        "include_titles": _split_opt(args.titles),
        "exclude_titles": _split_opt(args.exclude_titles),
        "include_title_keywords": _split_opt(args.title_keywords),
        "exclude_title_keywords": _split_opt(args.exclude_title_keywords),
        "required_fields": _split_opt(args.required_fields),
        "require_email": args.require_email,
        "require_linkedin": args.require_linkedin,
        "require_company_domain": args.require_company_domain,
        "exclude_free_email": args.exclude_free_email,
        "exclude_role_based_email": args.exclude_role_based_email,
    }
    return {key: value for key, value in mapping.items() if value not in (None, [])}


def _resolve_filters(args: argparse.Namespace, base: Settings) -> FilterSettings:
    """Combine the environment, a named profile and the CLI into one rule set.

    Precedence, weakest first: the environment's ``LEAD_FILTERS__*`` values, then
    a named filter profile, then the filter flags on the command line.

    The profile *replaces* the environment's rules rather than layering onto
    them. A profile is a complete statement of which leads we keep, and merging
    it with whatever happened to be exported in the shell would make the same
    named profile mean different things on different machines — the opposite of
    what naming it is for. Explicit flags still win over both, so a one-off run
    can narrow a shared profile without editing the file.
    """
    profile = get_filter_profile(base.filter_profile, path=base.filter_profiles_path)
    rules = profile.to_filter_settings() if profile is not None else base.filters

    overrides = _filter_overrides(args)
    if not overrides:
        return rules
    return FilterSettings(**{**rules.model_dump(), **overrides})


def _apply_umbrella(args: argparse.Namespace, base: Settings) -> Settings:
    """Fill ``search_profile``/``filter_profile`` in from the umbrella ``--profile``.

    Precedence, weakest first: the environment (already folded into ``base``), the
    umbrella ``--profile``, then an explicit ``--search-profile`` or
    ``--filter-profile``. An explicitly named half is the more specific request,
    so it wins; the umbrella supplies only the halves nothing else named.

    A strategy defined in only one file fills in only that half and leaves the
    other exactly as the environment had it. That is what makes a search-only or
    filter-only profile a legitimate strategy rather than half a broken one — and
    the missing half is logged, because the run is otherwise indistinguishable
    from one where the profile covered both sides.

    Raises:
        ConfigError: if the named profile is defined in neither file, or its
            filter rules are invalid. Raised before any crawling starts.
    """
    if not base.profile:
        return base

    resolved = resolve_profile(
        base.profile,
        search_path=base.search_profiles_path,
        filter_path=base.filter_profiles_path,
    )

    logger = get_logger("main")
    for half in HALVES:
        if half not in resolved.defined_halves:
            logger.info(
                f"profile {resolved.name!r} defines no {half} half; "
                f"{half} settings are left as configured",
                extra={"profile": resolved.name, "half": half},
            )

    updates: dict[str, Any] = {}
    if resolved.search is not None and args.search_profile is None:
        updates["search_profile"] = resolved.name
    if resolved.filters is not None and args.filter_profile is None:
        updates["filter_profile"] = resolved.name
    return base.model_copy(update=updates) if updates else base


def build_settings(args: argparse.Namespace) -> Settings:
    """Merge CLI flags over the environment, then apply the named profiles."""
    overrides: dict[str, Any] = {
        # --verbose is sugar for the one log level it names; the two are mutually
        # exclusive at the parser, so there is no precedence question to resolve.
        "log_level": "DEBUG" if args.verbose else args.log_level,
        "log_format": args.log_format,
        "output_dir": args.output_dir,
        "output_prefix": args.output_prefix,
        "default_limit": args.limit,
        "dedup_strategy": args.dedup,
        "min_completeness": args.min_completeness,
        "sort_by_completeness": args.sort_by_completeness,
        "profile": args.profile,
        "search_profile": args.search_profile,
        "search_profiles_path": args.search_profiles_path,
        "filter_profile": args.filter_profile,
        "filter_profiles_path": args.filter_profiles_path,
    }

    formats = _split_csv_arg(args.format)
    if formats:
        overrides["output_formats"] = formats

    if args.csv_path is not None:
        overrides["csv_source"] = {"path": args.csv_path}

    if website_urls := _split_csv_arg(args.website_url):
        overrides["website"] = {"urls": website_urls}

    base = load_settings(
        env_file=args.config,
        **{key: value for key, value in overrides.items() if value is not None},
    )
    # Profiles are resolved after the base settings exist, because both the file
    # paths and the profile names come from settings — flag or environment — and
    # none of them is known before that call. Filters then resolve last, since the
    # rule set is what the filter profile and the filter flags have to agree on.
    named = _apply_umbrella(args, base)
    return named.model_copy(update={"filters": _resolve_filters(args, named)})


# --------------------------------------------------------------------------- #
# Listing helpers
# --------------------------------------------------------------------------- #
def _print_sources() -> None:
    from src.crawlers import registered_crawlers

    print("Available sources:\n")
    for slug, crawler in sorted(registered_crawlers().items()):
        marker = " (requires credentials)" if crawler.requires_credentials else ""
        print(f"  {slug:<10} {crawler.display_name}{marker}")
        if crawler.description:
            print(f"  {'':<10} {crawler.description}")
    print("\nUse with: --source <name>")


def _print_formats() -> None:
    from src.exporters import registered_exporters

    print("Available export formats:\n")
    for fmt, exporter in sorted(registered_exporters().items(), key=lambda item: item[0].value):
        print(f"  {fmt.value:<8} {exporter.description}")
    print("\nUse with: --format <name> (repeat or comma-separate)")


def _summarize_profile(profile: FilterProfile) -> str:
    """One line naming the rules a profile actually sets.

    Only non-default rules are listed, so the line reads as the profile's
    opinions rather than as a dump of its schema.
    """
    parts: list[str] = []
    if profile.allowed_titles:
        parts.append(f"{len(profile.allowed_titles)} allowed titles")
    if profile.blocked_titles:
        parts.append(f"{len(profile.blocked_titles)} blocked titles")
    if profile.allowed_countries:
        parts.append(f"countries {'/'.join(profile.allowed_countries)}")
    if profile.seniority:
        parts.append(f"seniority {'/'.join(profile.seniority)}")

    low, high = profile.minimum_employee_count, profile.maximum_employee_count
    if low is not None or high is not None:
        parts.append(f"{low if low is not None else 0}-{high if high is not None else 'any'} staff")
    if profile.required_fields:
        parts.append(f"requires {', '.join(profile.required_fields)}")
    return "; ".join(parts) or "no rules (keeps everything)"


def _print_filter_profiles(args: argparse.Namespace) -> None:
    """List the profiles defined in the filter profiles file.

    The path comes from ``--filter-profiles-path``, else from the environment, so
    the listing names the same file a run would read. An environment error
    surfaces here as a configuration error rather than being papered over with the
    built-in default, which would list profiles the run would never load.
    """
    path = args.filter_profiles_path
    if path is None:
        path = load_settings(env_file=args.config).filter_profiles_path

    profiles = load_filter_profiles(path)
    print(f"Filter profiles in {path}:\n")
    for name, profile in sorted(profiles.items()):
        print(f"  {name:<20} {_summarize_profile(profile)}")
        if profile.description:
            print(f"  {'':<20} {profile.description}")
    print(f"\nUse with: --filter-profile <name> (default file: {DEFAULT_PROFILES_PATH})")


def _print_profiles(args: argparse.Namespace) -> None:
    """List every profile name across both profile files, with the halves each covers.

    Both files are read, because that is what ``--profile`` does. A name that
    appears in only one of them is precisely the fact this listing exists to make
    visible, so listing one file's names would hide the thing worth seeing.
    """
    settings = load_settings(env_file=args.config)
    search_path = args.search_profiles_path or settings.search_profiles_path
    filter_path = args.filter_profiles_path or settings.filter_profiles_path

    entries = profile_names(search_path=search_path, filter_path=filter_path)
    where = f"{search_path} and {filter_path}" if search_path != filter_path else str(search_path)
    print(f"Profiles in {where}:\n")
    for name, halves in entries:
        covered = "+".join(half for half in HALVES if half in halves)
        print(f"  {name:<20} {covered}")
    if not entries:
        print("  <none>")
    print("\nUse with: --profile <name>, which applies every half named above.")
    print("          --search-profile <name> / --filter-profile <name> select one half.")


def _print_summary(result: CrawlResult, outputs: Sequence[Path], *, dry_run: bool) -> None:
    stats = result.stats
    print()
    print("Run summary")
    print("-----------")
    print(f"  sources            {', '.join(stats.per_provider) or '<none>'}")
    print(f"  raw collected      {stats.raw_collected}")
    print(f"  normalized         {stats.normalized}")
    print(f"  failed validation  {stats.validation_failed}")
    if stats.validation_warned:
        print(f"  flagged (kept)     {stats.validation_warned}")
    print(f"  filtered out       {stats.filtered_out}")
    print(f"  before dedup       {stats.records_before_deduplication}")
    print(f"  duplicates merged  {stats.duplicates_removed}")
    if stats.duplicates_removed:
        # Only worth the two extra lines when something was actually collapsed;
        # the split is what says whether the merge was provable or inferred.
        print(f"    exact            {stats.exact_duplicates}")
        print(f"    probable         {stats.probable_duplicates}")
    print(f"  after dedup        {stats.records_after_deduplication}")
    print(f"  leads kept         {len(result.leads)}")
    if stats.duration_seconds is not None:
        print(f"  duration           {stats.duration_seconds:.2f}s")

    if stats.pages_visited or stats.browser_errors or stats.selector_failures:
        # Printed only for a run that actually drove a browser, so the output of
        # an ``http``-less source is unchanged. A failure line appears only when
        # it happened: a permanent "selector failures 0" trains the reader to
        # skip the block that matters when it is not zero.
        print(f"  pages visited      {stats.pages_visited}")
        if stats.browser_errors:
            print(f"  browser errors     {stats.browser_errors}")
        if stats.selector_failures:
            print(f"  selector failures  {stats.selector_failures}")
        if stats.auth_failures:
            print(f"  auth failures      {stats.auth_failures}")

    if stats.per_validation_reason:
        print("\n  validation rules:")
        for rule, count in sorted(stats.per_validation_reason.items(), key=lambda kv: -kv[1]):
            print(f"    {rule:<28} {count}")

    if stats.per_filter_reason:
        print("\n  filtered by rule:")
        for rule, count in sorted(stats.per_filter_reason.items(), key=lambda kv: -kv[1]):
            print(f"    {rule:<28} {count}")

    if stats.source_errors:
        print("\n  source errors:")
        for provider, message in stats.source_errors.items():
            print(f"    {provider}: {message}")

    if dry_run:
        print("\n  dry run: nothing written")
    elif outputs:
        print("\n  files written:")
        for path in outputs:
            print(f"    {path}")

    if result.leads:
        print("\n  top leads:")
        for lead in result.leads[:5]:
            name = lead.person.full_name or lead.person.email or lead.lead_id
            company = lead.company.name or lead.company.domain or "-"
            print(f"    {lead.completeness:.2f}  {name} — {company}")
    print()


# --------------------------------------------------------------------------- #
# Run
# --------------------------------------------------------------------------- #
async def _run(args: argparse.Namespace, settings: Settings) -> int:
    from src.crawlers import available_providers, build_crawler
    from src.processors import Pipeline

    logger = get_logger("main")

    requested = _split_csv_arg(args.source) or list(settings.default_sources)
    if not requested:
        raise ConfigError("no sources requested; pass --source or set LEAD_DEFAULT_SOURCES")

    unknown = [name for name in requested if name.casefold() not in available_providers()]
    if unknown:
        raise ConfigError(
            f"unknown source(s): {', '.join(unknown)}. "
            f"Available: {', '.join(available_providers())}"
        )

    crawlers = [build_crawler(name, settings) for name in requested]
    limit = args.limit or settings.default_limit

    # Fail on a contradictory or unwritable output request before paying for the
    # crawl — the crawl is the expensive half and, for a metered source, the
    # half that costs money.
    _check_output_target(args, settings, dry_run=args.dry_run)

    # At DEBUG only: which flags and environment values actually won. This is the
    # first thing to look at when a run behaves unlike the command line suggests,
    # and it deliberately lists fields rather than dumping the settings tree so
    # no credential can reach the log.
    get_logger("main").debug(
        "effective configuration",
        extra={
            "sources": requested,
            "limit": limit,
            "max_leads": args.max_leads,
            "formats": [fmt.value for fmt in settings.active_formats()],
            # Whichever of the two destinations actually wins, so the record
            # cannot claim a directory the run is not going to write to.
            "output": str(args.output) if args.output is not None else None,
            "output_dir": str(settings.output_dir),
            "output_prefix": settings.output_prefix,
            "dedup": settings.dedup_strategy.value,
            "min_completeness": settings.min_completeness,
            "sort_by_completeness": settings.sort_by_completeness,
            "filters_active": settings.filters.is_active,
            "profile": settings.profile,
            "search_profile": settings.search_profile,
            "filter_profile": settings.filter_profile,
            "website_urls": len(settings.website.urls),
            "max_concurrency": settings.max_concurrency,
            "http_max_attempts": settings.http_max_attempts,
            "write_run_report": settings.write_run_report,
        },
    )

    try:
        result = await Pipeline(settings).run(crawlers, limit=limit, max_leads=args.max_leads)
    finally:
        # Always release connection pools, including on failure.
        for crawler in crawlers:
            await crawler.aclose()

    # A source that failed is contained; *every* source failing is a run failure.
    failed = set(result.stats.source_errors)
    if failed and failed.issuperset({crawler.provider for crawler in crawlers}):
        logger.error("every requested source failed", extra={"sources": sorted(failed)})
        _print_summary(result, [], dry_run=args.dry_run)
        return EXIT_ALL_SOURCES_FAILED

    outputs: list[Path] = []
    if not args.dry_run:
        outputs = await _export(args, settings, result, limit=limit, sources=requested)

    _print_summary(result, outputs, dry_run=args.dry_run)

    if args.fail_on_empty and not result.leads:
        return EXIT_EMPTY
    return EXIT_OK


def _check_output_target(
    args: argparse.Namespace, settings: Settings, *, dry_run: bool = False
) -> None:
    """Reject an output request that cannot succeed, before any crawling.

    Three things are checked, and all of them are cheap and better known early:

    * ``--output`` names exactly one file, so it can carry exactly one format.
    * The destination directory exists or can be created.
    * That directory is the parent of ``--output``, or the run's output
      directory.

    Checked before the crawl so a contradictory or unwritable invocation fails
    immediately. Discovering it afterwards is not merely slow: for a metered
    source such as Apollo it means paying for a crawl whose results have
    nowhere to go.

    Args:
        args: Parsed CLI arguments.
        settings: Effective settings, for the output directory and formats.
        dry_run: When True nothing will be written, so the directory is left
            alone — a dry run must not create directories as a side effect.

    Raises:
        ConfigError: if the request cannot be satisfied.
    """
    if args.output is not None:
        formats = settings.active_formats()
        if len(formats) > 1:
            requested = ", ".join(fmt.value for fmt in formats)
            raise ConfigError(
                f"--output writes a single file, but {len(formats)} formats were "
                f"requested ({requested}); pass one --format or use --output-dir"
            )

    if dry_run:
        return

    if args.output is None:
        settings.ensure_output_dir()
        return

    # An explicit --output creates its parent the same way --output-dir creates
    # its own, so `--output build/leads.csv` works from a clean checkout.
    ensure_directory(args.output.parent, what="output directory")


async def _export(
    args: argparse.Namespace,
    settings: Settings,
    result: CrawlResult,
    *,
    limit: int,
    sources: Sequence[str] = (),
) -> list[Path]:
    """Write the requested exports and return the paths written.

    Two layouts, chosen by the flag the user passed:

    ``--output PATH``      exactly that file, next to a ``<stem>_report.json``.
    ``--output-dir DIR``   ``<prefix>_<source>_<timestamp>.<ext>`` per format,
                           timestamped so successive runs accumulate instead of
                           overwriting, and labelled with the source so files
                           from different campaigns are told apart by name.

    ``sources`` is the set of providers the run requested — passed in rather than
    read off the result, because a source that failed returned no counts yet
    still belongs in the name of the run that asked for it.

    A format that cannot be written does not cost the others, and does not cost
    the report: all three are attempted, every failure is logged as it happens,
    and the first one is re-raised at the end so the exit code still says the
    run did not fully succeed. The crawl behind this is the expensive part and
    the report is what explains it, so throwing either away over one unwritable
    file would be the wrong trade.

    Raises:
        ExportError: if any format or the run report could not be written.
    """
    from src.exporters import build_exporter, source_slug, write_run_report

    _check_output_target(args, settings)

    logger = get_logger("main")
    formats = settings.active_formats()
    outputs: list[Path] = []
    failures: list[ExportError] = []
    # UTC, matching every other timestamp the tool emits: the filename, the
    # report and the log lines then order the same way without a zone puzzle.
    timestamp = utcnow().strftime("%Y-%m-%d_%H-%M-%S")
    slug = source_slug(sources)

    if args.output is not None:
        directory = args.output.parent
        fixed_path: Path | None = args.output
        report_path = args.output.with_name(f"{args.output.stem}_report.json")
    else:
        directory = settings.ensure_output_dir()
        fixed_path = None
        report_path = directory / f"{settings.output_prefix}_{slug}_{timestamp}_report.json"

    for fmt in formats:
        # Outside the try: an unregistered format is a programming error, and
        # swallowing it here would report it as a filesystem problem.
        exporter = build_exporter(fmt, settings)
        path = fixed_path or exporter.build_path(directory, settings.output_prefix, slug, timestamp)
        try:
            export_result = await exporter.export(result.leads, path)
        except ExportError as exc:
            failures.append(exc)
            logger.error(
                "export failed",
                extra={"format": fmt.value, "path": str(path), "error": str(exc)},
            )
            continue
        outputs.append(export_result.path)
        result.stats.output_files.append(str(export_result.path))
        logger.info(
            "exported leads",
            extra={
                "format": fmt.value,
                "path": str(export_result.path),
                "records": export_result.records,
                "bytes": export_result.bytes_written,
            },
        )

    if settings.write_run_report:
        try:
            outputs.append(
                await write_run_report(result, report_path, settings, limit=limit, sources=sources)
            )
        except ExportError as exc:
            failures.append(exc)
            logger.error("run report failed", extra={"path": str(report_path), "error": str(exc)})

    if failures:
        raise failures[0]
    return outputs


def cli(argv: Sequence[str] | None = None) -> int:
    """Parse arguments and run. Returns the process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)

    # Logging is configured before anything can fail, so config errors are logged
    # in the format the caller asked for.
    configure_logging(
        "DEBUG" if args.verbose else (args.log_level or "INFO"),
        args.log_format or LogFormat.CONSOLE.value,
    )

    if args.list_sources:
        _print_sources()
        return EXIT_OK
    if args.list_formats:
        _print_formats()
        return EXIT_OK
    if args.list_filter_profiles:
        _print_filter_profiles(args)
        return EXIT_OK
    if args.list_profiles:
        _print_profiles(args)
        return EXIT_OK

    try:
        settings = build_settings(args)
        configure_logging(settings.log_level, settings.log_format.value)
        return asyncio.run(_run(args, settings))
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    except ExportError as exc:
        print(f"export error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except LeadCrawlerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except Exception as exc:
        get_logger("main").exception("unexpected failure")
        print(f"unexpected error: {exc}", file=sys.stderr)
        return EXIT_ERROR


def main() -> None:
    """Console-script entry point."""
    raise SystemExit(cli())


if __name__ == "__main__":
    main()
