"""Module 4: identity confidence scoring.

A name + a conference mention is weak on its own. This module combines identity
signals to produce a final confidence score and tier:

* CONFIRMED (>=0.85): a unique, active Scholar profile *and* explicit
  conference evidence.
* LIKELY (0.5-0.85): evidence found but the identity has minor ambiguity.
* UNCERTAIN (<0.5): name match only, or several same-name candidates (we then
  list all suspects).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Set

from models import (
    CoAuthor,
    Conference,
    Evidence,
    MatchResult,
    MatchStatus,
    status_for_confidence,
)
from normalize import normalize_name

logger = logging.getLogger(__name__)


def _tokens(text: Optional[str]) -> Set[str]:
    if not text:
        return set()
    return {t for t in normalize_name(text).split() if len(t) > 2}


def _overlap(a: Optional[str], b: Optional[str]) -> bool:
    ta, tb = _tokens(a), _tokens(b)
    return bool(ta and tb and (ta & tb))


def _interests_overlap(a: List[str], b: List[str]) -> bool:
    sa = {t for item in a for t in _tokens(item)}
    sb = {t for item in b for t in _tokens(item)}
    return bool(sa and sb and (sa & sb))


class Disambiguator:
    """Scores a co-author's conference match given identity signals."""

    def __init__(self, target: Dict[str, Any], conf: Conference) -> None:
        self.target = target
        self.conf = conf
        self.target_interests = target.get("interests", []) or []
        self.target_affiliation = target.get("affiliation", "")

    def assess(self, coauthor: CoAuthor, evidence: List[Evidence]) -> MatchResult:
        """Produce a final :class:`MatchResult` for ``coauthor``."""
        evidence = sorted(evidence, key=lambda e: e.raw_score, reverse=True)
        evidence_score = evidence[0].raw_score if evidence else 0.0

        confidence = evidence_score
        notes: List[str] = []
        suspected: List[str] = []

        has_unique_profile = bool(coauthor.scholar_id) and not coauthor.is_ambiguous
        collaborated = bool(coauthor.shared_papers)

        # --- Identity boosts ---
        if has_unique_profile:
            confidence += 0.05
        if collaborated:
            # Co-publishing with the target is the strongest identity signal.
            confidence += 0.10
        if self._affiliation_or_field_overlap(coauthor):
            confidence += 0.05

        # --- Identity penalties ---
        if coauthor.is_ambiguous:
            confidence *= 0.70
            suspected = [
                f"{c.name}"
                + (f" ({c.affiliation})" if c.affiliation else "")
                for c in coauthor.candidate_profiles
            ]
            notes.append(
                f"{len(coauthor.candidate_profiles)} same-name Scholar profiles; "
                "identity ambiguous."
            )
        elif not coauthor.scholar_id:
            # No resolvable profile -> rely on name-based evidence only.
            confidence *= 0.85
            notes.append("No Scholar profile resolved; matched by name only.")

        confidence = max(0.0, min(1.0, confidence))

        status = status_for_confidence(confidence)
        # Guard: CONFIRMED requires both a unique profile AND explicit evidence.
        if status == MatchStatus.CONFIRMED and not (has_unique_profile and evidence):
            status = MatchStatus.LIKELY
            confidence = min(confidence, 0.84)

        if not evidence:
            notes.append(
                f"No evidence of {self.conf.display} found in any source; the "
                "conference may not be indexed yet."
            )

        url = coauthor.profile_url or (evidence[0].url if evidence else None)
        return MatchResult(
            coauthor_name=coauthor.name,
            url=url,
            status=status,
            confidence=confidence,
            evidence=evidence,
            suspected_names=suspected,
            affiliation=coauthor.affiliation,
            note="; ".join(notes) if notes else None,
        )

    def _affiliation_or_field_overlap(self, coauthor: CoAuthor) -> bool:
        if _overlap(coauthor.affiliation, self.target_affiliation):
            return True
        for cand in coauthor.candidate_profiles:
            if _overlap(cand.affiliation, self.target_affiliation):
                return True
            if _interests_overlap(cand.interests, self.target_interests):
                return True
        return False
