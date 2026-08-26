"""Validate the product-to-test traceability contract without importing bot runtime code."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

REQUIRED_CONTRACT_IDS = frozenset(
    {
        *(f"S{number:02}" for number in range(1, 20)),
        "R01_RED_FLAG_ROUTE",
        "R02_QWEN_ONLY_CLASSIFICATION",
        "R03_CONTEXTUAL_BUTTONS",
        "R04_RESET_SEMANTICS",
        "R05_RUSSIAN_ONLY",
        "R06_PRIVACY_RETENTION",
        "R07_FOLLOWUP_AFTER_DELIVERY",
        "R08_CHATWOOT_HANDOFF",
    }
)


@dataclass(frozen=True)
class ProductContractItem:
    identifier: str
    status: str
    tests: tuple[str, ...]
    fixtures: tuple[str, ...]
    deferred_reason: str | None = None


def load_product_contract(path: Path) -> dict[str, ProductContractItem]:
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise ValueError("product contract cannot be read") from error
    if not isinstance(document, dict) or set(document) != {"requirements"}:
        raise ValueError("product contract has invalid root")
    rows = document["requirements"]
    if not isinstance(rows, list):
        raise TypeError("product contract requirements must be a list")
    result: dict[str, ProductContractItem] = {}
    for row in rows:
        item = _parse_item(row)
        if item.identifier in result:
            raise ValueError(f"duplicate product contract id: {item.identifier}")
        result[item.identifier] = item
    return result


def _parse_item(value: Any) -> ProductContractItem:
    if not isinstance(value, dict):
        raise TypeError("product contract requirement must be an object")
    allowed = {"id", "status", "tests", "fixtures", "deferred_reason"}
    if set(value) - allowed or not {"id", "status", "tests", "fixtures"} <= set(value):
        raise ValueError("product contract requirement has invalid keys")
    identifier = value["id"]
    status = value["status"]
    tests = value["tests"]
    fixtures = value["fixtures"]
    reason = value.get("deferred_reason")
    if not isinstance(identifier, str) or not identifier:
        raise ValueError("product contract id must be a string")
    if status not in {"implemented", "deferred"}:
        raise ValueError("product contract status is invalid")
    if not _string_list(tests) or not _string_list(fixtures):
        raise ValueError("product contract references must be string lists")
    if reason is not None and (not isinstance(reason, str) or not reason):
        raise ValueError("product contract deferred reason is invalid")
    if status == "implemented" and reason is not None:
        raise ValueError("implemented product contract item cannot have a deferred reason")
    if status == "deferred" and reason is None:
        raise ValueError("deferred product contract item needs a reason")
    return ProductContractItem(identifier, status, tuple(tests), tuple(fixtures), reason)


def _string_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) and item for item in value)
