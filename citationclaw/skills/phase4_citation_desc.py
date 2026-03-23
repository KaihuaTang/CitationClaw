from __future__ import annotations

from pathlib import Path

from citationclaw.skills.base import SkillContext, SkillResult
from citationclaw.core.citing_description_cache import CitingDescriptionCache
from citationclaw.core.citing_description_searcher import CitingDescriptionSearcher


class CitationDescriptionSkill:
    name = "phase4_citation_desc"

    async def run(self, ctx: SkillContext, **kwargs) -> SkillResult:
        try:
            return await self._run_inner(ctx, **kwargs)
        except Exception as e:
            ctx.log(f"[Phase4] fatal error: {e}")
            raise

    async def _run_inner(self, ctx: SkillContext, **kwargs) -> SkillResult:
        config = ctx.config
        input_excel = Path(kwargs["input_excel"])
        output_excel = Path(kwargs["output_excel"])
        parallel_workers = kwargs.get("parallel_workers", config.parallel_author_search)
        quota_event = kwargs.get("quota_event")

        desc_cache = kwargs.get("desc_cache") or CitingDescriptionCache()
        desc_searcher = CitingDescriptionSearcher(
            api_key=config.openai_api_key,
            base_url=config.openai_base_url,
            model=config.openai_model,
            log_callback=ctx.log,
            progress_callback=ctx.progress or (lambda _c, _t: None),
            cache=desc_cache,
            cancel_event=quota_event,
        )

        _cancel_check = ctx.cancel_check
        _quota = quota_event

        def _combined_cancel():
            if _cancel_check and _cancel_check():
                return True
            if _quota is not None and _quota.is_set():
                return True
            return False

        await desc_searcher.search(
            input_excel=input_excel,
            output_excel=output_excel,
            parallel_workers=parallel_workers,
            cancel_check=_combined_cancel,
        )
        stats = desc_cache.stats()
        return SkillResult(
            name=self.name,
            data={
                "output_excel": str(output_excel),
                "cache_stats": stats,
            },
        )
