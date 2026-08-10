from __future__ import annotations

import json
from pathlib import Path

RESOURCE_PATH = Path(__file__).parent.parent / "knowledge" / "verified_resources.json"


def find_verified_answer(query: str) -> str | None:
    data = json.loads(RESOURCE_PATH.read_text(encoding="utf-8"))
    words = set(query.lower().split())
    for resource in data["resources"]:
        if words.intersection(resource["topics"]):
            return resource["text"]
    return None

