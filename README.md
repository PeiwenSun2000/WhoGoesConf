# Conference Co-author Overlap Finder

**EN** — A CLI tool that, given a researcher's Google Scholar profile and a target conference (e.g. `ECCV 2026`), finds which of their co-authors *also* have a paper at that conference. It mines co-authors from the profile and recent papers, recovers full names via arXiv, then gathers acceptance evidence from multiple sources and scores each match with a confidence tier.

**中文** — 一个命令行工具：给定某位研究者的 Google Scholar 主页和目标会议（如 `ECCV 2026`），找出他/她的哪些合作者*同样*在该会议上有论文。工具会从主页和近期论文中提取合作者，借助 arXiv 补全全名，再从多个来源收集“被接收”的证据，并为每个匹配给出置信度分级。

---

## Features / 功能

- **EN**: pluggable backends (`scholarly` / SerpAPI), anti-blocking (proxies, delays, backoff), arXiv full-name recovery, multi-source evidence (Scholar pubs, homepage, arXiv, web search), strict acronym+year matching, identity disambiguation, JSON + Markdown reports, aggressive caching.
- **中文**：可插拔后端（`scholarly` / SerpAPI）、反封锁（代理、随机延时、指数退避）、arXiv 全名补全、多来源证据（Scholar 论文、个人主页、arXiv、网页搜索）、严格的“缩写+年份”匹配、身份消歧、JSON + Markdown 报告、强缓存。

## Architecture / 模块

| Module | Role |
|--------|------|
| `scholar_client.py` | Data backends + anti-blocking / 数据后端与反封锁 |
| `coauthor_extractor.py` | Build & normalize co-author set, arXiv full names / 构建并归一化合作者集合 |
| `conference_matcher.py` | Multi-source acceptance detection / 多来源接收检测 |
| `disambiguator.py` | Confidence scoring & tiers / 置信度打分与分级 |
| `main.py` | CLI, orchestration, caching, reporting / 命令行、编排、缓存、报告 |
| `models.py`, `normalize.py` | Shared models & name helpers / 公共数据结构与姓名工具 |

## Setup / 环境构建

Requires **Python 3.9+** / 需要 **Python 3.9+**。

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

**SerpAPI (recommended / 推荐)** — Google Scholar aggressively blocks bots. A SerpAPI key avoids CAPTCHAs and is the most reliable backend. 谷歌学术会强力封锁爬虫，使用 SerpAPI 密钥可绕过验证码，是最稳定的后端。

```bash
export SERPAPI_KEY="your_key_here"     # or put it in a .env file / 或写入 .env 文件
```

> `.env`, `cache/`, and generated reports are git-ignored. Never commit your API key. / `.env`、`cache/` 与生成的报告均已被 git 忽略，请勿提交 API 密钥。

## Usage / 使用

Preview the output format with mock data (no network) / 用模拟数据预览输出格式（无需联网）:

```bash
python main.py --demo
```

Live run / 实际运行:

```bash
python main.py \
  --scholar-url "https://scholar.google.com/citations?user=XXXX" \
  --conference "ECCV 2026" --backend serpapi
```

Key flags / 常用参数:

| Flag | Default | Description / 说明 |
|------|---------|--------------------|
| `--scholar-url` | — | Target Scholar profile URL / 目标 Scholar 主页 |
| `--conference` | — | Conference + year, fuzzy variants ok / 会议+年份，支持模糊写法 |
| `--backend` | `auto` | `auto` / `scholarly` / `serpapi` |
| `--limit` | `0` | Cap co-authors processed (0 = all) / 限制处理的合作者数（0 为全部） |
| `--delay` | `3` | Min seconds between Scholar calls / 调用间最小延时 |
| `--no-arxiv-names` | off | Skip arXiv full-name recovery / 跳过 arXiv 全名补全 |
| `--proxy` / `--free-proxies` | — | Proxy options (scholarly backend) / 代理选项 |
| `--no-cache` | off | Bypass local cache / 不使用缓存 |

## Output / 输出

- `report.json` — structured results / 结构化结果
- `report.md` — Markdown table / Markdown 表格

Confidence tiers / 置信度分级:

| Status | Score | Meaning / 含义 |
|--------|-------|----------------|
| CONFIRMED | ≥ 0.85 | Unique profile + explicit evidence / 唯一主页 + 明确证据 |
| LIKELY | 0.5–0.85 | Evidence found, identity name-only / 有证据，仅按姓名匹配 |
| UNCERTAIN | < 0.5 | Weak/role mention or no evidence / 弱提及或无证据 |

## Notes / 注意

- **EN**: SerpAPI's `google_scholar_profiles` engine is discontinued, so co-author profiles can't be resolved by name on that backend — matches stay LIKELY ("name only"). A match is never reported without an evidence snippet.
- **中文**：SerpAPI 的 `google_scholar_profiles` 接口已停用，该后端无法按姓名解析合作者主页，故匹配最高停留在 LIKELY（“仅姓名”）。任何匹配都必须附带证据片段，不会无证据下结论。
