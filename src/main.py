"""Command-line entry point.

    python -m src.main
    python -m src.main --source apollo --limit 100
    python -m src.main --source csv --csv-path leads.csv --format csv,jsonl

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
from src.config import load_settings
from src.models.enums import DedupStrategy, LogFormat
from src.utils.errors import ConfigError, ExportError, LeadCrawlerError
from src.utils.logging import configure_logging, get_logger
from src.utils.time import utcnow

if TYPE_CHECKING:
    from collections.abc import Sequence

    from src.config import Settings
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
        "--list-sources", action="store_true", help="List available sources and exit."
    )

    # --- Output -------------------------------------------------------- #
    output = parser.add_argument_group("output")
    output.add_argument(
        "-o", "--output-dir", type=Path, metavar="DIR", help="Where to write exports."
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
    filters.add_argument("--title-keywords", metavar="LIST", help="Job title must contain one.")
    filters.add_argument(
        "--exclude-title-keywords", metavar="LIST", help="Job title must contain none."
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
    runtime.add_argument("--log-level", choices=_LOG_LEVELS, help="Logging verbosity.")
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
        "include_title_keywords": _split_opt(args.title_keywords),
        "exclude_title_keywords": _split_opt(args.exclude_title_keywords),
        "require_email": args.require_email,
        "require_linkedin": args.require_linkedin,
        "require_company_domain": args.require_company_domain,
        "exclude_free_email": args.exclude_free_email,
        "exclude_role_based_email": args.exclude_role_based_email,
    }
    return {key: value for key, value in mapping.items() if value not in (None, [])}


def build_settings(args: argparse.Namespace) -> Settings:
    """Merge CLI flags over the environment."""
    overrides: dict[str, Any] = {
        "log_level": args.log_level,
        "log_format": args.log_format,
        "output_dir": args.output_dir,
        "output_prefix": args.output_prefix,
        "default_limit": args.limit,
        "dedup_strategy": args.dedup,
        "min_completeness": args.min_completeness,
        "sort_by_completeness": args.sort_by_completeness,
    }

    formats = _split_csv_arg(args.format)
    if formats:
        overrides["output_formats"] = formats

    if filter_overrides := _filter_overrides(args):
        overrides["filters"] = filter_overrides

    if args.csv_path is not None:
        overrides["csv_source"] = {"path": args.csv_path}

    return load_settings(
        env_file=args.config,
        **{key: value for key, value in overrides.items() if value is not None},
    )


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


def _print_summary(result: CrawlResult, outputs: Sequence[Path], *, dry_run: bool) -> None:
    stats = result.stats
    print()
    print("Run summary")
    print("-----------")
    print(f"  sources            {', '.join(stats.per_provider) or '<none>'}")
    print(f"  raw collected      {stats.raw_collected}")
    print(f"  normalized         {stats.normalized}")
    print(f"  failed validation  {stats.validation_failed}")
    print(f"  filtered out       {stats.filtered_out}")
    print(f"  duplicates merged  {stats.duplicates_removed}")
    print(f"  leads kept         {len(result.leads)}")
    if stats.duration_seconds is not None:
        print(f"  duration           {stats.duration_seconds:.2f}s")

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
    from src.exporters import build_exporter, write_run_report
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
        directory = settings.ensure_output_dir()
        timestamp = utcnow().strftime("%Y%m%dT%H%M%SZ")
        for fmt in settings.active_formats():
            exporter = build_exporter(fmt, settings)
            path = exporter.build_path(directory, settings.output_prefix, timestamp)
            export_result = await exporter.export(result.leads, path)
            outputs.append(export_result.path)
            result.stats.output_files.append(str(export_result.path))
            logger.info("exported leads", extra={"format": fmt.value, "path": str(path)})

        if settings.write_run_report:
            report_path = directory / f"{settings.output_prefix}_{timestamp}_report.json"
            outputs.append(await write_run_report(result, report_path, settings, limit=limit))

    _print_summary(result, outputs, dry_run=args.dry_run)

    if args.fail_on_empty and not result.leads:
        return EXIT_EMPTY
    return EXIT_OK


def cli(argv: Sequence[str] | None = None) -> int:
    """Parse arguments and run. Returns the process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)

    # Logging is configured before anything can fail, so config errors are logged
    # in the format the caller asked for.
    configure_logging(args.log_level or "INFO", args.log_format or LogFormat.CONSOLE.value)

    if args.list_sources:
        _print_sources()
        return EXIT_OK
    if args.list_formats:
        _print_formats()
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
