"""Phase 4: 引文语境提取 — PDF parse + lightweight LLM extract.

Rewritten for accuracy:
- Reuses PDFs already downloaded in Phase 2 (no re-download)
- Section-tagged paragraph extraction with context window
- Supports both [N] and (Author, Year) citation formats
- Uses lightweight LLM (dashboard_model) — no search needed
- Parallel processing with semaphore
"""
import asyncio
import json
from pathlib import Path
from typing import Optional, List

from citationclaw.skills.base import SkillContext, SkillResult
from citationclaw.core.pdf_downloader import PDFDownloader
from citationclaw.core.pdf_parser import PDFCitationParser
from citationclaw.core.pdf_mineru_parser import MinerUParser
from citationclaw.core.pdf_parse_cache import PDFParseCache
from citationclaw.config.prompt_loader import PromptLoader


class CitationExtractSkill:
    name = "phase4_citation_extract"

    async def run(self, ctx: SkillContext, **kwargs) -> SkillResult:
        input_file = Path(kwargs["input_file"])
        output_file = Path(kwargs["output_file"])
        target_title = kwargs["target_title"]
        target_authors = kwargs.get("target_authors", [])
        target_year = kwargs.get("target_year")
        cache = kwargs.get("citation_desc_cache")
        # Phase 2 already downloaded PDFs — reuse them
        phase2_pdf_paths: Optional[list] = kwargs.get("pdf_paths")

        parser = PDFCitationParser()
        mineru_parser = MinerUParser()
        parse_cache = PDFParseCache()
        prompt_loader = PromptLoader()

        papers = self._read_jsonl(input_file)
        total = len(papers)
        results = []
        stats = {"total": total, "pdf_found": 0, "pdf_missing": 0,
                 "cached": 0, "extracted": 0, "no_context": 0, "self_cite_skip": 0}

        # Determine LLM model: prefer lightweight (dashboard_model), fallback to openai_model
        llm_model = getattr(ctx.config, 'dashboard_model', '') or ctx.config.openai_model

        # Prepare downloader only if Phase 2 didn't pass PDF paths
        downloader = None
        if not phase2_pdf_paths:
            downloader = PDFDownloader()

        try:
            # Parallel processing
            sem = asyncio.Semaphore(5)
            result_slots: List[Optional[dict]] = [None] * total

            async def _process_one(i: int, paper: dict):
                async with sem:
                    if ctx.cancel_check and ctx.cancel_check():
                        result_slots[i] = paper
                        return

                    citing_title = paper.get("Paper_Title", paper.get("title", ""))

                    # Skip self-citation papers
                    is_self = paper.get("Is_Self_Citation")
                    if is_self and str(is_self).lower() not in ('false', '0', 'nan', 'none', ''):
                        paper["Citing_Description"] = "自引论文，已跳过"
                        paper["citing_desc_source"] = "self_citation_skip"
                        stats["self_cite_skip"] += 1
                        result_slots[i] = paper
                        if ctx.progress:
                            ctx.progress(i + 1, total)
                        return

                    # Check cache (args: paper_link, citing_paper_title, target_title)
                    if cache:
                        cached_desc = cache.get(
                            paper.get("Paper_Link", ""),
                            citing_title,
                            target_title,
                        )
                        if cached_desc:
                            paper["Citing_Description"] = cached_desc
                            paper["citing_desc_source"] = "cache"
                            stats["cached"] += 1
                            result_slots[i] = paper
                            if ctx.progress:
                                ctx.progress(i + 1, total)
                            return

                    # Get citation contexts from PDF
                    contexts = await self._get_contexts(
                        i, paper, parser, mineru_parser, parse_cache,
                        downloader, phase2_pdf_paths,
                        target_title, target_authors, target_year,
                        ctx, stats,
                    )

                    if contexts:
                        # LLM extracts description from parsed text
                        description = await self._llm_extract(
                            ctx, prompt_loader, llm_model,
                            citing_title, target_title, contexts,
                        )
                        paper["Citing_Description"] = description
                        paper["citing_desc_source"] = "pdf"
                        stats["extracted"] += 1
                    else:
                        paper["Citing_Description"] = "未在PDF中找到相关引用描述"
                        paper["citing_desc_source"] = "pdf_no_context"
                        stats["no_context"] += 1

                    # Cache result (args: paper_link, citing_title, target_title, desc)
                    if cache and paper.get("Citing_Description"):
                        desc = paper["Citing_Description"]
                        if desc not in ("未在PDF中找到相关引用描述", "PDF不可用", "LLM提取失败"):
                            await cache.update(
                                paper.get("Paper_Link", ""),
                                citing_title,
                                target_title,
                                desc,
                            )

                    result_slots[i] = paper
                    if ctx.progress:
                        ctx.progress(i + 1, total)

            await asyncio.gather(*[_process_one(i, p) for i, p in enumerate(papers)])

        finally:
            if downloader:
                await downloader.close()
            if cache:
                await cache.flush()

        # Collect results in order
        results = [r for r in result_slots if r is not None]

        # Write output
        output_file.parent.mkdir(parents=True, exist_ok=True)
        with open(output_file, "w", encoding="utf-8") as f:
            for r in results:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

        ctx.log(
            f"[引文语境] 完成: 提取 {stats['extracted']} / "
            f"无上下文 {stats['no_context']} / PDF缺失 {stats['pdf_missing']} / "
            f"缓存 {stats['cached']} / 自引跳过 {stats['self_cite_skip']}"
        )

        return SkillResult(name=self.name, data={
            "output_file": str(output_file),
            **stats,
        })

    async def _get_contexts(
        self, idx: int, paper: dict,
        parser: PDFCitationParser, mineru_parser: MinerUParser,
        parse_cache: PDFParseCache, downloader: Optional[PDFDownloader],
        phase2_pdf_paths: Optional[list],
        target_title: str, target_authors: list,
        target_year: Optional[int],
        ctx: SkillContext, stats: dict,
    ) -> List[dict]:
        """Get citation contexts from PDF — reuse Phase 2 downloads where possible."""
        citing_title = paper.get("Paper_Title", paper.get("title", ""))
        pkey = mineru_parser.paper_key(paper)

        # Try 1: MinerU parse cache (already parsed in Phase 2)
        if parse_cache.has(pkey):
            parse_dir = parse_cache.get_parsed_dir(pkey)
            cached_parsed = mineru_parser._load_cached(parse_dir)
            if cached_parsed and cached_parsed.get("full_md"):
                contexts = parser.extract_from_text(
                    cached_parsed["full_md"],
                    target_title, target_authors, target_year,
                    context_window=1,
                )
                if contexts:
                    stats["pdf_found"] += 1
                    return contexts

        # Try 2: Phase 2 PDF path (already downloaded)
        pdf_path = None
        if phase2_pdf_paths and idx < len(phase2_pdf_paths):
            pdf_path = phase2_pdf_paths[idx]

        # Try 3: Download fresh if no Phase 2 path
        if not pdf_path and downloader:
            pdf_path = await downloader.download(paper, log=ctx.log)

        if not pdf_path:
            stats["pdf_missing"] += 1
            paper["Citing_Description"] = "PDF不可用"
            paper["citing_desc_source"] = "unavailable"
            return []

        stats["pdf_found"] += 1

        # Parse PDF and extract contexts
        contexts = parser.extract_citation_contexts(
            Path(pdf_path) if isinstance(pdf_path, str) else pdf_path,
            target_title, target_authors, target_year,
            context_window=1,
        )
        return contexts

    async def _llm_extract(
        self, ctx: SkillContext, prompt_loader: PromptLoader,
        model: str, citing_title: str, target_title: str,
        contexts: List[dict],
    ) -> str:
        """Use lightweight LLM to extract citation description from parsed contexts.

        Returns structured string: "[Section] description text 【sentiment引用】"
        or "未在已解析文本中找到相关引用描述" if not found.
        """
        try:
            from openai import AsyncOpenAI
            from citationclaw.core.http_utils import make_async_client
            import re

            # Build structured context text with section tags and match type
            parts = []
            for c in contexts:
                if c.get("match_type") == "direct":
                    tag = "★"
                elif c.get("match_type") == "ref_entry":
                    tag = ""  # Reference entry, included for key identification
                else:
                    tag = ""
                parts.append(f"[{c['section']}]{tag} {c['text']}")
            parsed_paragraphs = "\n\n".join(parts)

            prompt = prompt_loader.render(
                "citation_extract",
                citing_title=citing_title,
                target_title=target_title,
                parsed_paragraphs=parsed_paragraphs,
            )

            client = AsyncOpenAI(
                api_key=ctx.config.openai_api_key,
                base_url=(ctx.config.openai_base_url or "").rstrip("/") + "/",
                http_client=make_async_client(timeout=60.0),
            )
            response = await client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
            )
            text = response.choices[0].message.content.strip()

            # Parse JSON response
            text_clean = re.sub(r'```json\s*', '', text)
            text_clean = re.sub(r'```\s*', '', text_clean).strip()
            try:
                data = json.loads(text_clean)
                if isinstance(data, dict):
                    if not data.get("found", True):
                        return "未在已解析文本中找到相关引用描述"

                    desc = data.get("description", "").strip()
                    section = data.get("section", "").strip()
                    sentiment = data.get("sentiment", "中性").strip()
                    citation_key = data.get("citation_key", "").strip()

                    # Validate: description must not be a reference entry (author list + year + journal)
                    if desc and self._looks_like_ref_entry(desc):
                        return "未在已解析文本中找到相关引用描述"

                    if not desc:
                        return "未在已解析文本中找到相关引用描述"

                    # Build clean output
                    result_parts = []
                    if section and section not in ("References", "参考文献"):
                        result_parts.append(f"[{section}]")
                    result_parts.append(desc)
                    if sentiment == "正面":
                        result_parts.append("【正面引用】")
                    elif sentiment == "负面":
                        result_parts.append("【负面引用】")
                    # 中性 = no tag (default)

                    return " ".join(result_parts)
            except (json.JSONDecodeError, ValueError):
                pass

            return text
        except Exception as e:
            ctx.log(f"  ⚠ LLM提取失败: {e}")
            return "LLM提取失败"

    @staticmethod
    def _looks_like_ref_entry(text: str) -> bool:
        """Check if text looks like a reference list entry rather than a body citation.

        Reference entries typically contain: author names, year, journal/conference, volume.
        Body citations are sentences that DISCUSS the cited work.
        """
        import re
        text_lower = text.strip().lower()
        # Starts with author names followed by year pattern (typical ref format)
        if re.match(r'^[A-Z][a-z]+,?\s+[A-Z]', text.strip()):
            # Has journal/conference indicators
            if any(k in text_lower for k in [
                "arxiv", "preprint", "proceedings", "journal", "conference",
                "ieee", "acm", "iclr", "icml", "neurips", "cvpr", "iccv",
                "aaai", "emnlp", "acl", "pp.", "vol.", "no."
            ]):
                return True
        # Very long text with many author names (likely a ref entry copied verbatim)
        if len(text) > 200 and text.count(",") > 5 and re.search(r'\d{4}[a-z]?\.?\s*$', text.strip()):
            return True
        return False

    def _read_jsonl(self, path: Path) -> list:
        """Read JSONL, handling both flat and legacy wrapped {idx: record} formats."""
        papers = []
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                data = json.loads(line)
                if isinstance(data, dict):
                    inner = data
                    for v in data.values():
                        if isinstance(v, dict) and ("Paper_Title" in v or "paper_title" in v):
                            inner = v
                            break
                    papers.append(inner)
                else:
                    papers.append(data)
        return papers
