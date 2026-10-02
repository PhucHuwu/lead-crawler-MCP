"""Typed domain model for the lead pipeline.

Import from this package rather than the submodules so the public surface stays
stable if the internal file layout changes.
"""

from src.models.company import Company
from src.models.enums import (
    DedupStrategy,
    ExportFormat,
    LogFormat,
    RejectionReason,
    SeniorityLevel,
)
from src.models.lead import LeadSource, RawLead, StandardizedLead, slugify_identity
from src.models.person import Person
from src.models.results import (
    MAX_RECORDED_REJECTIONS,
    CrawlResult,
    CrawlStats,
    RejectedLead,
)

__all__ = [
    "MAX_RECORDED_REJECTIONS",
    "Company",
    "CrawlResult",
    "CrawlStats",
    "DedupStrategy",
    "ExportFormat",
    "LeadSource",
    "LogFormat",
    "Person",
    "RawLead",
    "RejectedLead",
    "RejectionReason",
    "SeniorityLevel",
    "StandardizedLead",
    "slugify_identity",
]
