from pathlib import Path

from app.skills import SKILL_NAMES, load_support_skills


def test_all_product_skills_are_loaded_in_stable_order() -> None:
    prompt = load_support_skills()

    assert SKILL_NAMES == (
        "needs-discovery",
        "offer-aid",
        "collect-contact",
        "crisis-escalation",
        "verified-information",
        "follow-up",
        "level-two-support",
    )
    positions = [prompt.index(f"# Skill: {name}") for name in SKILL_NAMES]
    assert positions == sorted(positions)


def test_skills_encode_tone_buttons_and_human_invariants() -> None:
    prompt = load_support_skills()

    assert "мотивационного интервью" in prompt.lower()
    assert "не более четырёх" in prompt.lower()
    assert "поговорить с живым человеком" in prompt.lower()
    assert "8-800-2000-122" in prompt
    assert "не придумывай" in prompt.lower()
    assert all((Path("skills") / name / "SKILL.md").is_file() for name in SKILL_NAMES)

