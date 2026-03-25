# CitationClaw v2 Pipeline Redesign — Design Document

**Date:** 2026-03-23
**Status:** Approved
**Author:** VisionXLab@RethinkLab, SJTU

---

## 1. Problem Statement

Current CitationClaw v2 pipeline has three critical issues:

1. **Inaccuracy** — Search LLM hallucinations in author info and citing descriptions
2. **Cost** — Heavy reliance on paid search LLM APIs
3. **Speed** — Sequential LLM calls create bottlenecks

Root cause: using search LLM as both "search engine" and "reasoning engine" — a black box that cannot be verified.

## 2. Design Principles

1. **Facts first** — Structured APIs and PDF parsing before any LLM call
2. **Source attribution** — Every data point tagged with `source: "openalex" | "s2" | "arxiv" | "pdf" | "browser" | "llm"`
3. **LLM only understands, never searches** — LLM receives real data, extracts structure; no hallucination source
4. **Crash-safe** — Every step persists immediately; any interruption resumes without data loss
5. **Single-point config** — All prompts, rules, and settings in dedicated config files

## 3. Architecture: Three-Layer Four-Source

```
┌──────────────────────────────────────────────────────┐
│                     User Interface                    │
│  Setup Wizard · Paper Input · Live Log · Results · QA │
└────────────────────────┬─────────────────────────────┘
                         │
┌────────────────────────▼─────────────────────────────┐
│                  Pipeline Orchestrator                 │
│  Skill Registry · Runtime · Cache Coordinator · Ctrl  │
└────────────────────────┬─────────────────────────────┘
                         │
┌────────────────────────▼─────────────────────────────┐
│              Data Acquisition & Processing            │
│                                                       │
│  ┌─ Fact Layer (zero hallucination) ───────────────┐ │
│  │  OpenAlex API → authors, affiliations, citations │ │
│  │  Semantic Scholar API → h-index, influential cit │ │
│  │  arXiv API → preprint metadata, PDF direct link  │ │
│  │  Google Scholar + ScraperAPI → citing paper list  │ │
│  │  PDF download + local parse → citation context   │ │
│  └──────────────────────────────────────────────────┘ │
│                         │ when data incomplete         │
│  ┌─ Search Layer (verifiable) ──────────────────────┐ │
│  │  Playwright browser → Scholar profile, org pages  │ │
│  │  Search results as "raw material" for LLM         │ │
│  └──────────────────────────────────────────────────┘ │
│                         │ raw material → structured    │
│  ┌─ Intelligence Layer (controlled LLM) ────────────┐ │
│  │  Lightweight LLM: extract/integrate (no search)   │ │
│  │  Scholar assessment · Report generation            │ │
│  └──────────────────────────────────────────────────┘ │
└───────────────────────────────────────────────────────┘
```

### Four Data Sources

| Source | Type | Provides | Cost | Rate Limit |
|--------|------|----------|------|------------|
| OpenAlex API | Structured | Authors, affiliations, citations, concepts | Free | None (recommend <10/s) |
| Semantic Scholar API | Structured | h-index, influential citations, abstracts | Free | 1/s with key |
| arXiv API | Structured | Preprint metadata, PDF direct links | Free | 3/s |
| Playwright Browser | Local search | Scholar profiles, org pages, awards | Free | Limited by local resources |

### Multi-Source Merge Strategy

- OpenAlex as primary (free, unlimited, broadest coverage 250M+ papers)
- S2 supplements h-index and influential citation count (unique fields)
- arXiv supplements preprint PDF links
- All three queried in parallel, non-blocking
- Conflict resolution: prefer higher-priority source
- When all structured sources lack data → LLM fallback (tagged `source: "llm"`)

## 4. New Pipeline Phases

### Phase Comparison: Current vs New

| Phase | Current | New | Change |
|-------|---------|-----|--------|
| Citing list | ScraperAPI → Scholar | **Unchanged** | None |
| Authors + affiliations | search LLM (hallucination risk) | **OpenAlex + S2 + arXiv APIs** | Replace |
| Scholar tier assessment | search LLM (hallucination risk) | **Browser search + lightweight LLM** (verify first) | Redesign |
| Citation descriptions | search LLM reads paper (fabrication risk) | **PDF download + local parse + lightweight LLM** | Replace |
| Report generation | Lightweight LLM | **Unchanged** (optimize prompts) | Minor |

### New Pipeline Flow

```
Phase 1: Citing Paper List (unchanged)
  Google Scholar + ScraperAPI → citing paper list JSONL
  ↓
Phase 2: Metadata Collection (new)
  Per citing paper, in parallel:
  ├─ OpenAlex API → authors, affiliations, citations (primary)
  ├─ Semantic Scholar API → h-index, influential citations (supplement)
  ├─ arXiv API → preprint PDF link (supplement)
  └─ All three incomplete → LLM fallback (tagged source: "llm")
  ↓
Phase 3: Scholar Tier Assessment (redesigned)
  Pre-filter by rules (h-index, citation count, institution match)
  → Candidate scholars only:
    Playwright browser search (Scholar profile, Google, org pages)
    → Lightweight LLM extracts titles/honors from real web content
  ↓
Phase 4: Citation Description Extraction (redesigned)
  PDF auto-download (S2/arXiv/Unpaywall multi-source)
  → Local PDF parsing (extract citation context paragraphs)
  → Lightweight LLM locates description from parsed text (no search)
  ↓
Phase 5: Export & Report (optimized)
  User-selected sections only → dynamic report generation
```

### Pipeline Parallelism: Streaming Design

```
Phase 2 + Phase 3 run as streaming pipeline:

  ┌─ Phase 2 Workers ────────────────────┐
  │ Paper A → query APIs → ✓ cache ──────┼──→ Phase 3: search scholars of Paper A
  │ Paper B → query APIs → ✓ cache ──────┼──→ Phase 3: search scholars of Paper B
  │ Paper C → query APIs → (in progress) │
  └──────────────────────────────────────┘

Rules:
  - Phase 3 starts per-paper as soon as Phase 2 completes that paper
  - Phase 2 failure for one paper does not block others
  - Failed items collected at end, user can "retry failed only"
```

### Concurrency Configuration

| Phase | Default Workers | Limiting Factor | User Configurable |
|-------|----------------|-----------------|-------------------|
| Phase 1 page scraping | 1 (sequential) | Scholar anti-scrape | No |
| Phase 1 year-traverse | 3 years parallel | ScraperAPI credits | Yes |
| Phase 2 OpenAlex | 10 | None | Yes |
| Phase 2 S2 | 1 | API key rate limit | Auto |
| Phase 2 arXiv | 3 | 3/s limit | Auto |
| Phase 3 browser | 5 tabs | Local CPU/memory | Yes |
| Phase 3 LLM | 10 | API rate | Yes |
| Phase 4 PDF download | 10 | Network bandwidth | Yes |
| Phase 4 PDF parse | CPU cores | CPU intensive | Auto |
| Phase 4 LLM extract | 10 | API rate | Yes |

### Estimated Performance (100 citing papers)

| Phase | Current | New | Speedup |
|-------|---------|-----|---------|
| Phase 1 | ~2 min | ~2 min | — |
| Phase 2 | ~8 min (LLM per-paper) | ~30s (API batch) | **16x** |
| Phase 3 | (included in Phase 2) | ~3 min (candidates only) | — |
| Phase 4 | ~15 min (LLM per-paper) | ~5 min (PDF parallel) | **3x** |
| Phase 5 | ~1 min | ~1 min | — |
| **Total** | **~26 min** | **~12 min** | **~2x** |

## 5. Phase 3 Scholar Assessment — Two Candidates

Phase 3 has two candidate approaches. **Validate Candidate A first.**

### Candidate A: Playwright Browser Search + Lightweight LLM

```
Input: author name + affiliation (from Phase 2 fact data)
  ↓
Playwright auto-search (per scholar):
  Step 1: Google Scholar profile → h-index, citations
  Step 2: Google search "{name} {affil} Fellow OR Academician OR award"
  Step 3 (if needed): Institution official page
  Max 3 rounds, 20s timeout per scholar
  LLM judges sufficiency after each step
  ↓
Lightweight LLM integration:
  Input = real web page text snippets (not LLM search results)
  Task = extract titles/honors/positions from existing content
  No searching, no fabrication
```

### Candidate B: Rule Engine + Search LLM Fallback

```
Input: author name + affiliation + h-index (from S2)
  ↓
Rule engine pre-filter:
  h-index > 40 OR citations > 10000 → "high-impact candidate"
  Institution matches known list → candidate
  Others → "regular scholar", skip deep search
  ↓
Only candidates → search LLM (greatly reduced call volume, ~5-10%)
```

### Validation Plan

Take 20 known scholars (mix of academicians, Fellows, regular professors, international).
Run both approaches. Compare:
- Time per scholar
- Accuracy (titles correctly identified)
- If Candidate A: time < 15s and accuracy > 90% → adopt A
- Otherwise → adopt B

## 6. Scholar Tier Criteria (International)

```yaml
# Covers both domestic (China) and international scholars

tiers:
  - name: "Academician"
    priority: 1
    criteria:
      domestic:
        - 中国科学院院士
        - 中国工程院院士
      international:
        - National Academy of Engineering (NAE) Member
        - National Academy of Sciences (NAS) Member
        - Fellow of the Royal Society (FRS)
        - European Academy of Sciences Member
        - Other national academy members

  - name: "Fellow"
    priority: 2
    criteria:
      - IEEE Fellow, ACM Fellow, ACL Fellow, AAAI Fellow
      - APS Fellow, ACS Fellow, RSC Fellow
      - Any major international academic society Fellow

  - name: "Major Award Winner"
    priority: 3
    criteria:
      - Turing Award, Nobel Prize, Fields Medal
      - CVPR/ICCV/NeurIPS/ICML Best Paper Award
      - ACL/EMNLP Best Paper Award
      - National Science Foundation awards (NSF CAREER, etc.)

  - name: "National Talent (China)"
    priority: 4
    criteria:
      - 杰青 (Distinguished Young Scholar)
      - 长江学者 (Changjiang Scholar)
      - 优青 (Excellent Young Scholar)
      - 万人计划 (Ten Thousand Talents)

  - name: "Industry Leader"
    priority: 5
    criteria:
      institutions: [Google, DeepMind, OpenAI, Meta AI, Microsoft Research,
                     Apple ML, Amazon Science, NVIDIA Research]
      positions: [Chief Scientist, VP of Research, Lab Director,
                  Distinguished Scientist, Principal Researcher]

  - name: "University Leadership"
    priority: 6
    criteria:
      positions: [President, Dean, Department Chair]
      condition: "Top 100 university (QS/THE ranking)"
```

## 7. Optimized Prompt Design

Reduced from 6 prompts to 3 core prompts (plus 1-2 for report generation):

### 7.1 `self_citation.txt` — Self-Citation Detection

```
Input: structured author lists (from APIs, not LLM)
Task: compare author overlap
Output: "是" or "否"
```

```
【任务】判断一篇施引论文是否为自引。
【自引定义】若施引论文的任意一位作者，同时也是被引目标论文的作者，则视为自引。

【被引目标论文的作者列表】
{target_authors}

【施引论文的作者列表】
{citing_authors}

【注意】姓名可能有中英文差异、缩写差异（如 "Xiao-Ming Wang" vs "王晓明" vs "X. Wang"），
请结合单位信息综合判断。
请直接回答：是 或 否
```

### 7.2 `scholar_assess.txt` — Scholar Assessment + JSON Output (merged Step 5+6)

```
Input: structured S2/OpenAlex data + browser search snippets
Task: assess tier and output structured JSON
Output: JSON array or "无"
```

```
以下是一篇施引论文的作者信息。

【结构化数据（来自学术数据库，可信）】
{structured_data}

【网络搜索结果（来自浏览器搜索的原始网页片段）】
{browser_search_snippets}

请根据以上信息，判断哪些作者属于顶级学者。

判定标准（国内外通用）：
- 院士：中国两院院士、NAE/NAS/FRS/欧洲科学院等国外院士
- Fellow：IEEE/ACM/ACL/AAAI 等国际学术组织 Fellow
- 重大奖项：图灵奖、诺贝尔奖、顶会 Best Paper 等
- 国家级人才：杰青、长江学者、优青、万人计划
- 知名机构核心：Google/DeepMind/OpenAI 等首席科学家、研究VP
- 大学领导层：知名大学校长/院长

若无顶级学者，输出"无"。
若有，以 JSON 数组格式输出：
[
  {
    "name": "姓名",
    "affiliation": "机构",
    "country": "国家",
    "position": "职务",
    "honors": "荣誉称号",
    "tier": "院士|Fellow|重大奖项|国家级人才|知名机构核心|大学领导层",
    "evidence": "判定依据（引用上方数据中的具体内容）"
  }
]
```

### 7.3 `citation_extract.txt` — Citation Description Extraction

```
Input: parsed PDF text (citation context paragraphs)
Task: locate and extract how the citing paper describes the target
Output: structured citation description
```

```
以下是论文《{citing_title}》中与引用相关的文本段落（来自 PDF 解析）：

{parsed_paragraphs}

请从以上文本中找出引用《{target_title}》的具体描述。

要求：
1. 只摘录上方文本中真实存在的内容，严禁编造
2. 注明出现在哪个部分（Introduction/Related Work/Method 等）
3. 仅当原文有明确积极评价词汇时注明【正面引用】
4. 找不到则输出"未在已解析文本中找到相关引用描述"

输出格式：
{
  "found": true/false,
  "section": "出现的章节",
  "description": "原文摘录",
  "sentiment": "正面|中性|未标注"
}
```

### 7.4 Report-stage prompts (lower priority, no accuracy risk)

- `report_insight.txt` — Generate analytical insights from structured data
- Report chat system prompt — Answer questions based on report data

## 8. Cache & Fault Tolerance

### 8.1 Cache Architecture

```
data/cache/
  ├── phase1_cache.json          # Citing list (existing, keep)
  ├── metadata_cache.json        # Phase 2: OpenAlex/S2/arXiv results
  │     key: DOI or paper_title.lower()
  │     value: {authors, affiliations, h_index, citations,
  │             source: "openalex"|"s2"|"arxiv"|"llm",
  │             fetched_at: ISO8601}
  │
  ├── scholar_cache.json         # Phase 3: Scholar tier results
  │     key: normalized(name + affiliation)
  │     value: {tier, honors, positions,
  │             source: "browser"|"llm",
  │             evidence: [...raw web snippets],
  │             assessed_at: ISO8601}
  │
  ├── pdf_cache/                 # Phase 4: Downloaded PDFs
  │     {doi_hash}.pdf
  │
  └── citation_desc_cache.json   # Phase 4: Citation descriptions
        key: citing_paper||target_paper
        value: {description, section, sentiment,
                source: "pdf"|"llm",
                extracted_at: ISO8601}
```

### 8.2 Write Strategy

- **Phase 1 cache**: Immediate write (existing behavior)
- **Metadata/Scholar/CitationDesc caches**: Batch write every 10 updates, atomic (tempfile + os.replace)
- **PDF cache**: Write-on-download (file-based, naturally atomic)
- **ERROR sentinels**: Never cached; ensures retry on next run
- **Cache flush**: Always in `finally` block, even on crash/cancel

### 8.3 Fault Tolerance

| Scenario | Behavior |
|----------|----------|
| Phase 2 interrupted at 60/100 papers | Restart: skip 60 cached, query remaining 40 |
| Phase 3 interrupted at 20/50 scholars | Restart: skip 20 cached, search remaining 30 |
| Phase 4 PDF download fails for some | Mark failed, continue others, user can "retry failed" |
| User cancels | Immediate stop, all completed work preserved |
| API quota exhausted | Stop current phase, preserve completed work, prompt user |
| Network error on single paper | Mark failed, continue processing others |
| S2 rate-limited | Auto-degrade: continue with OpenAlex data, queue S2 queries |

### 8.4 "Retry Failed Only" Feature

After any run completes (with or without errors):
```
Task Summary:
  Phase 2: 98/100 success, 2 failed
  Phase 3: 45/50 success, 5 failed
  Phase 4: 80/90 success, 10 failed (5 no PDF available)

  [Retry 17 failed items]  [Export current results]  [Full re-run]
```

## 9. Configuration Architecture

### 9.1 File Structure

```
citationclaw/
  config/
    ├── prompts/                # All prompt templates (plain text)
    │     ├── self_citation.txt
    │     ├── scholar_assess.txt
    │     ├── citation_extract.txt
    │     ├── report_insight.txt
    │     └── fallback_author.txt
    │
    ├── rules/                  # Rule configurations (YAML)
    │     ├── scholar_tiers.yaml      # Tier definitions + criteria
    │     ├── institutions.yaml       # Known institution list
    │     ├── search_strategy.yaml    # Browser search steps + limits
    │     └── data_sources.yaml       # API priorities + merge strategy
    │
    └── providers.yaml          # LLM provider presets
```

### 9.2 LLM Provider Configuration

```yaml
# config/providers.yaml

presets:
  openai:
    name: "OpenAI"
    base_url: "https://api.openai.com/v1"
    default_model: "gpt-4o-mini"

  deepseek:
    name: "DeepSeek"
    base_url: "https://api.deepseek.com/v1"
    default_model: "deepseek-chat"

  google:
    name: "Google Gemini"
    base_url: "https://generativelanguage.googleapis.com/v1beta/openai/"
    default_model: "gemini-2.0-flash"

  zhipu:
    name: "智谱 AI"
    base_url: "https://open.bigmodel.cn/api/paas/v4/"
    default_model: "glm-4-flash"

  siliconflow:
    name: "硅基流动"
    base_url: "https://api.siliconflow.cn/v1"
    default_model: "Qwen/Qwen2.5-7B-Instruct"

  ollama:
    name: "Ollama 本地"
    base_url: "http://localhost:11434/v1"
    default_model: "qwen2.5:7b"

  vapi:
    name: "V-API 中转"
    base_url: "https://api.gpt.ge/v1"
    default_model: "gemini-2.0-flash"

  custom:
    name: "自定义 (OpenAI 兼容)"
    base_url: ""
    default_model: ""

# User's active config (single model, simple)
active:
  provider: "vapi"
  api_key: ""
  model: ""          # empty = use preset default_model
```

### 9.3 Dynamic Report Generation

Reports are content-driven, not template-driven:

```
User selects analysis dimensions before running:
  ☑ 知名学者分析        → enables Phase 3
  ☑ 机构分布分析        → computed from Phase 2 data
  ☐ 引文描述分析        → if unchecked, skips Phase 4 entirely
  ☑ 年份趋势           → computed from Phase 1 data
  ☑ 自引统计           → computed from Phase 2 data
  ☑ AI 洞察摘要        → LLM generates from all available data
  ☑ AI 问答助手        → embedded chat widget

Report HTML only renders sections that have data.
No "template mismatch" — if data exists, section appears.
```

### 9.4 Frontend Setup Wizard

```
Step 1: Select LLM Provider
  [OpenAI] [DeepSeek] [Gemini] [智谱] [硅基流动] [Ollama] [V-API] [自定义]
  → Auto-fills base_url and default model

Step 2: Enter API Key
  [sk-...                    ]
  ✓ Auto-detect connection status

Step 3: ScraperAPI Key (for Google Scholar)
  [key1, key2, ...           ]

Step 4: Optional — Semantic Scholar API Key
  [                          ]  (leave empty to use free tier)

Step 5: Select Analysis Dimensions
  ☑ Scholar assessment  ☑ Institution analysis  ☐ Citation descriptions ...

  [ Start Using ]
```

## 10. Proxy Handling

All HTTP clients use `trust_env=False` to bypass system proxy (SOCKS5/HTTP).

For Playwright browser:
- Detect system proxy settings
- Offer user choice: use proxy / direct connection / custom proxy
- Auto-configure Playwright launch args accordingly

## 11. Success Criteria

| Metric | Current | Target |
|--------|---------|--------|
| Author name+affiliation accuracy | ~70% (LLM hallucinations) | >95% (API-sourced) |
| Citation description accuracy | ~60% (LLM fabrication) | >90% (PDF-sourced) |
| Cost per 100 papers | ~25 RMB | <5 RMB |
| Time per 100 papers | ~26 min | <12 min |
| Data source traceability | None | 100% (every field tagged) |

## 12. Open Questions / Future Work

1. **Phase 3 Candidate A vs B** — Requires validation experiment before final decision
2. **PDF availability** — Not all papers have accessible PDFs; need graceful degradation
3. **Unpaywall integration** — Additional PDF source for paywalled papers
4. **GROBID integration** — Advanced PDF parsing for structured reference extraction
5. **Local model support** — Ollama integration for fully offline operation
6. **Batch mode** — CLI-based batch processing for institutional users
