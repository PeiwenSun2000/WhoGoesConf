"""Module 2: build the target researcher's co-author set.

The Scholar sidebar "Co-authors" list is notoriously incomplete, so we union it
with every co-author parsed from the target's recent publications (last N
years). Names are normalized and deduplicated, and we attempt to resolve each
co-author's own Scholar profile id (keeping *all* candidates when a name is
ambiguous).
"""

from __future__ import annotations

import datetime
import hashlib
import json
import logging
import os
import re
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

from models import CoAuthor, ProfileCandidate
from normalize import names_match, normalize_name
from scholar_client import ScholarBackend

try:
    from rapidfuzz import fuzz as _fuzz
except Exception:  # pragma: no cover - import guard
    _fuzz = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

_USER_RE = re.compile(r"[?&]user=([^&]+)")
_TITLE_CLEAN_RE = re.compile(r"[^a-z0-9\s]")


def _normalize_title(title: str) -> str:
    """Lowercase a title and strip punctuation for robust comparison."""
    return re.sub(r"\s+", " ", _TITLE_CLEAN_RE.sub(" ", title.lower())).strip()


def _title_similarity(a: str, b: str) -> float:
    """Fuzzy similarity of two paper titles in ``[0, 1]``."""
    na, nb = _normalize_title(a), _normalize_title(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    if _fuzz is None:
        return 1.0 if na in nb or nb in na else 0.0
    return float(_fuzz.token_sort_ratio(na, nb)) / 100.0


def _is_abbreviated(normalized_name: str) -> bool:
    """True if a normalized name contains a single-letter (initial) token."""
    toks = normalized_name.split()
    return any(len(t) == 1 for t in toks) or len(toks) < 2


class ArxivNameResolver:
    """Resolve a paper's full author names by matching its title on arXiv.

    Google Scholar (and especially SerpAPI) often expose only abbreviated
    author strings ("Z Ye, P Sun, ..."). arXiv lists full author names, so we
    look up each paper by title and reuse its author list. Results (including
    misses) are cached on disk so re-runs do not re-query arXiv.
    """

    def __init__(
        self,
        cache_dir: Optional[str] = None,
        enabled: bool = True,
        title_threshold: float = 0.82,
        delay: float = 3.0,
    ) -> None:
        self.enabled = enabled
        self.title_threshold = title_threshold
        self.delay = delay
        self._mem: Dict[str, Optional[List[str]]] = {}
        self._cache_path = (
            os.path.join(cache_dir, "arxiv_fullnames.json") if cache_dir else None
        )
        self._client = None
        self._arxiv = None
        if self._cache_path and os.path.exists(self._cache_path):
            try:
                with open(self._cache_path, "r", encoding="utf-8") as fh:
                    self._mem = json.load(fh)
            except (OSError, json.JSONDecodeError):
                self._mem = {}

    def _ensure_client(self) -> bool:
        if self._client is not None:
            return True
        try:
            import arxiv  # type: ignore
        except ImportError as exc:  # pragma: no cover - dependency guard
            logger.warning("Full-name resolution needs the 'arxiv' package: %s", exc)
            return False
        self._arxiv = arxiv
        self._client = arxiv.Client(page_size=10, delay_seconds=self.delay,
                                     num_retries=2)
        return True

    @staticmethod
    def _cache_key(title: str) -> str:
        return hashlib.sha1(_normalize_title(title).encode("utf-8")).hexdigest()

    def _save(self) -> None:
        if not self._cache_path:
            return
        try:
            with open(self._cache_path, "w", encoding="utf-8") as fh:
                json.dump(self._mem, fh)
        except OSError as exc:  # pragma: no cover
            logger.debug("Could not persist arXiv name cache: %s", exc)

    def authors_for_title(self, title: str) -> Optional[List[str]]:
        """Return full author names for ``title`` from arXiv, or ``None``."""
        if not self.enabled or not title or len(title) < 8:
            return None
        key = self._cache_key(title)
        if key in self._mem:
            cached = self._mem[key]
            return cached or None
        result = self._query(title)
        self._mem[key] = result or []
        self._save()
        return result

    def _query(self, title: str) -> Optional[List[str]]:
        if not self._ensure_client():
            return None
        words = _normalize_title(title).split()[:14]
        if not words:
            return None
        query = 'ti:"{}"'.format(" ".join(words))
        try:
            search = self._arxiv.Search(query=query, max_results=8)
            results = list(self._client.results(search))
        except Exception as exc:  # noqa: BLE001 - degrade gracefully
            logger.warning("arXiv title lookup failed for %r: %s", title[:60], exc)
            return None
        best: Optional[List[str]] = None
        best_score = self.title_threshold
        for r in results:
            score = _title_similarity(getattr(r, "title", ""), title)
            if score >= best_score:
                best_score = score
                best = [a.name for a in getattr(r, "authors", []) if a.name]
        if best:
            logger.debug("Resolved %d full names for %r (score %.2f)",
                         len(best), title[:50], best_score)
        return best


def parse_scholar_id(scholar_url: str) -> str:
    """Extract the ``user=XXXX`` id from a Google Scholar profile URL.

    Raises ``ValueError`` when no id is present.
    """
    parsed = urlparse(scholar_url)
    qs = parse_qs(parsed.query)
    if "user" in qs and qs["user"]:
        return qs["user"][0]
    match = _USER_RE.search(scholar_url)
    if match:
        return match.group(1)
    raise ValueError(
        f"Could not find a 'user=' id in the Scholar URL: {scholar_url!r}"
    )


def _split_authors(author_field: Any) -> List[str]:
    """Split a publication's author field into individual name strings.

    scholarly stores authors as an ``" and "``-joined string; SerpAPI may store
    a list of dicts or a comma-joined string.
    """
    if not author_field:
        return []
    if isinstance(author_field, list):
        names: List[str] = []
        for item in author_field:
            if isinstance(item, dict):
                names.append(item.get("name", ""))
            else:
                names.append(str(item))
        return [n.strip() for n in names if n and n.strip()]
    text = str(author_field)
    # Normalize common separators to " and ".
    text = text.replace(";", " and ").replace(",", " and ") if " and " not in text else text
    parts = re.split(r"\s+and\s+", text)
    return [p.strip() for p in parts if p.strip()]


def _publication_year(pub: Dict[str, Any]) -> Optional[int]:
    bib = pub.get("bib", {}) or {}
    year = bib.get("pub_year") or bib.get("year")
    if year is None:
        return None
    try:
        return int(str(year)[:4])
    except (ValueError, TypeError):
        return None


class CoAuthorExtractor:
    """Collects and normalizes the target's co-authors."""

    def __init__(
        self,
        backend: ScholarBackend,
        years: int = 3,
        name_resolver: Optional[ArxivNameResolver] = None,
    ) -> None:
        self.backend = backend
        self.years = years
        self.name_resolver = name_resolver

    def extract(self, scholar_url: str) -> Dict[str, Any]:
        """Return ``{"target": <author dict>, "coauthors": List[CoAuthor]}``."""
        author_id = parse_scholar_id(scholar_url)
        logger.info("Fetching target profile user=%s", author_id)
        target = self.backend.get_author(author_id)

        merged: Dict[str, CoAuthor] = {}
        self._add_sidebar_coauthors(target, merged)
        self._add_paper_coauthors(target, merged)

        # Don't include the target as their own co-author.
        target_key = normalize_name(target.get("name", ""))
        merged.pop(target_key, None)

        self._merge_abbreviated(merged)

        coauthors = list(merged.values())
        n_abbrev = sum(_is_abbreviated(c.normalized_name) for c in coauthors)
        logger.info("Collected %d unique co-authors (%d still abbreviated).",
                    len(coauthors), n_abbrev)
        self._resolve_profiles(coauthors)
        return {"target": target, "coauthors": coauthors}

    def _upsert(
        self,
        merged: Dict[str, CoAuthor],
        name: str,
        source: str,
        scholar_id: Optional[str] = None,
        affiliation: Optional[str] = None,
        paper_title: Optional[str] = None,
    ) -> None:
        if not name or not name.strip():
            return
        key = normalize_name(name)
        if not key:
            return
        existing = merged.get(key)
        if existing is None:
            existing = CoAuthor(name=name, normalized_name=key)
            merged[key] = existing
        if source not in existing.sources:
            existing.sources.append(source)
        if scholar_id and not existing.scholar_id:
            existing.scholar_id = scholar_id
        if affiliation and not existing.affiliation:
            existing.affiliation = affiliation
        if paper_title and paper_title not in existing.shared_papers:
            existing.shared_papers.append(paper_title)
        # Prefer the most complete (longest) display name.
        if len(name) > len(existing.name):
            existing.name = name

    def _add_sidebar_coauthors(
        self, target: Dict[str, Any], merged: Dict[str, CoAuthor]
    ) -> None:
        for ca in target.get("coauthors", []) or []:
            self._upsert(
                merged,
                name=ca.get("name", ""),
                source="sidebar",
                scholar_id=ca.get("scholar_id"),
                affiliation=ca.get("affiliation"),
            )

    def _add_paper_coauthors(
        self, target: Dict[str, Any], merged: Dict[str, CoAuthor]
    ) -> None:
        current_year = datetime.date.today().year
        cutoff = current_year - self.years
        target_name = target.get("name", "")
        pubs = target.get("publications", []) or []
        for pub in pubs:
            year = _publication_year(pub)
            # Keep papers with no year (often very recent / "to appear").
            if year is not None and year < cutoff:
                continue
            filled = pub
            bib = pub.get("bib", {}) or {}
            authors = _split_authors(bib.get("author"))
            if not authors:
                # Author list may require filling the publication stub.
                try:
                    filled = self.backend.fill_publication(pub)
                    bib = filled.get("bib", {}) or {}
                    authors = _split_authors(bib.get("author"))
                except Exception as exc:  # noqa: BLE001 - degrade gracefully
                    logger.warning("Could not fill publication: %s", exc)
                    continue
            title = bib.get("title", "")
            # Prefer arXiv's full author names over abbreviated Scholar strings.
            if self.name_resolver and title:
                full_authors = self.name_resolver.authors_for_title(title)
                if full_authors:
                    authors = full_authors
            for author_name in authors:
                if names_match(author_name, target_name):
                    continue
                self._upsert(
                    merged,
                    name=author_name,
                    source="papers",
                    paper_title=title,
                )

    def _merge_abbreviated(self, merged: Dict[str, CoAuthor]) -> None:
        """Fold abbreviated entries into full-name ones when they clearly match.

        E.g. "Z Ye" (from a paper not found on arXiv) is merged into "Zhen Ye"
        (recovered via arXiv). Skips merges that are ambiguous (more than one
        full-name candidate matches).
        """
        full = [c for c in merged.values() if not _is_abbreviated(c.normalized_name)]
        if not full:
            return
        for ca in list(merged.values()):
            if not _is_abbreviated(ca.normalized_name):
                continue
            matches = [f for f in full if f is not ca and names_match(ca.name, f.name)]
            if len(matches) != 1:
                continue
            target = matches[0]
            for src in ca.sources:
                if src not in target.sources:
                    target.sources.append(src)
            for paper in ca.shared_papers:
                if paper not in target.shared_papers:
                    target.shared_papers.append(paper)
            if not target.scholar_id and ca.scholar_id:
                target.scholar_id = ca.scholar_id
            if not target.affiliation and ca.affiliation:
                target.affiliation = ca.affiliation
            merged.pop(ca.normalized_name, None)

    def _resolve_profiles(self, coauthors: List[CoAuthor]) -> None:
        """Attempt to resolve a Scholar profile id for each co-author.

        All name-matching candidates are retained so the disambiguator can flag
        ambiguous identities. Co-authors that already have an id from the
        sidebar are skipped to save quota.
        """
        for ca in coauthors:
            if ca.scholar_id:
                continue
            try:
                candidates = self.backend.search_author_by_name(ca.name, limit=5)
            except Exception as exc:  # noqa: BLE001 - degrade gracefully
                logger.warning("Profile search failed for %s: %s", ca.name, exc)
                continue
            matched: List[ProfileCandidate] = []
            for c in candidates:
                if not names_match(c.get("name", ""), ca.name):
                    continue
                matched.append(
                    ProfileCandidate(
                        scholar_id=c.get("scholar_id") or "",
                        name=c.get("name", ""),
                        affiliation=c.get("affiliation"),
                        email_domain=c.get("email_domain"),
                        homepage=c.get("homepage"),
                        interests=c.get("interests", []) or [],
                        citedby=c.get("citedby"),
                    )
                )
            ca.candidate_profiles = matched
            if len(matched) == 1:
                ca.scholar_id = matched[0].scholar_id
                ca.affiliation = ca.affiliation or matched[0].affiliation
                ca.homepage = ca.homepage or matched[0].homepage
            elif len(matched) > 1:
                logger.info(
                    "Ambiguous identity for '%s': %d candidate profiles.",
                    ca.name, len(matched),
                )
