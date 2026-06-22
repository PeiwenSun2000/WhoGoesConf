# 🎓 WhoGoesConf

中文 | [English](README.en.md)

> ☕ **你的圈子里，还有谁也要去这个会议？**

要去参加一个大会，却好奇哪些合作者也会出现在同一个会场？🤔 把某位研究者的 Google Scholar 主页和目标会议（如 `ECCV 2026`）交给 WhoGoesConf，它就会告诉你：他/她的哪些合作者*同样*在该会议上有论文被接收——这样你就知道该去看谁的 poster 🪧、茶歇时找谁聊天了。

🔍 工作原理：先从主页*和*近期论文里挖出合作者（Scholar 侧边栏出了名的不全 🙈），再借助 arXiv 补全全名（Scholar 总爱只显示首字母缩写，比如把全名缩成 `J. Doe`），随后在 Scholar 论文、个人主页、arXiv 和公开网页里搜寻“被接收”的证据。每个结论都附带置信度分级和可引用的证据片段 🧾——绝不给出无法佐证的匹配。

## ✨ 功能

可插拔后端（`scholarly` / SerpAPI）🔌、反封锁（代理、随机延时、指数退避）🛡️、arXiv 全名补全 📛、多来源证据（Scholar 论文、个人主页、arXiv、网页搜索）🌐、严格的“缩写+年份”匹配 🎯、身份消歧 🕵️、JSON + Markdown 报告 📊，以及强缓存 ⚡。

## 🧩 模块结构

| 模块 | 职责 |
|------|------|
| `scholar_client.py` | 数据后端与反封锁 |
| `coauthor_extractor.py` | 构建并归一化合作者集合、arXiv 全名补全 |
| `conference_matcher.py` | 多来源接收检测 |
| `disambiguator.py` | 置信度打分与分级 |
| `main.py` | 命令行、编排、缓存、报告 |
| `models.py`、`normalize.py` | 公共数据结构与姓名工具 |

## 🚀 环境构建

需要 **Python 3.9+**。

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

**SerpAPI（推荐）** 💡 —— 谷歌学术特别爱给爬虫弹验证码。用 SerpAPI 密钥可以轻松绕过，是最稳定的后端。

```bash
export SERPAPI_KEY="your_key_here"     # 或写入 .env 文件
```

> 🔒 `.env`、`cache/` 与生成的报告均已被 git 忽略，千万别提交你的 API 密钥！

## 🕹️ 使用

用模拟数据预览输出格式（无需联网）：

```bash
python main.py --demo
```

实际运行：

```bash
python main.py \
  --scholar-url "https://scholar.google.com/citations?user=XXXX" \
  --conference "ECCV 2026" --backend serpapi
```

🎛️ 常用参数：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--scholar-url` | — | 目标 Scholar 主页 URL |
| `--conference` | — | 会议+年份，支持模糊写法 |
| `--backend` | `auto` | `auto` / `scholarly` / `serpapi` |
| `--limit` | `0` | 限制处理的合作者数（0 为全部） |
| `--delay` | `3` | 调用 Scholar 间的最小延时（秒） |
| `--no-arxiv-names` | 关闭 | 跳过 arXiv 全名补全 |
| `--proxy` / `--free-proxies` | — | 代理选项（scholarly 后端） |
| `--no-cache` | 关闭 | 不使用本地缓存 |

## 📦 输出

- `report.json` 🧱 —— 结构化结果
- `report.md` 📋 —— Markdown 表格

`report.md` 示例（来自 `--demo`）：

> **Co-authors of Alex Researcher at ECCV 2026**
> **Summary:** 1 CONFIRMED, 2 LIKELY, 1 UNCERTAIN out of 4 co-authors.

| Co-author | Profile / URL | Status | Confidence | Evidence | Snippet |
|-----------|---------------|--------|-----------|----------|---------|
| Jane Doe | [link](https://scholar.google.com/citations?user=JANE9999) | CONFIRMED | 0.93 | scholar_pub | Neural Field Rendering for Dynamic Scenes. ECCV 2026 (European Conference on Computer Vision). |
| Wei Zhang | [link](https://weizhang.example.edu) | LIKELY | 0.74 | homepage | News: Our paper was accepted to ECCV 2026! See you in Milan. |
| John Smith | - | LIKELY | 0.62 | arxiv | Comments: Accepted to ECCV 2026. 14 pages, 8 figures. |
| Maria Garcia | [link](https://scholar.google.com/citations?user=MARIA777) | UNCERTAIN | 0.20 | - | - |

*备注：* **Wei Zhang** —— 存在 2 个同名 Scholar 主页，身份不确定；**John Smith** —— 未解析到 Scholar 主页，仅按姓名匹配；**Maria Garcia** —— 任何来源都未找到 ECCV 2026 的证据，会议可能尚未被收录。

🎚️ 置信度分级：

| 状态 | 分数 | 含义 |
|------|------|------|
| ✅ CONFIRMED | ≥ 0.85 | 唯一主页 + 明确证据 |
| 🟡 LIKELY | 0.5–0.85 | 有证据，但仅按姓名匹配 |
| ❓ UNCERTAIN | < 0.5 | 弱提及/任职信息，或无证据 |

## ⚠️ 注意

SerpAPI 的 `google_scholar_profiles` 接口已停用，该后端无法按姓名解析合作者主页，故匹配最高停留在 🟡 LIKELY（“仅姓名”）。另外别忘了：任何匹配都必须附带证据片段 🧾，绝不会在没有证据的情况下下结论。
