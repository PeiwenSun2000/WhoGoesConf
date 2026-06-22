# Conference Co-author Overlap Finder

English | [中文](README.zh.md)

A CLI tool that, given a researcher's Google Scholar profile and a target conference (e.g. `ECCV 2026`), finds which of their co-authors *also* have a paper at that conference. It mines co-authors from the profile and recent papers, recovers full names via arXiv, then gathers acceptance evidence from multiple sources and scores each match with a confidence tier.

## Features

Pluggable backends (`scholarly` / SerpAPI), anti-blocking (proxies, randomized delays, exponential backoff), arXiv full-name recovery, multi-source evidence (Scholar pubs, homepage, arXiv, web search), strict acronym+year matching, identity disambiguation, JSON + Markdown reports, and aggressive caching.

## Architecture

| Module | Role |
|--------|------|
| `scholar_client.py` | Data backends + anti-blocking |
| `coauthor_extractor.py` | Build & normalize the co-author set, arXiv full names |
| `conference_matcher.py` | Multi-source acceptance detection |
| `disambiguator.py` | Confidence scoring & tiers |
| `main.py` | CLI, orchestration, caching, reporting |
| `models.py`, `normalize.py` | Shared models & name helpers |

## Setup

Requires **Python 3.9+**.

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

**SerpAPI (recommended)** — Google Scholar aggressively blocks bots. A SerpAPI key avoids CAPTCHAs and is the most reliable backend.

```bash
export SERPAPI_KEY="your_key_here"     # or put it in a .env file
```

> `.env`, `cache/`, and generated reports are git-ignored. Never commit your API key.

## Usage

Preview the output format with mock data (no network):

```bash
python main.py --demo
```

Live run:

```bash
python main.py \
  --scholar-url "https://scholar.google.com/citations?user=XXXX" \
  --conference "ECCV 2026" --backend serpapi
```

Key flags:

| Flag | Default | Description |
|------|---------|-------------|
| `--scholar-url` | — | Target Scholar profile URL |
| `--conference` | — | Conference + year, fuzzy variants ok |
| `--backend` | `auto` | `auto` / `scholarly` / `serpapi` |
| `--limit` | `0` | Cap co-authors processed (0 = all) |
| `--delay` | `3` | Min seconds between Scholar calls |
| `--no-arxiv-names` | off | Skip arXiv full-name recovery |
| `--proxy` / `--free-proxies` | — | Proxy options (scholarly backend) |
| `--no-cache` | off | Bypass local cache |

## Output

- `report.json` — structured results
- `report.md` — Markdown table

Example `report.md` (from `--demo`):

```markdown
# Co-authors of Alex Researcher at ECCV 2026

**Summary:** 1 CONFIRMED, 2 LIKELY, 1 UNCERTAIN out of 4 co-authors.

| Co-author | Profile / URL | Status | Confidence | Evidence | Snippet |
|-----------|---------------|--------|-----------|----------|---------|
| Jane Doe | [link](https://scholar.google.com/citations?user=JANE9999) | CONFIRMED | 0.93 | scholar_pub | Neural Field Rendering for Dynamic Scenes. ECCV 2026 (European Conference on Computer Vision). |
| Wei Zhang | [link](https://weizhang.example.edu) | LIKELY | 0.74 | homepage | News: Our paper was accepted to ECCV 2026! See you in Milan. |
| John Smith | - | LIKELY | 0.62 | arxiv | Comments: Accepted to ECCV 2026. 14 pages, 8 figures. |
| Maria Garcia | [link](https://scholar.google.com/citations?user=MARIA777) | UNCERTAIN | 0.20 | - | - |

## Notes
- **Wei Zhang**: 2 same-name Scholar profiles; identity ambiguous.
- **John Smith**: No Scholar profile resolved; matched by name only.
- **Maria Garcia**: No evidence of ECCV 2026 found in any source; the conference may not be indexed yet.
```

Confidence tiers:

| Status | Score | Meaning |
|--------|-------|---------|
| CONFIRMED | ≥ 0.85 | Unique profile + explicit evidence |
| LIKELY | 0.5–0.85 | Evidence found, identity name-only |
| UNCERTAIN | < 0.5 | Weak/role mention or no evidence |

## Notes

SerpAPI's `google_scholar_profiles` engine is discontinued, so co-author profiles can't be resolved by name on that backend — matches stay LIKELY ("name only"). A match is never reported without an evidence snippet.
