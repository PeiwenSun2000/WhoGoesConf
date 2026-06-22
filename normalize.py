"""Name normalization and fuzzy-matching helpers.

Google Scholar author strings are messy: initials vs. full names ("J. Smith"
vs. "John Smith"), accented characters, and Chinese/pinyin order ("Wei Sun" vs.
"Sun Wei"). These helpers produce a stable normalized key for deduplication and
expose thin wrappers around :mod:`rapidfuzz` for fuzzy comparison.
"""

from __future__ import annotations

import re
import unicodedata
from typing import List

try:
    from rapidfuzz import fuzz
except Exception:  # pragma: no cover - import guard for environments w/o deps
    fuzz = None  # type: ignore[assignment]


_WS_RE = re.compile(r"\s+")
_NON_NAME_RE = re.compile(r"[^a-z\s]")


def strip_accents(text: str) -> str:
    """Remove diacritics, e.g. ``"Müller" -> "Muller"``."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def clean_name(name: str) -> str:
    """Lowercase, strip accents/punctuation, and collapse whitespace."""
    name = strip_accents(name).lower()
    # Treat dots and hyphens as separators so "J.-P." -> "j p".
    name = name.replace(".", " ").replace("-", " ")
    name = _NON_NAME_RE.sub(" ", name)
    return _WS_RE.sub(" ", name).strip()


def name_tokens(name: str) -> List[str]:
    """Return sorted name tokens, ignoring lone initials' trailing dots."""
    return sorted(t for t in clean_name(name).split() if t)


def normalize_name(name: str) -> str:
    """Produce an order-independent key for deduplication.

    Tokens are sorted so that pinyin order variants ("Wei Sun" / "Sun Wei")
    collapse to the same key. Single-character initials are kept (as a single
    letter) so that "J Smith" and "John Smith" can still fuzzy-match later but
    do not accidentally merge with unrelated names.
    """
    return " ".join(name_tokens(name))


def _initials_compatible(a_tokens: List[str], b_tokens: List[str]) -> bool:
    """Check whether two token lists are compatible under initial expansion.

    "j smith" is compatible with "john smith" because every token of the
    shorter (initial) form is either present in, or a prefix-initial of, a token
    in the other form, and the family-name token matches exactly.
    """
    short, long = sorted((a_tokens, b_tokens), key=len)
    if not short or not long:
        return False
    long_set = set(long)
    for tok in short:
        if tok in long_set:
            continue
        if len(tok) == 1 and any(other.startswith(tok) for other in long):
            continue
        return False
    return True


def similarity(a: str, b: str) -> float:
    """Return a fuzzy similarity score in ``[0, 1]`` for two names."""
    na, nb = normalize_name(a), normalize_name(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    a_tokens, b_tokens = na.split(), nb.split()
    if _initials_compatible(a_tokens, b_tokens):
        return 0.95
    if fuzz is None:
        return 1.0 if na == nb else 0.0
    return float(fuzz.token_sort_ratio(na, nb)) / 100.0


def names_match(a: str, b: str, threshold: float = 0.88) -> bool:
    """Whether two author strings likely refer to the same person."""
    return similarity(a, b) >= threshold


def fuzzy_contains(haystack: str, needle: str, threshold: float = 0.85) -> float:
    """Return the best partial-ratio score in ``[0, 1]`` of ``needle`` in text.

    Uses :func:`rapidfuzz.fuzz.partial_ratio` so a short conference string can
    be located inside a long venue/description blob. Returns ``0.0`` when below
    ``threshold`` to make callers' intent explicit.
    """
    if not haystack or not needle:
        return 0.0
    hay = strip_accents(haystack).lower()
    ndl = strip_accents(needle).lower()
    if ndl in hay:
        return 1.0
    if fuzz is None:
        return 0.0
    score = float(fuzz.partial_ratio(ndl, hay)) / 100.0
    return score if score >= threshold else 0.0
