"""Module 3: detect whether a co-author has a paper at the target conference.

Conferences are frequently NOT yet indexed in Google Scholar before the event,
so we consult multiple sources per co-author in priority order and stop as soon
as we find strong (CONFIRMED-grade) evidence:

1. The co-author's Scholar publications (venue / citation text).
2. Their homepage / lab page (scraped for "Accepted to ECCV 2026" etc.).
3. arXiv search by author name (abstract + comments).
4. A general web search via SerpAPI (optional).

Every positive hit carries an :class:`Evidence` snippet; we NEVER report a
match without one.

Precision rules (to avoid the obvious false positives):
* The exact target year must appear next to the acronym -- a bare "ECCV" or a
  different year ("ECCV2024") does NOT count.
* The surrounding context is classified: an explicit acceptance phrase scores
  high; a role/site/submission context ("Area Chair", "reviewer", "Call for",
  "submitted to", conference websites) is heavily down-ranked.
* For name-based sources (web search) the co-author's name must appear near the
  conference mention.
"""

from __future__ import annotations

import logging
import re
from typing import List, Optional, Tuple

from models import Conference, Evidence
from normalize import normalize_name
from scholar_client import ScholarBackend

logger = logging.getLogger(__name__)

# Strength weight per source, used by the disambiguator.
SOURCE_WEIGHT = {
    "scholar_pub": 1.0,
    "homepage": 0.85,
    "arxiv": 0.8,
    "web_search": 0.6,
}

# A hit at or above this score from a strong source ends the search early.
CONFIRM_SCORE = 0.95

# Context windows (chars on each side of a conference mention).
_WINDOW = 170

# Explicit acceptance signals (forthcoming or indexed). Includes a few CJK terms.
_ACCEPT_KEYWORDS = (
    "accepted to", "accepted at", "accepted by", "accepted as", "accepted in",
    "is accepted", "are accepted", "was accepted", "were accepted",
    "has been accepted", "have been accepted", "to appear", "camera-ready",
    "camera ready", "our paper", "录用", "接收", "已接收", "收录",
)

# Contexts that indicate service / submission / a generic listing, NOT an
# accepted paper by this person.
_NEGATIVE_KEYWORDS = (
    "area chair", "senior area chair", "reviewer", "review for", "reviewing",
    "program committee", "pc member", "senior pc", "organizer", "organizing",
    "call for", "submission", "submitted to", "on submission", "under review",
    "serve as", "serving as", "invited to serve", "workshop", "tutorial",
    "keynote", "steering committee", "program chair", "general chair",
    "chair for", "审稿", "投稿", "在投",
)

# Domains that are conference/aggregator sites rather than a person's own page.
_SITE_DOMAINS = (
    "ecva.net", "thecvf.com", "visionbib.com", "myhuiban.com", "wikicfp.com",
    "dblp.org", "aideadlines", "conferences.", "openreview.net",
)

_ACRONYM_RE = re.compile(r"([A-Za-z]{2,})")
_YEAR_TOKEN_RE = re.compile(r"(?:19|20)\d{2}")

# How close (chars) a name token must sit to the conference mention to "bind".
_NAME_BIND_DIST = 150


def parse_conference(raw: str) -> Conference:
    """Parse a fuzzy conference string into a normalized :class:`Conference`.

    Accepts variants like ``"ECCV 2026"``, ``"eccv2026"``, ``"ECCV'26"``.
    """
    text = raw.strip()
    acr_match = _ACRONYM_RE.search(text)
    acronym = acr_match.group(1).upper() if acr_match else text.upper()

    year: Optional[int] = None
    full = re.search(r"(?:19|20)\d{2}", text)
    if full:
        year = int(full.group(0))
    else:
        short = re.search(r"['’]?(\d{2})\b", text.replace(acronym, "", 1))
        if short:
            yy = int(short.group(1))
            year = 2000 + yy if yy < 70 else 1900 + yy

    variants = _build_variants(acronym, year)
    return Conference(raw=raw, acronym=acronym, year=year, variants=variants)


def _build_variants(acronym: str, year: Optional[int]) -> List[str]:
    """Human-readable variants (used only for display, not for matching)."""
    variants = set()
    if year is not None:
        yy = str(year)[-2:]
        variants.update(
            {
                f"{acronym} {year}",
                f"{acronym}{year}",
                f"{acronym} '{yy}",
                f"{acronym}'{yy}",
            }
        )
    else:
        variants.add(acronym)
    return sorted(variants, key=len, reverse=True)


def build_conference_regex(conf: Conference) -> "re.Pattern[str]":
    """Compile a precise acronym+year regex.

    Requires the acronym immediately followed (allowing separators) by the
    *exact* target year, so "ECCV2024" or a bare "ECCV" never match.
    """
    acr = re.escape(conf.acronym)
    if conf.year is not None:
        yy = str(conf.year)[-2:]
        # Full year (not followed by another digit) or apostrophe + 2 digits.
        year_alt = r"(?:{full}(?!\d)|['’]{yy}(?!\d))".format(full=conf.year, yy=yy)
    else:
        year_alt = r"(?:19|20)\d{2}(?!\d)"
    pattern = r"\b{acr}[\s\-:'’]*{year}".format(acr=acr, year=year_alt)
    return re.compile(pattern, re.IGNORECASE)


def _classify_context(window: str) -> float:
    """Score the context around a conference mention in ``[0, 1]``."""
    if any(k in window for k in _NEGATIVE_KEYWORDS):
        return 0.2
    if any(k in window for k in _ACCEPT_KEYWORDS):
        return 1.0
    return 0.5


class ConferenceMatcher:
    """Runs the multi-source acceptance search for a single conference."""

    def __init__(
        self,
        backend: ScholarBackend,
        conf: Conference,
        enable_web_search: bool = True,
        timeout: float = 20.0,
    ) -> None:
        self.backend = backend
        self.conf = conf
        self.enable_web_search = enable_web_search
        self.timeout = timeout
        self._conf_re = build_conference_regex(conf)

    def find_evidence(
        self, name: str, scholar_id: Optional[str], homepage: Optional[str]
    ) -> List[Evidence]:
        """Collect evidence from all sources, stopping early on a strong hit."""
        evidence: List[Evidence] = []
        sources = [
            ("scholar_pub", lambda: self._from_scholar_pubs(scholar_id)),
            ("homepage", lambda: self._from_homepage(name, homepage)),
            ("arxiv", lambda: self._from_arxiv(name)),
            ("web_search", lambda: self._from_web_search(name)),
        ]
        for source_name, fn in sources:
            try:
                hits = fn()
            except Exception as exc:  # noqa: BLE001 - degrade gracefully
                logger.warning("Source '%s' failed for %s: %s", source_name, name, exc)
                continue
            evidence.extend(hits)
            if any(h.raw_score >= CONFIRM_SCORE for h in hits) and \
                    SOURCE_WEIGHT.get(source_name, 0) >= 0.85:
                logger.debug("Strong hit from %s for %s; stopping early.",
                             source_name, name)
                break
        return evidence

    def _evaluate(
        self,
        text: str,
        name: str,
        url: Optional[str] = None,
        own_context: bool = False,
    ) -> Optional[Tuple[float, str]]:
        """Return ``(score, snippet)`` for the best conference mention, or None.

        ``own_context`` marks sources where the text already belongs to the
        person (their publication list / homepage / arXiv paper), so name
        proximity is implied. For web search the name must be *bound* to the
        specific conference mention (see :meth:`_name_bound`).
        """
        if not text:
            return None
        low = text.lower()
        name_tokens = [t for t in normalize_name(name).split() if len(t) >= 2]
        site_penalty = 0.35 if (url and any(d in url.lower() for d in _SITE_DOMAINS)) else 1.0

        best: Optional[Tuple[float, str]] = None
        for m in self._conf_re.finditer(text):
            s, e = m.start(), m.end()
            w_start, w_end = max(0, s - _WINDOW), min(len(text), e + _WINDOW)
            window = low[w_start:w_end]

            ctx = _classify_context(window)
            name_near = own_context or self._name_bound(low, name_tokens, m)
            name_factor = 1.0 if name_near else 0.35
            score = ctx * name_factor * site_penalty

            if best is None or score > best[0]:
                snippet = re.sub(r"\s+", " ", text[w_start:w_end].strip())
                best = (score, snippet)
        return best

    def _name_bound(self, low: str, name_tokens: List[str], m: "re.Match[str]") -> bool:
        """Whether the co-author's name is tied to *this* conference mention.

        Every name token (of length >= 2) must occur within ``_NAME_BIND_DIST``
        chars of the mention, with no *different* year between the token and the
        mention. This prevents a name that belongs to a neighbouring "ECCV 2024"
        entry from being credited to a separate "ECCV 2026" announcement.
        """
        if not name_tokens:
            return False
        s, e = m.start(), m.end()
        target = self.conf.year
        for tok in name_tokens:
            bound_here = False
            for nm in re.finditer(r"\b" + re.escape(tok) + r"\b", low):
                ts, te = nm.start(), nm.end()
                if te <= s:
                    gap, dist = low[te:s], s - te
                elif ts >= e:
                    gap, dist = low[e:ts], ts - e
                else:
                    gap, dist = "", 0
                if dist > _NAME_BIND_DIST:
                    continue
                if target is not None and any(
                    int(y.group(0)) != target for y in _YEAR_TOKEN_RE.finditer(gap)
                ):
                    continue
                bound_here = True
                break
            if not bound_here:
                return False
        return True

    def _from_scholar_pubs(self, scholar_id: Optional[str]) -> List[Evidence]:
        if not scholar_id:
            return []
        author = self.backend.get_author(scholar_id)
        out: List[Evidence] = []
        for pub in author.get("publications", []) or []:
            bib = pub.get("bib", {}) or {}
            blob = " ".join(
                str(bib.get(k, "")) for k in ("venue", "citation", "journal", "title")
            )
            res = self._evaluate(blob, "", own_context=True)
            if res and res[0] > 0:
                out.append(
                    Evidence(
                        source="scholar_pub",
                        url=pub.get("pub_url") or author.get("scholar_id"),
                        snippet=res[1],
                        raw_score=res[0] * SOURCE_WEIGHT["scholar_pub"],
                    )
                )
        return out

    def _from_homepage(self, name: str, homepage: Optional[str]) -> List[Evidence]:
        if not homepage:
            return []
        try:
            import requests  # type: ignore
            from bs4 import BeautifulSoup  # type: ignore
        except ImportError as exc:  # pragma: no cover - dependency guard
            logger.warning("homepage scraping needs requests + beautifulsoup4: %s", exc)
            return []
        try:
            resp = requests.get(
                homepage,
                timeout=self.timeout,
                headers={"User-Agent": "Mozilla/5.0 (compatible; CoauthorFinder/1.0)"},
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to fetch homepage %s: %s", homepage, exc)
            return []
        if resp.status_code != 200:
            return []
        text = BeautifulSoup(resp.text, "html.parser").get_text(" ", strip=True)
        res = self._evaluate(text, name, url=homepage, own_context=True)
        if not res or res[0] <= 0:
            return []
        return [
            Evidence(
                source="homepage",
                url=homepage,
                snippet=res[1],
                raw_score=res[0] * SOURCE_WEIGHT["homepage"],
            )
        ]

    def _from_arxiv(self, name: str) -> List[Evidence]:
        try:
            import arxiv  # type: ignore
        except ImportError as exc:  # pragma: no cover - dependency guard
            logger.warning("arXiv search needs the 'arxiv' package: %s", exc)
            return []
        out: List[Evidence] = []
        try:
            search = arxiv.Search(
                query=f'au:"{name}"',
                max_results=15,
                sort_by=arxiv.SortCriterion.SubmittedDate,
            )
            results = list(search.results())
        except Exception as exc:  # noqa: BLE001
            logger.warning("arXiv search failed for %s: %s", name, exc)
            return []
        for r in results:
            comment = getattr(r, "comment", "") or ""
            blob = " ".join([r.title or "", r.summary or "", comment])
            # The comment field is where authors note acceptances; require an
            # acceptance context there (own_context, since it is their paper).
            res = self._evaluate(blob, name, url=getattr(r, "entry_id", None),
                                 own_context=True)
            if res and res[0] > 0:
                out.append(
                    Evidence(
                        source="arxiv",
                        url=getattr(r, "entry_id", None),
                        snippet=res[1],
                        raw_score=res[0] * SOURCE_WEIGHT["arxiv"],
                    )
                )
        return out

    def _from_web_search(self, name: str) -> List[Evidence]:
        # ``web_search`` may be provided directly by a SerpApiBackend or proxied
        # through a caching wrapper, so we feature-detect rather than isinstance.
        web_search = getattr(self.backend, "web_search", None)
        if not self.enable_web_search or not callable(web_search):
            return []
        query = f'"{name}" "{self.conf.display}"'
        results = web_search(query, limit=10)
        out: List[Evidence] = []
        for r in results:
            blob = " ".join([r.get("title", ""), r.get("snippet", "")])
            # Name must appear near the conference mention (own_context=False).
            res = self._evaluate(blob, name, url=r.get("link"), own_context=False)
            if res and res[0] > 0:
                out.append(
                    Evidence(
                        source="web_search",
                        url=r.get("link"),
                        snippet=res[1],
                        raw_score=res[0] * SOURCE_WEIGHT["web_search"],
                    )
                )
        return out
