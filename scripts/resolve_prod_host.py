"""Resolve the current public SSH address of the production VM through Yandex Cloud."""

from __future__ import annotations

import json
import subprocess
from typing import Any

PROD_VM_NAME = "female-homelessness-test"
DEFAULT_SSH_LOGIN = "lebwa82"


def public_ssh_host(instance: dict[str, Any], login: str = DEFAULT_SSH_LOGIN) -> str:
    for interface in instance.get("network_interfaces", []):
        address = interface.get("primary_v4_address", {}).get("one_to_one_nat", {}).get("address")
        if address:
            return f"{login}@{address}"
    raise ValueError(f"VM {PROD_VM_NAME!r} has no public IPv4 address")


def resolve_prod_host(vm_name: str = PROD_VM_NAME) -> str:
    result = subprocess.run(
        ["yc", "compute", "instance", "get", "--name", vm_name, "--format", "json"],
        check=True,
        capture_output=True,
        text=True,
    )
    return public_ssh_host(json.loads(result.stdout))


if __name__ == "__main__":
    print(resolve_prod_host())
