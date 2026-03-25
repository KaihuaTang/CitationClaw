# CitationClaw v2 Pipeline Redesign — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace search LLM dependency with structured APIs (OpenAlex + S2 + arXiv) + PDF parsing + local browser search, achieving >95% accuracy at <20% current cost.

**Architecture:** Three-layer pipeline — Fact Layer (structured APIs, zero hallucination), Search Layer (Playwright browser, verifiable), Intelligence Layer (lightweight LLM, extract-only). All config centralized in `config/` directory.

**Tech Stack:** FastAPI, httpx, Playwright, PyMuPDF (fitz), OpenAlex/S2/arXiv APIs, OpenAI SDK, BeautifulSoup

**Design Doc:** `docs/plans/2026-03-23-pipeline-redesign-design.md`

---

## Milestone Overview

| Milestone | Description | Tasks |
|-----------|-------------|-------|
| M1 | Config system redesign | Tasks 1-3 |
| M2 | Structured API clients (OpenAlex, S2, arXiv) | Tasks 4-7 |
| M3 | New Phase 2: Metadata collection | Tasks 8-9 |
| M4 | PDF download + parse pipeline | Tasks 10-12 |
| M5 | New Phase 4: Citation description extraction | Tasks 13-14 |
| M6 | Playwright browser search agent | Tasks 15-17 |
| M7 | New Phase 3: Scholar assessment | Tasks 18-19 |
| M8 | Pipeline integration + frontend | Tasks 20-22 |

---

## M1: Config System Redesign

### Task 1: Centralized Prompt Files

**Files:**
- Create: `citationclaw/config/prompts/self_citation.txt`
- Create: `citationclaw/config/prompts/scholar_assess.txt`
- Create: `citationclaw/config/prompts/citation_extract.txt`
- Create: `citationclaw/config/prompts/report_insight.txt`
- Create: `citationclaw/config/prompts/fallback_author.txt`
- Create: `citationclaw/config/__init__.py`
- Create: `citationclaw/config/prompt_loader.py`
- Test: `test/test_prompt_loader.py`

**Step 1: Create prompt template files**

Write each prompt to its own `.txt` file with `{variable}` placeholders. Content as specified in design doc sections 7.1-7.4.

**Step 2: Write the failing test**

```python
# test/test_prompt_loader.py
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from citationclaw.config.prompt_loader import PromptLoader

def test_load_existing_prompt():
    loader = PromptLoader()
    text = loader.get("self_citation")
    assert "{target_authors}" in text
    assert "{citing_authors}" in text

def test_load_with_variables():
    loader = PromptLoader()
    text = loader.render("self_citation", target_authors="Alice", citing_authors="Bob")
    assert "Alice" in text
    assert "Bob" in text
    assert "{target_authors}" not in text

def test_load_nonexistent_raises():
    loader = PromptLoader()
    with pytest.raises(FileNotFoundError):
        loader.get("nonexistent_prompt")

def test_custom_prompt_dir(tmp_path):
    (tmp_path / "test_prompt.txt").write_text("Hello {name}")
    loader = PromptLoader(prompt_dir=tmp_path)
    assert loader.render("test_prompt", name="World") == "Hello World"
```

**Step 3: Run test to verify it fails**

Run: `cd /Users/charlesyang/Desktop/CitationClaw-v2 && python -m pytest test/test_prompt_loader.py -v`
Expected: FAIL — module not found

**Step 4: Implement PromptLoader**

```python
# citationclaw/config/prompt_loader.py
from pathlib import Path
from typing import Optional

_DEFAULT_DIR = Path(__file__).parent / "prompts"

class PromptLoader:
    """Load and render prompt templates from config/prompts/ directory."""

    def __init__(self, prompt_dir: Optional[Path] = None):
        self._dir = prompt_dir or _DEFAULT_DIR

    def get(self, name: str) -> str:
        path = self._dir / f"{name}.txt"
        if not path.exists():
            raise FileNotFoundError(f"Prompt template not found: {path}")
        return path.read_text(encoding="utf-8")

    def render(self, name: str, **kwargs) -> str:
        template = self.get(name)
        return template.format(**kwargs)
```

**Step 5: Run test to verify it passes**

Run: `cd /Users/charlesyang/Desktop/CitationClaw-v2 && python -m pytest test/test_prompt_loader.py -v`
Expected: PASS

**Step 6: Commit**

```bash
git add citationclaw/config/ test/test_prompt_loader.py
git commit -m "feat: centralized prompt template system with PromptLoader"
```

---

### Task 2: Provider Configuration

**Files:**
- Create: `citationclaw/config/providers.yaml`
- Create: `citationclaw/config/provider_manager.py`
- Test: `test/test_provider_manager.py`

**Step 1: Create providers.yaml**

Write the YAML file with presets for OpenAI, DeepSeek, Gemini, 智谱, 硅基流动, Ollama, V-API, custom. As specified in design doc section 9.2.

**Step 2: Write the failing test**

```python
# test/test_provider_manager.py
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from citationclaw.config.provider_manager import ProviderManager

def test_list_presets():
    pm = ProviderManager()
    presets = pm.list_presets()
    assert "openai" in presets
    assert "deepseek" in presets
    assert "ollama" in presets

def test_get_preset_info():
    pm = ProviderManager()
    info = pm.get_preset("deepseek")
    assert info["base_url"] == "https://api.deepseek.com/v1"
    assert info["default_model"] == "deepseek-chat"

def test_build_client_config():
    pm = ProviderManager()
    cfg = pm.build_config(provider="deepseek", api_key="sk-test", model=None)
    assert cfg["api_key"] == "sk-test"
    assert cfg["base_url"] == "https://api.deepseek.com/v1"
    assert cfg["model"] == "deepseek-chat"  # fallback to default

def test_build_client_config_custom_model():
    pm = ProviderManager()
    cfg = pm.build_config(provider="deepseek", api_key="sk-test", model="deepseek-reasoner")
    assert cfg["model"] == "deepseek-reasoner"  # user override

def test_custom_provider():
    pm = ProviderManager()
    cfg = pm.build_config(
        provider="custom",
        api_key="sk-test",
        model="my-model",
        base_url="https://my-api.com/v1"
    )
    assert cfg["base_url"] == "https://my-api.com/v1"
    assert cfg["model"] == "my-model"
```

**Step 3: Run test → FAIL**

**Step 4: Implement ProviderManager**

```python
# citationclaw/config/provider_manager.py
import yaml
from pathlib import Path
from typing import Optional

_DEFAULT_FILE = Path(__file__).parent / "providers.yaml"

class ProviderManager:
    def __init__(self, config_file: Optional[Path] = None):
        path = config_file or _DEFAULT_FILE
        with open(path, encoding="utf-8") as f:
            self._data = yaml.safe_load(f)
        self._presets = self._data.get("presets", {})

    def list_presets(self) -> list:
        return list(self._presets.keys())

    def get_preset(self, name: str) -> dict:
        if name not in self._presets:
            raise KeyError(f"Unknown provider: {name}")
        return self._presets[name]

    def build_config(self, provider: str, api_key: str,
                     model: Optional[str] = None,
                     base_url: Optional[str] = None) -> dict:
        preset = self._presets.get(provider, {})
        return {
            "api_key": api_key,
            "base_url": base_url or preset.get("base_url", ""),
            "model": model or preset.get("default_model", ""),
        }
```

**Step 5: Run test → PASS**

**Step 6: Commit**

```bash
git add citationclaw/config/providers.yaml citationclaw/config/provider_manager.py test/test_provider_manager.py
git commit -m "feat: LLM provider presets with auto-fill for 7 vendors"
```

---

### Task 3: Rules Configuration (Scholar Tiers + Data Sources)

**Files:**
- Create: `citationclaw/config/rules/scholar_tiers.yaml`
- Create: `citationclaw/config/rules/data_sources.yaml`
- Create: `citationclaw/config/rules/institutions.yaml`
- Create: `citationclaw/config/rules/search_strategy.yaml`
- Create: `citationclaw/config/rules_loader.py`
- Test: `test/test_rules_loader.py`

**Step 1: Create YAML rule files**

Scholar tiers as specified in design doc section 6. Data sources as specified in section 3. Institutions list with known tech companies and top universities. Search strategy with steps, limits, and sufficiency check prompt.

**Step 2: Write failing test**

```python
# test/test_rules_loader.py
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from citationclaw.config.rules_loader import RulesLoader

def test_load_scholar_tiers():
    rules = RulesLoader()
    tiers = rules.get("scholar_tiers")
    assert "tiers" in tiers
    assert any(t["name"] == "Academician" for t in tiers["tiers"])

def test_load_data_sources():
    rules = RulesLoader()
    sources = rules.get("data_sources")
    assert "metadata_sources" in sources

def test_tier_keywords():
    rules = RulesLoader()
    tiers = rules.get("scholar_tiers")
    fellow_tier = next(t for t in tiers["tiers"] if t["name"] == "Fellow")
    assert "IEEE Fellow" in str(fellow_tier["criteria"])
```

**Step 3-6: Implement, test, commit**

```bash
git commit -m "feat: YAML-based rules for scholar tiers, data sources, institutions, search strategy"
```

---

## M2: Structured API Clients

### Task 4: OpenAlex API Client

**Files:**
- Create: `citationclaw/core/openalex_client.py`
- Test: `test/test_openalex_client.py`

**Step 1: Write failing test**

```python
# test/test_openalex_client.py
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from citationclaw.core.openalex_client import OpenAlexClient

def test_build_search_url():
    client = OpenAlexClient()
    url = client._build_search_url("Attention is All You Need")
    assert "openalex.org" in url
    assert "search" in url or "filter" in url

def test_parse_work_response():
    """Test parsing a realistic OpenAlex work response."""
    client = OpenAlexClient()
    mock_response = {
        "id": "W123",
        "title": "Attention is All You Need",
        "publication_year": 2017,
        "cited_by_count": 100000,
        "authorships": [
            {
                "author": {"id": "A1", "display_name": "Ashish Vaswani"},
                "institutions": [{"display_name": "Google Brain", "country_code": "US"}],
            }
        ],
        "doi": "https://doi.org/10.xxxx",
    }
    result = client._parse_work(mock_response)
    assert result["title"] == "Attention is All You Need"
    assert result["authors"][0]["name"] == "Ashish Vaswani"
    assert result["authors"][0]["affiliation"] == "Google Brain"
    assert result["authors"][0]["country"] == "US"
    assert result["source"] == "openalex"

def test_parse_author_response():
    """Test parsing OpenAlex author data."""
    client = OpenAlexClient()
    mock_author = {
        "id": "A1",
        "display_name": "Ashish Vaswani",
        "cited_by_count": 200000,
        "summary_stats": {"h_index": 30},
        "affiliations": [{"institution": {"display_name": "Google Brain"}}],
    }
    result = client._parse_author(mock_author)
    assert result["name"] == "Ashish Vaswani"
    assert result["h_index"] == 30
    assert result["citation_count"] == 200000
```

**Step 2: Run test → FAIL**

**Step 3: Implement OpenAlexClient**

```python
# citationclaw/core/openalex_client.py
"""OpenAlex API client for structured academic metadata.

API docs: https://docs.openalex.org/
Free, no key required, recommend <10 req/s.
"""
import httpx
import asyncio
from typing import Optional, List
from urllib.parse import quote

BASE_URL = "https://api.openalex.org"

class OpenAlexClient:
    def __init__(self, email: Optional[str] = None):
        # Polite pool: pass email for higher rate limit
        self._params = {"mailto": email} if email else {}
        self._client = httpx.AsyncClient(
            trust_env=False, timeout=30.0,
            headers={"User-Agent": "CitationClaw/2.0 (academic research tool)"}
        )

    async def search_work(self, title: str) -> Optional[dict]:
        """Search for a paper by title, return parsed metadata."""
        url = self._build_search_url(title)
        resp = await self._client.get(url, params=self._params)
        if resp.status_code != 200:
            return None
        data = resp.json()
        results = data.get("results", [])
        if not results:
            return None
        return self._parse_work(results[0])

    async def get_author(self, author_id: str) -> Optional[dict]:
        """Get author details by OpenAlex author ID."""
        url = f"{BASE_URL}/authors/{author_id}"
        resp = await self._client.get(url, params=self._params)
        if resp.status_code != 200:
            return None
        return self._parse_author(resp.json())

    async def batch_search_works(self, titles: List[str],
                                  concurrency: int = 10) -> List[Optional[dict]]:
        """Search multiple papers concurrently."""
        sem = asyncio.Semaphore(concurrency)
        async def _search(t):
            async with sem:
                return await self.search_work(t)
        return await asyncio.gather(*[_search(t) for t in titles])

    def _build_search_url(self, title: str) -> str:
        return f"{BASE_URL}/works?search={quote(title)}&per_page=1"

    def _parse_work(self, work: dict) -> dict:
        authors = []
        for authorship in work.get("authorships", []):
            author = authorship.get("author", {})
            institutions = authorship.get("institutions", [])
            inst = institutions[0] if institutions else {}
            authors.append({
                "name": author.get("display_name", ""),
                "openalex_id": author.get("id", ""),
                "affiliation": inst.get("display_name", ""),
                "country": inst.get("country_code", ""),
            })
        return {
            "title": work.get("title", ""),
            "year": work.get("publication_year"),
            "doi": work.get("doi", ""),
            "cited_by_count": work.get("cited_by_count", 0),
            "openalex_id": work.get("id", ""),
            "authors": authors,
            "source": "openalex",
        }

    def _parse_author(self, author: dict) -> dict:
        stats = author.get("summary_stats", {})
        affiliations = author.get("affiliations", [])
        current = affiliations[0] if affiliations else {}
        return {
            "name": author.get("display_name", ""),
            "openalex_id": author.get("id", ""),
            "h_index": stats.get("h_index", 0),
            "citation_count": author.get("cited_by_count", 0),
            "affiliation": current.get("institution", {}).get("display_name", ""),
            "source": "openalex",
        }

    async def close(self):
        await self._client.aclose()
```

**Step 4: Run test → PASS**

**Step 5: Commit**

```bash
git commit -m "feat: OpenAlex API client for structured academic metadata"
```

---

### Task 5: Semantic Scholar API Client

**Files:**
- Create: `citationclaw/core/s2_client.py`
- Test: `test/test_s2_client.py`

Similar pattern to Task 4. Key differences:
- Rate limiting: 1 req/s with key, built-in rate limiter via `asyncio.sleep`
- Unique fields: `h_index`, `influentialCitationCount`
- API: `https://api.semanticscholar.org/graph/v1/paper/search?query={title}`
- Author API: `https://api.semanticscholar.org/graph/v1/author/{id}?fields=name,hIndex,citationCount,affiliations`
- Optional API key via `x-api-key` header

```bash
git commit -m "feat: Semantic Scholar API client with rate limiting"
```

---

### Task 6: arXiv API Client

**Files:**
- Create: `citationclaw/core/arxiv_client.py`
- Test: `test/test_arxiv_client.py`

Key features:
- API: `http://export.arxiv.org/api/query?search_query=ti:{title}`
- XML response parsing (Atom feed)
- Extract: authors, affiliations, abstract, PDF link (`http://arxiv.org/pdf/{id}`)
- Rate limit: 3 req/s

```bash
git commit -m "feat: arXiv API client for preprint metadata and PDF links"
```

---

### Task 7: Multi-Source Metadata Collector

**Files:**
- Create: `citationclaw/core/metadata_collector.py`
- Test: `test/test_metadata_collector.py`

**Core logic:** Query all three sources in parallel, merge results by priority.

```python
# Core merge strategy (simplified)
class MetadataCollector:
    async def collect(self, title: str) -> dict:
        """Query OpenAlex + S2 + arXiv in parallel, merge results."""
        oa_result, s2_result, arxiv_result = await asyncio.gather(
            self.openalex.search_work(title),
            self.s2.search_paper(title),
            self.arxiv.search_paper(title),
            return_exceptions=True,
        )
        return self._merge(oa_result, s2_result, arxiv_result)

    def _merge(self, oa, s2, arxiv) -> dict:
        """Merge by priority: OpenAlex > S2 > arXiv. S2 supplements h-index."""
        # Base from OpenAlex (most complete)
        # Add h-index, influential_citations from S2
        # Add pdf_url from arXiv
        # Tag each field with source
```

```bash
git commit -m "feat: multi-source metadata collector with parallel query and merge"
```

---

## M3: New Phase 2

### Task 8: New Phase 2 Skill — Metadata Collection

**Files:**
- Create: `citationclaw/skills/phase2_metadata.py`
- Modify: `citationclaw/skills/registry.py` — replace AuthorIntelSkill
- Create: `citationclaw/core/metadata_cache.py`
- Test: `test/test_phase2_metadata.py`

**Core:** Replace LLM-based author search with structured API calls.

```python
class MetadataCollectionSkill:
    name = "phase2_metadata"

    async def run(self, ctx: SkillContext, **kwargs) -> SkillResult:
        input_file = kwargs["input_file"]  # Phase 1 JSONL
        cache = kwargs.get("metadata_cache")
        collector = MetadataCollector(...)

        papers = parse_phase1_jsonl(input_file)
        results = []
        for paper in papers:
            # Check cache first
            cached = await cache.get(paper["paper_title"])
            if cached:
                results.append(cached)
                continue
            # Query APIs
            metadata = await collector.collect(paper["paper_title"])
            await cache.update(paper["paper_title"], metadata)
            results.append(metadata)

        # Write output JSONL
        write_jsonl(output_file, results)
        return SkillResult(name=self.name, data={"output_file": output_file})
```

```bash
git commit -m "feat: new Phase 2 skill using structured APIs instead of search LLM"
```

---

### Task 9: Self-Citation Detection Update

**Files:**
- Modify: `citationclaw/core/author_searcher.py` — extract self-citation logic
- Create: `citationclaw/core/self_citation.py`
- Test: `test/test_self_citation.py`

**Change:** Self-citation now uses structured author lists from APIs instead of LLM-searched text. Can often be done purely by name matching (no LLM needed). LLM only for ambiguous cases (name variants).

```python
class SelfCitationDetector:
    def check(self, target_authors: list, citing_authors: list) -> bool:
        """Rule-based check first, LLM fallback for ambiguous names."""
        # Exact name match
        if self._exact_match(target_authors, citing_authors):
            return True
        # Fuzzy match (name variants)
        if self._fuzzy_match(target_authors, citing_authors):
            return True
        # If uncertain, use LLM with structured data
        return False  # or call LLM
```

```bash
git commit -m "feat: rule-based self-citation detection with LLM fallback"
```

---

## M4: PDF Pipeline

### Task 10: Multi-Source PDF Downloader

**Files:**
- Create: `citationclaw/core/pdf_downloader.py`
- Test: `test/test_pdf_downloader.py`

**Sources (in priority order):**
1. arXiv direct link (free, reliable)
2. Semantic Scholar PDF link
3. Unpaywall API (`https://api.unpaywall.org/v2/{doi}?email=...`)
4. DOI redirect (follow DOI → publisher → PDF link)

```python
class PDFDownloader:
    async def download(self, paper: dict, cache_dir: Path) -> Optional[Path]:
        """Try multiple sources to download PDF."""
        doi_hash = hashlib.md5(paper.get("doi", paper["title"]).encode()).hexdigest()
        cached = cache_dir / f"{doi_hash}.pdf"
        if cached.exists():
            return cached

        for source_fn in [self._try_arxiv, self._try_s2, self._try_unpaywall, self._try_doi]:
            pdf_bytes = await source_fn(paper)
            if pdf_bytes:
                cached.write_bytes(pdf_bytes)
                return cached
        return None  # PDF not available
```

```bash
git commit -m "feat: multi-source PDF downloader with local caching"
```

---

### Task 11: PDF Citation Context Parser

**Files:**
- Create: `citationclaw/core/pdf_parser.py`
- Test: `test/test_pdf_parser.py`

**Dependencies:** Add `PyMuPDF` (fitz) to requirements.txt

**Core:** Extract paragraphs containing citations to the target paper.

```python
class PDFCitationParser:
    def extract_citation_contexts(self, pdf_path: Path, target_title: str,
                                   target_authors: list) -> list:
        """Parse PDF and find paragraphs citing the target paper."""
        doc = fitz.open(str(pdf_path))
        full_text = "\n".join(page.get_text() for page in doc)

        # Find reference entry for target paper
        ref_id = self._find_reference_id(full_text, target_title, target_authors)

        # Extract paragraphs containing that reference
        contexts = self._extract_contexts(full_text, ref_id, target_title)

        return [{
            "section": self._detect_section(ctx),
            "text": ctx,
            "source": "pdf",
        } for ctx in contexts]
```

```bash
git commit -m "feat: PDF citation context parser using PyMuPDF"
```

---

### Task 12: PDF Pipeline Integration Test

**Files:**
- Test: `test/test_pdf_pipeline.py`

End-to-end test: given a known paper, download PDF, parse citation contexts, verify extracted text matches expected content.

```bash
git commit -m "test: end-to-end PDF download + parse pipeline test"
```

---

## M5: New Phase 4

### Task 13: New Citation Description Skill

**Files:**
- Create: `citationclaw/skills/phase4_citation_extract.py`
- Modify: `citationclaw/skills/registry.py`
- Test: `test/test_phase4_citation_extract.py`

**Core:** PDF parse → lightweight LLM extract (no search).

```python
class CitationExtractSkill:
    name = "phase4_citation_extract"

    async def run(self, ctx, **kwargs):
        # For each citing paper:
        #   1. Download PDF (multi-source, cached)
        #   2. Parse citation contexts (local, no LLM)
        #   3. If contexts found → LLM extracts description from parsed text
        #   4. If no PDF available → mark as "PDF不可用", no LLM fallback
        #   5. Cache result with source tag
```

```bash
git commit -m "feat: new Phase 4 using PDF parse + lightweight LLM extract"
```

---

### Task 14: Citation Description Cache Update

**Files:**
- Modify: `citationclaw/core/citing_description_cache.py`

**Change:** Add `source` field to cache entries. Support `"pdf"` | `"llm"` source tags.

```bash
git commit -m "feat: add source attribution to citation description cache"
```

---

## M6: Playwright Browser Search

### Task 15: Playwright Browser Manager

**Files:**
- Create: `citationclaw/core/browser_manager.py`
- Test: `test/test_browser_manager.py`

**Dependencies:** Add `playwright` to requirements.txt

```python
class BrowserManager:
    """Manage Playwright browser lifecycle and tab pool."""

    async def init(self, headless: bool = True, proxy: Optional[str] = None):
        """Launch browser with proxy detection/configuration."""

    async def search_google(self, query: str) -> str:
        """Search Google, return result page HTML text."""

    async def get_page_text(self, url: str) -> str:
        """Navigate to URL, return visible text content."""

    async def close(self):
        """Close browser and all tabs."""
```

**Proxy handling:**
- Detect system proxy (HTTP_PROXY/HTTPS_PROXY)
- User config: `use_proxy: auto | direct | custom`
- Pass to Playwright `browser.launch(proxy={...})`

```bash
git commit -m "feat: Playwright browser manager with proxy detection and tab pool"
```

---

### Task 16: Scholar Search Agent

**Files:**
- Create: `citationclaw/core/scholar_search_agent.py`
- Test: `test/test_scholar_search_agent.py`

**Core:** Autonomous multi-step search with sufficiency check.

```python
class ScholarSearchAgent:
    """Search for a scholar's titles/honors using browser + LLM integration."""

    async def search(self, name: str, affiliation: str,
                     h_index: int, citation_count: int) -> dict:
        """
        Multi-step search with autonomous sufficiency judgment.

        Steps (configured in search_strategy.yaml):
          1. Google Scholar profile page
          2. Google search "name affiliation Fellow OR award"
          3. Institution official page (if needed)

        Each step:
          - Browser fetches real web page
          - LLM extracts relevant info from page text
          - LLM judges: is info sufficient? (Y/N)
          - If sufficient, stop early

        Limits: max 3 rounds, 20s timeout per scholar.
        """
```

```bash
git commit -m "feat: autonomous scholar search agent with sufficiency checking"
```

---

### Task 17: Scholar Search Agent Validation

**Files:**
- Create: `test/test_scholar_search_validation.py`
- Create: `test/fixtures/known_scholars.json`

**Purpose:** A/B validation of browser search approach vs accuracy benchmark.

```json
// test/fixtures/known_scholars.json
[
  {"name": "Andrew Ng", "affiliation": "Stanford", "expected_tier": "Industry Leader",
   "expected_honors": ["Stanford Professor", "Google Brain co-founder"]},
  {"name": "张钹", "affiliation": "清华大学", "expected_tier": "Academician",
   "expected_honors": ["中国科学院院士"]}
]
```

Run against 20 known scholars, measure time and accuracy.

```bash
git commit -m "test: scholar search agent validation with known scholars benchmark"
```

---

## M7: New Phase 3

### Task 18: Rule-Based Pre-Filter

**Files:**
- Create: `citationclaw/core/scholar_prefilter.py`
- Test: `test/test_scholar_prefilter.py`

**Core:** Use S2/OpenAlex h-index + institution matching to filter candidates.

```python
class ScholarPreFilter:
    def __init__(self, rules: dict):
        self.h_threshold = rules["pre_filter"]["h_index_threshold"]
        self.cite_threshold = rules["pre_filter"]["citation_threshold"]

    def is_candidate(self, author: dict) -> bool:
        """Rule-based pre-filter: should this author be deeply searched?"""
        if author.get("h_index", 0) >= self.h_threshold:
            return True
        if author.get("citation_count", 0) >= self.cite_threshold:
            return True
        if self._matches_institution(author.get("affiliation", "")):
            return True
        return False
```

```bash
git commit -m "feat: rule-based scholar pre-filter using structured API data"
```

---

### Task 19: New Phase 3 Skill — Scholar Assessment

**Files:**
- Create: `citationclaw/skills/phase3_scholar_assess.py`
- Modify: `citationclaw/skills/registry.py`
- Test: `test/test_phase3_scholar_assess.py`

**Core:** Pre-filter → browser search (candidates only) → LLM assess from real data.

```python
class ScholarAssessSkill:
    name = "phase3_scholar_assess"

    async def run(self, ctx, **kwargs):
        # 1. Load Phase 2 metadata results
        # 2. Deduplicate authors across all papers
        # 3. Pre-filter: rule engine identifies candidates
        # 4. For candidates: browser search + LLM assess (parallel, 5 tabs)
        # 5. For non-candidates: mark as "regular scholar"
        # 6. Cache all results in scholar_cache.json
        # 7. Write enriched JSONL with scholar tier annotations
```

```bash
git commit -m "feat: new Phase 3 scholar assessment with pre-filter + browser search"
```

---

## M8: Pipeline Integration & Frontend

### Task 20: Task Executor Rewrite

**Files:**
- Modify: `citationclaw/app/task_executor.py`
- Focus: `execute_for_titles()` method

**Changes:**
- Replace old Phase 2 (AuthorIntelSkill) with Phase 2 (MetadataCollectionSkill)
- Add new Phase 3 (ScholarAssessSkill) between metadata and export
- Replace old Phase 4 (CitationDescriptionSkill) with new PDF-based Phase 4
- Implement streaming pipeline: Phase 2 → Phase 3 flows per-paper
- Add "retry failed only" capability
- Update progress reporting for new phase structure

```bash
git commit -m "refactor: rewrite task executor for new 5-phase pipeline"
```

---

### Task 21: Frontend — Provider Setup Wizard

**Files:**
- Modify: `citationclaw/templates/index.html`
- Modify: `citationclaw/static/js/main.js`
- Modify: `citationclaw/app/main.py` — add `/api/providers` endpoint

**Changes:**
- Add provider preset selector with auto-fill
- Simplify to single model field
- Add S2 API key optional field
- Add analysis dimension checkboxes
- Add "retry failed" button in task status panel

```bash
git commit -m "feat: frontend setup wizard with provider presets and dimension selector"
```

---

### Task 22: Dependencies & Cleanup

**Files:**
- Modify: `requirements.txt`
- Modify: `pyproject.toml`

**New dependencies:**
- `PyMuPDF>=1.24.0` — PDF parsing
- `playwright>=1.40.0` — browser automation
- `pyyaml>=6.0` — YAML config parsing

**Post-install hook:**
- `playwright install chromium` — auto-install browser

**Cleanup:**
- Remove old `author_searcher_legacy.py` references
- Update README with new architecture

```bash
git commit -m "chore: update dependencies for new pipeline (PyMuPDF, Playwright, PyYAML)"
```

---

## Execution Order & Dependencies

```
M1 (Config) ────────────────────────────────────────────┐
  Task 1 (Prompts)                                      │
  Task 2 (Providers)                                    │
  Task 3 (Rules)                                        │
                                                        ▼
M2 (API Clients) ───────────────────────┐         M4 (PDF) ──────────┐
  Task 4 (OpenAlex)                     │           Task 10 (Download)│
  Task 5 (S2)                           │           Task 11 (Parse)   │
  Task 6 (arXiv)                        │           Task 12 (Test)    │
  Task 7 (Collector)                    │                             │
              │                         │                             │
              ▼                         │                             ▼
M3 (Phase 2) ──────────┐               │         M5 (Phase 4) ──────┐
  Task 8 (Skill)        │               │           Task 13 (Skill)  │
  Task 9 (Self-cite)    │               │           Task 14 (Cache)  │
              │         │               │                    │       │
              ▼         │               ▼                    │       │
M6 (Browser) ──────────┐│         M7 (Phase 3) ─────────────┘       │
  Task 15 (Manager)     ││           Task 18 (Pre-filter)            │
  Task 16 (Agent)       ││           Task 19 (Skill)                 │
  Task 17 (Validate)    ││                    │                      │
              │         ││                    │                      │
              ▼         ▼▼                    ▼                      ▼
M8 (Integration) ───────────────────────────────────────────────────┐
  Task 20 (Executor rewrite)                                        │
  Task 21 (Frontend wizard)                                         │
  Task 22 (Dependencies)                                            │
└───────────────────────────────────────────────────────────────────┘
```

**Parallelizable:** M2 and M4 can be developed in parallel. M6 can start after M1.
**Sequential:** M3 depends on M2. M5 depends on M4. M7 depends on M2+M6. M8 depends on all.
