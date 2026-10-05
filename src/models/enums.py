"""Closed vocabularies used by the pipeline.

Only values the *processing* logic branches on live here. In particular the
source ``provider`` is a plain string rather than an enum: adding a new data
source must never require editing a shared enum, which is the whole point of the
adapter architecture.
"""

from __future__ import annotations

from enum import StrEnum


class SeniorityLevel(StrEnum):
    """Normalized seniority, inferred from a job title when not supplied."""

    FOUNDER = "founder"
    C_SUITE = "c_suite"
    VP = "vp"
    DIRECTOR = "director"
    MANAGER = "manager"
    SENIOR = "senior"
    ENTRY = "entry"
    INTERN = "intern"
    UNKNOWN = "unknown"

    @classmethod
    def coerce(cls, value: object) -> SeniorityLevel:
        """Parse a value tolerantly, falling back to :attr:`UNKNOWN`.

        Accepts the enum itself, the string value, or the member name so that
        ``"C_SUITE"`` and ``"c_suite"`` both resolve.
        """
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            candidate = value.strip().casefold().replace("-", "_").replace(" ", "_")
            for member in cls:
                if candidate in (member.value, member.name.casefold()):
                    return member
            # Common aliases seen in upstream data.
            aliases = {
                "executive": cls.C_SUITE,
                "cxo": cls.C_SUITE,
                "chief": cls.C_SUITE,
                "ceo": cls.C_SUITE,
                "cto": cls.C_SUITE,
                "cfo": cls.C_SUITE,
                "coo": cls.C_SUITE,
                "cmo": cls.C_SUITE,
                "cro": cls.C_SUITE,
                "cio": cls.C_SUITE,
                "cpo": cls.C_SUITE,
                "owner": cls.FOUNDER,
                "cofounder": cls.FOUNDER,
                "partner": cls.FOUNDER,
                "vice_president": cls.VP,
                "svp": cls.VP,
                "evp": cls.VP,
                "head": cls.DIRECTOR,
                "lead": cls.MANAGER,
                "principal": cls.SENIOR,
                "staff": cls.SENIOR,
                "junior": cls.ENTRY,
                "associate": cls.ENTRY,
                "trainee": cls.INTERN,
            }
            if candidate in aliases:
                return aliases[candidate]
        return cls.UNKNOWN


class ValidationSeverity(StrEnum):
    """How bad a validation finding is.

    Validation is asked to "reject **or mark**" a record, and the two are not
    the same judgement. A lead with no identity at all is unusable and must go.
    A lead whose company domain was garbled in the source is still perfectly
    addressable — dropping it would throw away good data to punish a bad field.
    """

    #: The record cannot be used; it is dropped with this reason recorded.
    ERROR = "error"
    #: The record is kept, and the finding is counted and reportable.
    WARNING = "warning"


class DedupStrategy(StrEnum):
    """How aggressively duplicate leads are collapsed.

    Each strategy is a prefix of a shared, ordered ladder of identity keys
    (strongest first): ``email`` -> ``linkedin`` -> ``phone_name`` ->
    ``name_domain`` -> ``name_company`` -> ``lastname_domain``.
    """

    #: Keep everything.
    NONE = "none"
    #: Collapse only on a shared email address.
    EMAIL = "email"
    #: Email, LinkedIn URL, or phone+name. Safe default for mixed sources.
    IDENTITY = "identity"
    #: The above plus name+company-domain and last-name+domain. Highest recall,
    #: some risk of merging two distinct people at the same company.
    AGGRESSIVE = "aggressive"


class ExportFormat(StrEnum):
    """Supported output encodings."""

    CSV = "csv"
    JSON = "json"
    JSONL = "jsonl"


class DuplicateKind(StrEnum):
    """How certain a duplicate match is.

    The distinction is the difference between "this is the same record" and
    "this is probably the same person". Both are collapsed, but only the first
    is a fact — and reporting them as one number would hide how much of a run's
    dedup was inference.
    """

    #: The same record: the source said so, or two sources gave a unique
    #: person-level identifier (email, LinkedIn profile).
    EXACT = "exact"
    #: The same person, most likely: a near-unique identifier confirmed by a
    #: name, or a name anchored to a company. Could be wrong.
    PROBABLE = "probable"


class RejectionReason(StrEnum):
    """Why a lead did not make it into the exported set."""

    NORMALIZATION_FAILED = "normalization_failed"
    #: A later stage (validation, filtering) raised on this record. Distinct from
    #: ``NORMALIZATION_FAILED`` so a processor bug is not misread as bad input.
    PROCESSING_FAILED = "processing_failed"
    VALIDATION_FAILED = "validation_failed"
    FILTERED_OUT = "filtered_out"
    DUPLICATE = "duplicate"


class LogFormat(StrEnum):
    """Log rendering style."""

    CONSOLE = "console"
    JSON = "json"
