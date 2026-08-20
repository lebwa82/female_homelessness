from __future__ import annotations

from functools import lru_cache
from pathlib import Path

SKILL_NAMES = (
    "needs-discovery",
    "offer-aid",
    "collect-contact",
    "crisis-escalation",
    "verified-information",
    "follow-up",
    "level-two-support",
)
SKILLS_ROOT = Path(__file__).resolve().parent.parent / "skills"


@lru_cache(maxsize=1)
def load_support_skills() -> str:
    sections: list[str] = []
    for name in SKILL_NAMES:
        body = (SKILLS_ROOT / name / "SKILL.md").read_text(encoding="utf-8")
        sections.append(f"# Skill: {name}\n\n{body}")
    return "\n\n---\n\n".join(sections)

