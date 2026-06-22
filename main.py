"""Module 5: CLI orchestration and reporting.

Wires the pipeline together:

    parse conference -> build (cached) backend -> extract co-authors ->
    match each co-author against the conference -> disambiguate -> report.

Emits both ``report.json`` and a console + file Markdown table. A ``--demo``
mode renders mock data with zero network calls so you can preview the output
format before running live.

Usage::

    python main.py --scholar-url "https://scholar.google.com/citations?user=XXXX" \
                    --conference "ECCV 2026"
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
from typing import Any, Dict, List, Optional

from models import (
    CoAuthor,
    Conference,
    Evidence,
    MatchResult,
    MatchStatus,
)

logger = logging.getLogger("coauthor_finder")

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")

# Order used when sorting the report: strongest matches first.
_STATUS_ORDER = {
    MatchStatus.CONFIRMED: 0,
    MatchStatus.LIKELY: 1,
    MatchStatus.UNCERTAIN: 2,
}


# --------------------------------------------------------------------------- #
# Caching backend wrapper
# --------------------------------------------------------------------------- #
class CachingBackend:
    """Read-through JSON cache around any :class:`ScholarBackend`.

    Each method's result is cached in ``cache/<sha1>.json`` keyed by the backend
    name, method name and arguments, so re-runs never re-hit the network.
    """

    def __init__(self, backend: Any, cache_dir: str = CACHE_DIR, enabled: bool = True):
        self.backend = backend
        self.cache_dir = cache_dir
        self.enabled = enabled
        if enabled:
            os.makedirs(cache_dir, exist_ok=True)

    def _key(self, method: str, *args: Any) -> str:
        raw = json.dumps(
            [getattr(self.backend, "name", "backend"), method, args],
            sort_keys=True,
            default=str,
        )
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    def _path(self, key: str) -> str:
        return os.path.join(self.cache_dir, f"{key}.json")

    def _cached(self, method: str, args: tuple, producer) -> Any:
        if not self.enabled:
            return producer()
        key = self._key(method, *args)
        path = self._path(key)
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    logger.debug("Cache hit for %s%s", method, args)
                    return json.load(fh)
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning("Failed to read cache %s: %s", path, exc)
        result = producer()
        try:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(result, fh, default=str)
        except (OSError, TypeError) as exc:
            logger.warning("Failed to write cache %s: %s", path, exc)
        return result

    # Delegated, cached methods --------------------------------------------- #
    def get_author(self, author_id: str) -> Dict[str, Any]:
        return self._cached("get_author", (author_id,),
                            lambda: self.backend.get_author(author_id))

    def fill_publication(self, publication: Dict[str, Any]) -> Dict[str, Any]:
        # Keyed by title to keep the cache stable across runs.
        title = (publication.get("bib", {}) or {}).get("title", "")
        return self._cached("fill_publication", (title,),
                            lambda: self.backend.fill_publication(publication))

    def search_author_by_name(self, name: str, limit: int = 5) -> List[Dict[str, Any]]:
        return self._cached("search_author_by_name", (name, limit),
                            lambda: self.backend.search_author_by_name(name, limit))

    def search_pubs(self, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        return self._cached("search_pubs", (query, limit),
                            lambda: self.backend.search_pubs(query, limit))

    def web_search(self, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        if not hasattr(self.backend, "web_search"):
            return []
        return self._cached("web_search", (query, limit),
                            lambda: self.backend.web_search(query, limit))


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
def build_report(
    target: Dict[str, Any], conf: Conference, results: List[MatchResult]
) -> Dict[str, Any]:
    """Assemble the JSON-serializable report payload."""
    summary = {
        "confirmed": sum(r.status == MatchStatus.CONFIRMED for r in results),
        "likely": sum(r.status == MatchStatus.LIKELY for r in results),
        "uncertain": sum(r.status == MatchStatus.UNCERTAIN for r in results),
        "total_coauthors": len(results),
    }
    return {
        "target": {
            "name": target.get("name", ""),
            "scholar_id": target.get("scholar_id", ""),
            "affiliation": target.get("affiliation", ""),
        },
        "conference": conf.to_dict(),
        "summary": summary,
        "results": [r.to_dict() for r in results],
    }


def render_markdown(
    target: Dict[str, Any], conf: Conference, results: List[MatchResult]
) -> str:
    """Render a human-readable Markdown report."""
    lines: List[str] = []
    lines.append(f"# Co-authors of {target.get('name', 'target')} at {conf.display}")
    lines.append("")
    confirmed = sum(r.status == MatchStatus.CONFIRMED for r in results)
    likely = sum(r.status == MatchStatus.LIKELY for r in results)
    uncertain = sum(r.status == MatchStatus.UNCERTAIN for r in results)
    lines.append(
        f"**Summary:** {confirmed} CONFIRMED, {likely} LIKELY, "
        f"{uncertain} UNCERTAIN out of {len(results)} co-authors."
    )
    lines.append("")
    lines.append("| Co-author | Profile / URL | Status | Confidence | Evidence | Snippet |")
    lines.append("|-----------|---------------|--------|-----------|----------|---------|")
    for r in results:
        ev = r.evidence[0] if r.evidence else None
        source = ev.source if ev else "-"
        snippet = (ev.snippet[:90] + "...") if ev and len(ev.snippet) > 90 else (
            ev.snippet if ev else "-"
        )
        snippet = snippet.replace("|", "\\|").replace("\n", " ")
        url = r.url or "-"
        url_cell = f"[link]({url})" if url and url != "-" else "-"
        name_cell = r.coauthor_name
        if r.suspected_names:
            name_cell += f"<br/>suspects: {', '.join(r.suspected_names)}"
        lines.append(
            f"| {name_cell} | {url_cell} | {r.status.value} | "
            f"{r.confidence:.2f} | {source} | {snippet} |"
        )
    lines.append("")
    notes = [r for r in results if r.note]
    if notes:
        lines.append("## Notes")
        for r in notes:
            lines.append(f"- **{r.coauthor_name}**: {r.note}")
        lines.append("")
    return "\n".join(lines)


def sort_results(results: List[MatchResult]) -> List[MatchResult]:
    return sorted(
        results,
        key=lambda r: (_STATUS_ORDER[r.status], -r.confidence, r.coauthor_name.lower()),
    )


def write_outputs(report: Dict[str, Any], markdown: str, json_path: str, md_path: str) -> None:
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, ensure_ascii=False)
    with open(md_path, "w", encoding="utf-8") as fh:
        fh.write(markdown)
    logger.info("Wrote %s and %s", json_path, md_path)


# --------------------------------------------------------------------------- #
# Demo (mock) data
# --------------------------------------------------------------------------- #
def _demo_dataset() -> Dict[str, Any]:
    """Mock target + results illustrating every status tier and edge case."""
    target = {
        "name": "Alex Researcher",
        "scholar_id": "DEMO1234",
        "affiliation": "Vision Lab, Example University",
        "interests": ["computer vision", "3d reconstruction"],
    }
    conf = Conference(
        raw="ECCV 2026",
        acronym="ECCV",
        year=2026,
        variants=["ECCV 2026", "ECCV2026", "ECCV '26", "ECCV'26", "ECCV 26", "ECCV"],
    )
    results = [
        MatchResult(
            coauthor_name="Jane Doe",
            url="https://scholar.google.com/citations?user=JANE9999",
            status=MatchStatus.CONFIRMED,
            confidence=0.93,
            evidence=[
                Evidence(
                    source="scholar_pub",
                    url="https://scholar.google.com/citations?user=JANE9999",
                    snippet="Neural Field Rendering for Dynamic Scenes. ECCV 2026 "
                    "(European Conference on Computer Vision).",
                    raw_score=1.0,
                )
            ],
            affiliation="Vision Lab, Example University",
            note=None,
        ),
        MatchResult(
            coauthor_name="Wei Zhang",
            url="https://weizhang.example.edu",
            status=MatchStatus.LIKELY,
            confidence=0.74,
            evidence=[
                Evidence(
                    source="homepage",
                    url="https://weizhang.example.edu",
                    snippet="News: Our paper was accepted to ECCV 2026! See you in "
                    "Milan.",
                    raw_score=1.0,
                )
            ],
            suspected_names=[
                "Wei Zhang (Example University)",
                "Wei Zhang (Other Institute of Technology)",
            ],
            affiliation="Example University",
            note="2 same-name Scholar profiles; identity ambiguous.",
        ),
        MatchResult(
            coauthor_name="John Smith",
            url=None,
            status=MatchStatus.LIKELY,
            confidence=0.62,
            evidence=[
                Evidence(
                    source="arxiv",
                    url="https://arxiv.org/abs/2503.01234",
                    snippet="Comments: Accepted to ECCV 2026. 14 pages, 8 figures.",
                    raw_score=0.8,
                )
            ],
            affiliation=None,
            note="No Scholar profile resolved; matched by name only.",
        ),
        MatchResult(
            coauthor_name="Maria Garcia",
            url="https://scholar.google.com/citations?user=MARIA777",
            status=MatchStatus.UNCERTAIN,
            confidence=0.20,
            evidence=[],
            affiliation="Robotics Institute",
            note="No evidence of ECCV 2026 found in any source; the conference "
            "may not be indexed yet.",
        ),
    ]
    return {"target": target, "conf": conf, "results": results}


def run_demo(json_path: str, md_path: str) -> None:
    data = _demo_dataset()
    target, conf, results = data["target"], data["conf"], data["results"]
    results = sort_results(results)
    report = build_report(target, conf, results)
    markdown = render_markdown(target, conf, results)
    write_outputs(report, markdown, json_path, md_path)
    print(markdown)


# --------------------------------------------------------------------------- #
# Live pipeline
# --------------------------------------------------------------------------- #
def run_live(args: argparse.Namespace) -> int:
    from coauthor_extractor import ArxivNameResolver, CoAuthorExtractor
    from conference_matcher import ConferenceMatcher, parse_conference
    from disambiguator import Disambiguator
    from scholar_client import ScholarBlockedError, make_backend

    try:
        from tqdm import tqdm  # type: ignore
    except ImportError:  # pragma: no cover - optional UX dependency
        def tqdm(it, **_kw):  # type: ignore
            return it

    conf = parse_conference(args.conference)
    logger.info("Target conference: %s (variants: %s)", conf.display, conf.variants)

    serpapi_key = os.getenv("SERPAPI_KEY")
    try:
        raw_backend = make_backend(
            backend=args.backend,
            delay=args.delay,
            use_free_proxies=args.free_proxies,
            proxy_url=args.proxy,
            serpapi_key=serpapi_key,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("Could not initialize backend: %s", exc)
        return 2

    backend = CachingBackend(raw_backend, enabled=not args.no_cache)

    name_resolver = None
    if not args.no_arxiv_names:
        name_resolver = ArxivNameResolver(
            cache_dir=CACHE_DIR if not args.no_cache else None,
            enabled=True,
        )

    try:
        extractor = CoAuthorExtractor(
            backend, years=args.years, name_resolver=name_resolver
        )
        extracted = extractor.extract(args.scholar_url)
    except ScholarBlockedError as exc:
        _print_blocked_help(exc)
        return 3
    except ValueError as exc:
        logger.error("%s", exc)
        return 2

    target = extracted["target"]
    coauthors: List[CoAuthor] = extracted["coauthors"]
    if not coauthors:
        logger.warning("No co-authors found for the target profile.")

    if args.limit and args.limit > 0 and len(coauthors) > args.limit:
        # Prioritize the most resolvable co-authors (a Scholar id and/or direct
        # collaboration) so a capped run still produces meaningful matches.
        coauthors.sort(
            key=lambda c: (bool(c.scholar_id), len(c.shared_papers)),
            reverse=True,
        )
        logger.info(
            "Limiting to top %d of %d co-authors (use --limit 0 for all).",
            args.limit, len(coauthors),
        )
        coauthors = coauthors[: args.limit]

    matcher = ConferenceMatcher(
        backend, conf, enable_web_search=bool(serpapi_key)
    )
    disambiguator = Disambiguator(target, conf)

    results: List[MatchResult] = []
    for ca in tqdm(coauthors, desc="Matching co-authors", unit="author"):
        try:
            evidence = matcher.find_evidence(ca.name, ca.scholar_id, ca.homepage)
        except ScholarBlockedError as exc:
            _print_blocked_help(exc)
            return 3
        except Exception as exc:  # noqa: BLE001 - never crash the whole run
            logger.warning("Matching failed for %s: %s", ca.name, exc)
            evidence = []
        results.append(disambiguator.assess(ca, evidence))

    results = sort_results(results)
    report = build_report(target, conf, results)
    markdown = render_markdown(target, conf, results)
    write_outputs(report, markdown, args.out, args.md_out)
    print(markdown)
    return 0


def _print_blocked_help(exc: Exception) -> None:
    logger.error("Google Scholar blocked the request: %s", exc)
    sys.stderr.write(
        "\n"
        "================ ACTION REQUIRED ================\n"
        "Google Scholar is blocking automated requests (CAPTCHA / IP ban).\n"
        "Mitigations:\n"
        "  1. Re-run with rotating free proxies:   --free-proxies\n"
        "  2. Supply your own proxy:               --proxy http://user:pass@host:port\n"
        "  3. Set a SerpAPI key (most reliable):   export SERPAPI_KEY=...\n"
        "     (SerpAPI handles CAPTCHAs server-side; no proxy needed.)\n"
        "Cached progress is preserved, so re-runs resume where you left off.\n"
        "=================================================\n"
    )


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="main.py",
        description="Find which of a researcher's co-authors also have a paper "
        "at a target conference.",
    )
    p.add_argument(
        "--scholar-url",
        help="Target Google Scholar profile URL (contains user=XXXX).",
    )
    p.add_argument(
        "--conference",
        help='Target conference, e.g. "ECCV 2026" (fuzzy variants accepted).',
    )
    p.add_argument("--delay", type=float, default=3.0,
                   help="Minimum delay (seconds) between Scholar calls. Default: 3.")
    p.add_argument("--years", type=int, default=3,
                   help="How many recent years of papers to scan. Default: 3.")
    p.add_argument("--limit", type=int, default=0,
                   help="Cap the number of co-authors processed (0 = no cap). "
                        "Useful to bound API quota on a test run.")
    p.add_argument("--no-arxiv-names", action="store_true",
                   help="Disable resolving abbreviated co-author names to full "
                        "names via arXiv title lookups.")
    p.add_argument("--backend", choices=["auto", "scholarly", "serpapi"],
                   default="auto", help="Data backend. Default: auto.")
    p.add_argument("--proxy", default=None,
                   help="A single proxy URL (http://user:pass@host:port).")
    p.add_argument("--free-proxies", action="store_true",
                   help="Use rotating free proxies (best-effort, often unreliable).")
    p.add_argument("--out", default="report.json", help="JSON report path.")
    p.add_argument("--md-out", default="report.md", help="Markdown report path.")
    p.add_argument("--no-cache", action="store_true",
                   help="Bypass the local cache and always hit the network.")
    p.add_argument("--demo", action="store_true",
                   help="Render a mock report (no network calls) to preview format.")
    p.add_argument("-v", "--verbose", action="store_true", help="Verbose logging.")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # Load .env so SERPAPI_KEY etc. are available.
    try:
        from dotenv import load_dotenv  # type: ignore
        load_dotenv()
    except ImportError:
        pass

    if args.demo:
        run_demo(args.out, args.md_out)
        return 0

    if not args.scholar_url or not args.conference:
        build_parser().error("--scholar-url and --conference are required "
                             "(or use --demo).")
    return run_live(args)


if __name__ == "__main__":
    raise SystemExit(main())
