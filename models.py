"""Shared data models for the Conference Co-author Overlap Finder.

All dataclasses are JSON-serializable via :func:`to_dict` helpers so that the
orchestration layer can cache raw responses and emit the final report without
coupling to any particular backend.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Dict, List, Optional


class MatchStatus(str, Enum):
    """Confidence tier for a co-author conference match."""

    CONFIRMED = "CONFIRMED"
    LIKELY = "LIKELY"
    UNCERTAIN = "UNCERTAIN"


# Confidence thresholds shared across the pipeline.
CONFIRMED_THRESHOLD = 0.85
LIKELY_THRESHOLD = 0.5


def status_for_confidence(confidence: float) -> MatchStatus:
    """Map a numeric confidence score to a :class:`MatchStatus` tier."""
    if confidence >= CONFIRMED_THRESHOLD:
        return MatchStatus.CONFIRMED
    if confidence >= LIKELY_THRESHOLD:
        return MatchStatus.LIKELY
    return MatchStatus.UNCERTAIN


@dataclass
class Conference:
    """A normalized representation of a target conference query."""

    raw: str
    acronym: str
    year: Optional[int]
    variants: List[str] = field(default_factory=list)

    @property
    def display(self) -> str:
        """Human-friendly canonical name, e.g. ``ECCV 2026``."""
        if self.year is not None:
            return f"{self.acronym} {self.year}"
        return self.acronym

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ProfileCandidate:
    """A possible Google Scholar profile for a (possibly ambiguous) name."""

    scholar_id: str
    name: str
    affiliation: Optional[str] = None
    email_domain: Optional[str] = None
    homepage: Optional[str] = None
    interests: List[str] = field(default_factory=list)
    citedby: Optional[int] = None

    @property
    def profile_url(self) -> str:
        return f"https://scholar.google.com/citations?user={self.scholar_id}"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class CoAuthor:
    """A co-author of the target researcher.

    ``candidate_profiles`` holds every Scholar profile whose name matched; when
    more than one survives disambiguation the identity is ambiguous and we emit
    "suspects" instead of a single definitive match.
    """

    name: str
    normalized_name: str
    sources: List[str] = field(default_factory=list)  # "sidebar" and/or "papers"
    scholar_id: Optional[str] = None
    affiliation: Optional[str] = None
    homepage: Optional[str] = None
    shared_papers: List[str] = field(default_factory=list)
    candidate_profiles: List[ProfileCandidate] = field(default_factory=list)

    @property
    def profile_url(self) -> Optional[str]:
        if self.scholar_id:
            return f"https://scholar.google.com/citations?user={self.scholar_id}"
        return None

    @property
    def is_ambiguous(self) -> bool:
        return len(self.candidate_profiles) > 1

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["profile_url"] = self.profile_url
        data["is_ambiguous"] = self.is_ambiguous
        return data


@dataclass
class Evidence:
    """A single piece of evidence that a co-author is at the conference."""

    source: str  # "scholar_pub" | "homepage" | "arxiv" | "web_search"
    url: Optional[str]
    snippet: str
    raw_score: float  # fuzzy match score in [0, 1]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class MatchResult:
    """Final, report-ready record for one co-author."""

    coauthor_name: str
    url: Optional[str]
    status: MatchStatus
    confidence: float
    evidence: List[Evidence] = field(default_factory=list)
    suspected_names: List[str] = field(default_factory=list)
    affiliation: Optional[str] = None
    note: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "coauthor_name": self.coauthor_name,
            "url": self.url,
            "status": self.status.value,
            "confidence": round(self.confidence, 3),
            "evidence": [e.to_dict() for e in self.evidence],
            "suspected_names": self.suspected_names,
            "affiliation": self.affiliation,
            "note": self.note,
        }
