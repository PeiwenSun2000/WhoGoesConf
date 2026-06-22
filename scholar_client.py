"""Module 1: pluggable data-acquisition backends with anti-blocking.

Exposes an abstract :class:`ScholarBackend` and two concrete implementations:

* :class:`ScholarlyBackend` - wraps the ``scholarly`` library (default).
* :class:`SerpApiBackend`   - uses SerpAPI's Google Scholar engine when a
  ``SERPAPI_KEY`` is available.

Both return plain ``dict`` structures so the rest of the pipeline stays
backend-agnostic. The scholarly backend wires up proxies (ScraperAPI / free
proxies / a single user-supplied proxy) per the official guidance at
https://github.com/scholarly-python-package/scholarly and adds randomized
delays plus exponential backoff so we never hammer Google Scholar.
"""

from __future__ import annotations

import logging
import os
import random
import time
from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


class ScholarError(Exception):
    """Base class for backend errors."""


class ScholarBlockedError(ScholarError):
    """Raised when Google Scholar blocks us (CAPTCHA / IP ban / 429).

    The orchestration layer catches this to print an actionable message telling
    the user to supply a proxy or a SerpAPI key.
    """


def _sleep_with_jitter(base_delay: float, min_delay: float = 2.0, max_delay: float = 5.0) -> None:
    """Sleep ``base_delay`` seconds plus randomized jitter in [min, max].

    The jitter window defaults to the 2-5s range mandated by the spec; the
    configured ``--delay`` is treated as an additional floor.
    """
    delay = max(base_delay, random.uniform(min_delay, max_delay))
    logger.debug("Sleeping %.2fs before next Scholar call", delay)
    time.sleep(delay)


class ScholarBackend(ABC):
    """Abstract interface for a Google Scholar data source."""

    name: str = "abstract"

    @abstractmethod
    def get_author(self, author_id: str) -> Dict[str, Any]:
        """Return a *filled* author record (profile + publications)."""

    @abstractmethod
    def fill_publication(self, publication: Dict[str, Any]) -> Dict[str, Any]:
        """Fill a single publication stub with its full author list/venue."""

    @abstractmethod
    def search_author_by_name(self, name: str, limit: int = 5) -> List[Dict[str, Any]]:
        """Return up to ``limit`` candidate author profiles for ``name``."""

    @abstractmethod
    def search_pubs(self, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        """Free-text publication search (used for fallback discovery)."""


class ScholarlyBackend(ScholarBackend):
    """Default backend backed by the ``scholarly`` library."""

    name = "scholarly"

    def __init__(
        self,
        delay: float = 3.0,
        max_retries: int = 3,
        use_free_proxies: bool = False,
        proxy_url: Optional[str] = None,
        scraperapi_key: Optional[str] = None,
    ) -> None:
        self.delay = delay
        self.max_retries = max_retries
        self._scholarly = None  # lazily imported
        self._configure_proxy(use_free_proxies, proxy_url, scraperapi_key)

    def _ensure_scholarly(self):
        if self._scholarly is None:
            try:
                from scholarly import scholarly  # type: ignore
            except ImportError as exc:  # pragma: no cover - dependency guard
                raise ScholarError(
                    "The 'scholarly' package is not installed. Run "
                    "'pip install -r requirements.txt'."
                ) from exc
            self._scholarly = scholarly
        return self._scholarly

    def _configure_proxy(
        self,
        use_free_proxies: bool,
        proxy_url: Optional[str],
        scraperapi_key: Optional[str],
    ) -> None:
        """Set up a ProxyGenerator if any proxy option was requested.

        Following the scholarly docs, this is done once per session and lets the
        library transparently route blocked queries through the proxy.
        """
        if not (use_free_proxies or proxy_url or scraperapi_key):
            logger.info(
                "No proxy configured. Google Scholar may block requests; "
                "consider --free-proxies, --proxy, or a ScraperAPI key."
            )
            return
        try:
            from scholarly import ProxyGenerator, scholarly  # type: ignore
        except ImportError as exc:  # pragma: no cover - dependency guard
            raise ScholarError("The 'scholarly' package is not installed.") from exc

        pg = ProxyGenerator()
        ok = False
        if scraperapi_key:
            logger.info("Configuring ScraperAPI proxy backend.")
            ok = pg.ScraperAPI(scraperapi_key)
        elif proxy_url:
            logger.info("Configuring single user-supplied proxy.")
            ok = pg.SingleProxy(http=proxy_url, https=proxy_url)
        elif use_free_proxies:
            logger.info("Configuring rotating free proxies (best-effort).")
            ok = pg.FreeProxies()

        if ok:
            scholarly.use_proxy(pg)
            logger.info("Proxy successfully configured.")
        else:
            logger.warning(
                "Failed to configure proxy; proceeding without one. "
                "Expect possible CAPTCHA blocks."
            )

    def _with_retry(self, fn: Callable[[], Any], what: str) -> Any:
        """Run ``fn`` with randomized delays and exponential backoff.

        ``scholarly`` raises ``MaxTriesExceededException`` when it gives up after
        repeated CAPTCHAs; we translate that into :class:`ScholarBlockedError`.
        """
        scholarly = self._ensure_scholarly()
        try:
            from scholarly import MaxTriesExceededException  # type: ignore
        except Exception:  # pragma: no cover - older versions
            MaxTriesExceededException = Exception  # type: ignore

        last_exc: Optional[Exception] = None
        for attempt in range(1, self.max_retries + 1):
            _sleep_with_jitter(self.delay)
            try:
                return fn()
            except MaxTriesExceededException as exc:  # type: ignore[misc]
                raise ScholarBlockedError(
                    f"Google Scholar blocked the request while {what}."
                ) from exc
            except Exception as exc:  # noqa: BLE001 - we re-raise after backoff
                last_exc = exc
                msg = str(exc).lower()
                if any(tok in msg for tok in ("captcha", "429", "blocked", "too many")):
                    backoff = (2 ** attempt) + random.uniform(0, 1)
                    logger.warning(
                        "Possible block while %s (attempt %d/%d): %s. "
                        "Backing off %.1fs.",
                        what, attempt, self.max_retries, exc, backoff,
                    )
                    time.sleep(backoff)
                    continue
                logger.warning("Error while %s (attempt %d/%d): %s",
                               what, attempt, self.max_retries, exc)
        if last_exc is not None and any(
            tok in str(last_exc).lower()
            for tok in ("captcha", "429", "blocked", "too many")
        ):
            raise ScholarBlockedError(f"Repeatedly blocked while {what}.") from last_exc
        raise ScholarError(f"Failed while {what}: {last_exc}")

    def get_author(self, author_id: str) -> Dict[str, Any]:
        scholarly = self._ensure_scholarly()

        def _run() -> Dict[str, Any]:
            author = scholarly.search_author_id(author_id)
            return scholarly.fill(
                author, sections=["basics", "indices", "coauthors", "publications"]
            )

        return self._with_retry(_run, f"fetching author {author_id}")

    def fill_publication(self, publication: Dict[str, Any]) -> Dict[str, Any]:
        scholarly = self._ensure_scholarly()
        return self._with_retry(
            lambda: scholarly.fill(publication),
            "filling publication",
        )

    def search_author_by_name(self, name: str, limit: int = 5) -> List[Dict[str, Any]]:
        scholarly = self._ensure_scholarly()

        def _run() -> List[Dict[str, Any]]:
            results: List[Dict[str, Any]] = []
            search = scholarly.search_author(name)
            for _ in range(limit):
                try:
                    results.append(next(search))
                except StopIteration:
                    break
            return results

        return self._with_retry(_run, f"searching author '{name}'")

    def search_pubs(self, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        scholarly = self._ensure_scholarly()

        def _run() -> List[Dict[str, Any]]:
            results: List[Dict[str, Any]] = []
            search = scholarly.search_pubs(query)
            for _ in range(limit):
                try:
                    results.append(next(search))
                except StopIteration:
                    break
            return results

        return self._with_retry(_run, f"searching pubs '{query}'")


class SerpApiBackend(ScholarBackend):
    """Backend using SerpAPI's Google Scholar engine.

    Only used when a ``SERPAPI_KEY`` is provided. SerpAPI handles CAPTCHAs on
    its side, so we do not need a separate proxy here, but we still apply
    randomized delays for politeness.
    """

    name = "serpapi"
    ENDPOINT = "https://serpapi.com/search.json"

    def __init__(self, api_key: str, delay: float = 1.0, timeout: float = 30.0) -> None:
        self.api_key = api_key
        self.delay = delay
        self.timeout = timeout
        # SerpAPI discontinued the google_scholar_profiles engine; once we see
        # that error we short-circuit further calls to avoid wasting quota.
        self._profiles_disabled = False
        try:
            import requests  # type: ignore
        except ImportError as exc:  # pragma: no cover - dependency guard
            raise ScholarError("The 'requests' package is not installed.") from exc
        self._requests = requests

    def _get(self, params: Dict[str, Any]) -> Dict[str, Any]:
        params = {**params, "api_key": self.api_key}
        _sleep_with_jitter(self.delay, min_delay=0.5, max_delay=1.5)
        try:
            resp = self._requests.get(self.ENDPOINT, params=params, timeout=self.timeout)
        except Exception as exc:  # noqa: BLE001
            raise ScholarError(f"SerpAPI request failed: {exc}") from exc
        if resp.status_code == 429:
            raise ScholarBlockedError("SerpAPI returned 429 (rate limited).")
        if resp.status_code != 200:
            raise ScholarError(f"SerpAPI HTTP {resp.status_code}: {resp.text[:200]}")
        return resp.json()

    def get_author(self, author_id: str) -> Dict[str, Any]:
        data = self._get({"engine": "google_scholar_author", "author_id": author_id})
        author = data.get("author", {})
        articles = data.get("articles", [])
        return {
            "scholar_id": author_id,
            "name": author.get("name", ""),
            "affiliation": author.get("affiliations", ""),
            "homepage": author.get("website"),
            "interests": [i.get("title", "") for i in author.get("interests", [])],
            "coauthors": [
                {"scholar_id": c.get("author_id"), "name": c.get("name", "")}
                for c in data.get("co_authors", [])
            ],
            "publications": [
                {
                    "bib": {
                        "title": a.get("title", ""),
                        "pub_year": a.get("year"),
                        "citation": a.get("publication", ""),
                        "author": a.get("authors", ""),
                    },
                    "pub_url": a.get("link"),
                }
                for a in articles
            ],
        }

    def fill_publication(self, publication: Dict[str, Any]) -> Dict[str, Any]:
        # SerpAPI's author endpoint already returns publication metadata, so
        # there is nothing further to fill here.
        return publication

    def search_author_by_name(self, name: str, limit: int = 5) -> List[Dict[str, Any]]:
        if self._profiles_disabled:
            return []
        try:
            data = self._get({"engine": "google_scholar_profiles", "mauthors": name})
        except ScholarError as exc:
            if "discontinued" in str(exc).lower():
                if not self._profiles_disabled:
                    logger.warning(
                        "SerpAPI google_scholar_profiles engine is discontinued; "
                        "co-author profile resolution by name is unavailable on "
                        "this backend. Sidebar co-author IDs are still used."
                    )
                self._profiles_disabled = True
                return []
            raise
        profiles = data.get("profiles", [])[:limit]
        return [
            {
                "scholar_id": p.get("author_id"),
                "name": p.get("name", ""),
                "affiliation": p.get("affiliations", ""),
                "email_domain": p.get("email"),
                "interests": [i.get("title", "") for i in p.get("interests", [])],
                "citedby": p.get("cited_by"),
            }
            for p in profiles
        ]

    def search_pubs(self, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        data = self._get({"engine": "google_scholar", "q": query, "num": limit})
        results = data.get("organic_results", [])[:limit]
        return [
            {
                "bib": {
                    "title": r.get("title", ""),
                    "citation": r.get("publication_info", {}).get("summary", ""),
                },
                "pub_url": r.get("link"),
                "snippet": r.get("snippet", ""),
            }
            for r in results
        ]

    def web_search(self, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        """General Google web search (used by the conference matcher)."""
        data = self._get({"engine": "google", "q": query, "num": limit})
        results = data.get("organic_results", [])[:limit]
        return [
            {
                "title": r.get("title", ""),
                "link": r.get("link"),
                "snippet": r.get("snippet", ""),
            }
            for r in results
        ]


def make_backend(
    backend: str = "auto",
    delay: float = 3.0,
    use_free_proxies: bool = False,
    proxy_url: Optional[str] = None,
    serpapi_key: Optional[str] = None,
    scraperapi_key: Optional[str] = None,
) -> ScholarBackend:
    """Factory that selects and constructs the appropriate backend.

    ``auto`` prefers SerpAPI when a key is present (most reliable, no CAPTCHAs),
    otherwise falls back to the scholarly library.
    """
    serpapi_key = serpapi_key or os.getenv("SERPAPI_KEY")
    scraperapi_key = scraperapi_key or os.getenv("SCRAPERAPI_KEY")

    if backend == "serpapi":
        if not serpapi_key:
            raise ScholarError("backend=serpapi requires SERPAPI_KEY.")
        return SerpApiBackend(serpapi_key, delay=min(delay, 1.0))

    if backend == "auto" and serpapi_key:
        logger.info("SERPAPI_KEY detected; using SerpAPI backend.")
        return SerpApiBackend(serpapi_key, delay=min(delay, 1.0))

    return ScholarlyBackend(
        delay=delay,
        use_free_proxies=use_free_proxies,
        proxy_url=proxy_url,
        scraperapi_key=scraperapi_key,
    )
