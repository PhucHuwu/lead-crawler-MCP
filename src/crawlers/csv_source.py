"""CSV file source.

Reads a lead export produced by any other tool. Header names vary wildly between
vendors, so columns are resolved through an alias table rather than requiring an
exact schema; anything unrecognized is preserved in ``RawLead.raw`` instead of
being dropped.

This source is the pragmatic on-ramp for lists that have no API (purchased
lists, conference attendee exports, manual research).
"""

from __future__ import annotations

import asyncio
import csv
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from src.crawlers.base import BaseCrawler
from src.crawlers.registry import register_crawler
from src.models.lead import RawLead
from src.utils.errors import CrawlerError, SourceNotFoundError

if TYPE_CHECKING:
    from collections.abc import Iterator

    from src.config import Settings

_HEADER_SANITIZE_RE = re.compile(r"[^a-z0-9]+")

#: Target field -> header spellings we accept for it. Compared after
#: sanitization, so ``"First Name"``, ``"first_name"`` and ``"FirstName"`` all
#: collapse to ``firstname`` and match one entry.
_COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "first_name": ("first_name", "firstname", "given_name", "fname", "first"),
    "last_name": ("last_name", "lastname", "surname", "family_name", "lname", "last"),
    "full_name": ("full_name", "fullname", "name", "contact_name", "person_name"),
    "job_title": ("job_title", "title", "position", "role", "job_position", "jobtitle"),
    "seniority": ("seniority", "seniority_level", "level"),
    "email": ("email", "email_address", "work_email", "e_mail", "mail", "contact_email"),
    "phone": (
        "phone",
        "phone_number",
        "telephone",
        "mobile",
        "mobile_number",
        "direct_phone",
        "contact_phone",
    ),
    "linkedin_url": ("linkedin_url", "linkedin", "linkedin_profile", "person_linkedin_url"),
    "company_name": (
        "company_name",
        "company",
        "organization",
        "organisation",
        "org_name",
        "employer",
        "account_name",
    ),
    "company_domain": ("company_domain", "domain", "website_domain", "primary_domain"),
    "company_website": ("company_website", "website", "url", "company_url", "web"),
    "company_industry": ("company_industry", "industry", "sector", "vertical"),
    "company_employee_count": (
        "company_employee_count",
        "employee_count",
        "employees",
        "company_size",
        "headcount",
        "num_employees",
        "staff_count",
    ),
    "company_country": ("company_country", "country", "country_name", "location_country"),
    "company_city": ("company_city", "city", "town", "location_city"),
    "company_linkedin_url": (
        "company_linkedin_url",
        "company_linkedin",
        "organization_linkedin_url",
    ),
    "external_id": ("external_id", "id", "record_id", "lead_id", "contact_id"),
    "source_url": ("source_url", "source", "origin_url", "profile_url"),
}

#: Recognized target fields, in a stable order for error messages.
_TARGET_FIELDS = tuple(_COLUMN_ALIASES)


def _sanitize_header(header: str) -> str:
    return _HEADER_SANITIZE_RE.sub("", header.strip().casefold())


def _build_header_index() -> dict[str, str]:
    index: dict[str, str] = {}
    for target, aliases in _COLUMN_ALIASES.items():
        for alias in aliases:
            index[_sanitize_header(alias)] = target
    return index


_HEADER_INDEX = _build_header_index()


def _parse_column_map(entries: list[str]) -> dict[str, str]:
    """Parse ``target=source_header`` overrides into a sanitized-header map.

    Raises:
        SourceNotFoundError: if an entry is malformed or names an unknown target.
    """
    overrides: dict[str, str] = {}
    for entry in entries:
        target, separator, source = entry.partition("=")
        target, source = target.strip(), source.strip()
        if not separator or not target or not source:
            raise SourceNotFoundError(
                "csv", f"invalid column_map entry {entry!r}; expected 'target_field=Source Header'"
            )
        if target not in _TARGET_FIELDS:
            raise SourceNotFoundError(
                "csv",
                f"column_map target {target!r} is not a known field; "
                f"valid targets: {', '.join(_TARGET_FIELDS)}",
            )
        overrides[_sanitize_header(source)] = target
    return overrides


@register_crawler
class CsvCrawler(BaseCrawler):
    """Reads leads from a delimited text file."""

    provider = "csv"
    display_name = "CSV file"
    description = "Read leads from a local CSV/TSV export."

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self._path: Path | None = settings.csv_source.path

    def is_available(self) -> tuple[bool, str]:
        if self._path is None:
            return False, "set --csv-path or LEAD_CSV_SOURCE__PATH"
        if not self._path.exists():
            return False, f"file not found: {self._path}"
        if not self._path.is_file():
            return False, f"not a file: {self._path}"
        return True, ""

    async def crawl(self, limit: int) -> list[RawLead]:
        if self._path is None:
            raise SourceNotFoundError(self.provider, "no CSV path configured")

        config = self.settings.csv_source
        # Parsing is CPU/IO-bound and synchronous; run it off the event loop so
        # concurrent crawlers are not blocked behind a large file.
        leads = await asyncio.to_thread(
            self._read,
            self._path,
            limit,
            config.delimiter,
            config.encoding,
            _parse_column_map(config.column_map),
        )
        self.logger.info(
            "read leads from csv",
            extra={"path": str(self._path), "count": len(leads)},
        )
        return leads

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #
    def _read(
        self,
        path: Path,
        limit: int,
        delimiter: str,
        encoding: str,
        overrides: dict[str, str],
    ) -> list[RawLead]:
        index = {**_HEADER_INDEX, **overrides}

        try:
            with path.open("r", encoding=encoding, newline="") as handle:
                sample = handle.read(8192)
                handle.seek(0)
                resolved_delimiter = self._resolve_delimiter(sample, delimiter, path)
                reader = csv.DictReader(handle, delimiter=resolved_delimiter)
                return self._parse_rows(reader, index, limit)
        except UnicodeDecodeError as exc:
            raise SourceNotFoundError(
                self.provider,
                f"{path} is not valid {encoding}; set LEAD_CSV_SOURCE__ENCODING "
                f"(e.g. latin-1) to match the file",
            ) from exc
        except OSError as exc:
            # is_available() checks the file exists, so a failure here is a
            # permission or device problem rather than a missing path. Named
            # with the path so the operator knows which file to look at.
            raise CrawlerError(self.provider, f"cannot read {path}: {exc}") from exc

    def _resolve_delimiter(self, sample: str, configured: str, path: Path) -> str:
        if configured.casefold() != "auto":
            return configured
        try:
            return csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
        except csv.Error:
            self.logger.warning(
                "could not sniff delimiter, falling back to comma",
                extra={"path": str(path)},
            )
            return ","

    def _parse_rows(self, reader: Any, index: dict[str, str], limit: int) -> list[RawLead]:
        if reader.fieldnames is None:
            return []

        # Resolve each source column to a target field once per file.
        column_targets: dict[str, str] = {}
        for header in reader.fieldnames:
            if header is None:
                continue
            target = index.get(_sanitize_header(header))
            # `email` and `email_address` may both be present; first wins.
            if target and target not in column_targets.values():
                column_targets[header] = target

        if not column_targets:
            self.logger.warning(
                "no recognizable columns in csv; every row will be treated as unidentifiable",
                extra={"headers": list(reader.fieldnames)},
            )

        rows = self._numbered_rows(reader)
        leads, unmappable = self.map_records(
            rows,
            lambda item: self._row_to_lead(item[1], column_targets, item[0]),
            kind="csv row",
            label=lambda item: f"row {item[0]}",
            limit=limit,
        )
        if unmappable:
            self.logger.warning(
                "skipped unreadable csv rows",
                extra={"path": str(self._path), "rows": unmappable},
            )
        return leads

    def _numbered_rows(self, reader: Any) -> Iterator[tuple[int, dict[str, Any]]]:
        """Data rows paired with their line number, read lazily.

        Reading stops at the underlying reader's end or at the first
        :exc:`csv.Error`, which the csv module raises from the middle of the
        stream (a field over its size limit, a stray quote) and from which it
        cannot resume. A file that trips that is still worth every row before
        the break: a 50 000-row export with one pathological cell should yield
        49 999 leads and a warning, not nothing at all.

        Lazy so that the caller's row limit — which counts *leads*, and a blank
        row is not one — stops the read rather than being applied afterwards to
        a file already held in memory.
        """
        # Row 1 is the header, so the first data row is 2. Counted here rather
        # than by `enumerate` so that when `__next__` is what raised, the number
        # still points at the row that broke rather than the one before it.
        row_number = 1
        try:
            for row in reader:
                row_number += 1
                yield row_number, row
        except csv.Error as exc:
            self.logger.warning(
                "stopped reading csv at a malformed row",
                extra={
                    "path": str(self._path),
                    "row": row_number + 1,
                    "error": str(exc),
                },
            )

    def _row_to_lead(
        self, row: dict[str, Any], column_targets: dict[str, str], row_number: int
    ) -> RawLead | None:
        mapped: dict[str, Any] = {}
        for header, target in column_targets.items():
            value = row.get(header)
            if isinstance(value, str):
                value = value.strip()
            if value not in (None, ""):
                mapped.setdefault(target, value)

        # Skip rows that are entirely blank rather than emitting an empty lead.
        if not mapped:
            return None

        # Preserve every original column so nothing is silently lost.
        raw = {
            str(key): value
            for key, value in row.items()
            if key is not None and value not in (None, "")
        }

        return RawLead(
            provider=self.provider,
            external_id=str(mapped["external_id"]) if "external_id" in mapped else None,
            source_url=mapped.get("source_url"),
            first_name=mapped.get("first_name"),
            last_name=mapped.get("last_name"),
            full_name=mapped.get("full_name"),
            job_title=mapped.get("job_title"),
            seniority=mapped.get("seniority"),
            email=mapped.get("email"),
            phone=mapped.get("phone"),
            linkedin_url=mapped.get("linkedin_url"),
            company_name=mapped.get("company_name"),
            company_domain=mapped.get("company_domain"),
            company_website=mapped.get("company_website"),
            company_industry=mapped.get("company_industry"),
            company_employee_count=mapped.get("company_employee_count"),
            company_country=mapped.get("company_country"),
            company_city=mapped.get("company_city"),
            company_linkedin_url=mapped.get("company_linkedin_url"),
            raw={**raw, "_row_number": row_number},
        )
