from pathlib import Path

from scripts.dialogue_eval import load_cases
from scripts.product_contract import REQUIRED_CONTRACT_IDS, load_product_contract

ROOT = Path(__file__).parents[1]
CONTRACT = ROOT / "tests" / "fixtures" / "product_contract.yaml"
DIALOGUES = ROOT / "tests" / "fixtures" / "dialogue_scenarios.jsonl"
LIVE_SAFETY_DIALOGUES = ROOT / "tests" / "fixtures" / "live_safety_scenarios.jsonl"


def test_product_contract_covers_every_spec_scenario_and_records_mvp_debt_explicitly() -> None:
    contract = load_product_contract(CONTRACT)
    dialogue_ids = {case.id for case in load_cases(DIALOGUES)}

    assert set(contract) == REQUIRED_CONTRACT_IDS
    for item in contract.values():
        if item.status == "implemented":
            assert item.tests
        else:
            assert item.deferred_reason
        assert set(item.fixtures) <= dialogue_ids
        for reference in item.tests:
            path, function = reference.split("::", 1)
            source = (ROOT / path).read_text(encoding="utf-8")
            assert f"def {function}" in source


def test_live_safety_dataset_is_a_small_traceable_subset_of_the_offline_contract() -> None:
    all_ids = {case.id for case in load_cases(DIALOGUES)}
    live_cases = load_cases(LIVE_SAFETY_DIALOGUES)

    assert {case.id for case in live_cases} == {
        "listen-02",
        "human-01",
        "aid-02",
        "crisis-05",
        "crisis-07",
        "suicide-01",
        "s11-child-custody",
    }
    assert {case.id for case in live_cases} <= all_ids
