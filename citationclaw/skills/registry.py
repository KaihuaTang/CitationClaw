from __future__ import annotations

from citationclaw.skills.base import Skill
from citationclaw.skills.phase1_citation_fetch import CitationFetchSkill
from citationclaw.skills.phase2_author_intel import AuthorIntelSkill
from citationclaw.skills.phase3_export import ExportSkill
from citationclaw.skills.phase4_citation_desc import CitationDescriptionSkill
from citationclaw.skills.phase5_report import ReportGenerateSkill


class SkillRegistry:
    """Simple registry for pipeline skills."""

    def __init__(self):
        self._skills = {}

    def register(self, skill):
        if not hasattr(skill, "name") or not hasattr(skill, "run"):
            raise TypeError(
                f"Expected a Skill with 'name' and 'run' attributes, got {type(skill).__name__}"
            )
        self._skills[skill.name] = skill

    def get(self, name: str):
        if name not in self._skills:
            raise KeyError(f"Unknown skill: {name}")
        return self._skills[name]


def build_default_registry() -> SkillRegistry:
    reg = SkillRegistry()
    reg.register(CitationFetchSkill())
    reg.register(AuthorIntelSkill())
    reg.register(ExportSkill())
    reg.register(CitationDescriptionSkill())
    reg.register(ReportGenerateSkill())
    return reg
