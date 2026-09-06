"""Resolve the current public SSH address of the production VM through Yandex Cloud."""

from __future__ import annotations

import argparse
import ipaddress
import json
import subprocess
import sys
from typing import Any

PROD_VM_NAME = "female-homelessness-test"
PROD_VM_ID = "epdqi9moqfn8fqbe3g2n"
PROD_FOLDER_ID = "b1gepl5pmqr8f7hbu3bj"
DEFAULT_SSH_LOGIN = "lebwa82"


def public_ssh_host(instance: dict[str, Any], login: str = DEFAULT_SSH_LOGIN) -> str:
    for interface in instance.get("network_interfaces", []):
        address = interface.get("primary_v4_address", {}).get("one_to_one_nat", {}).get("address")
        if address:
            ipaddress.IPv4Address(address)
            return f"{login}@{address}"
    raise ValueError(f"VM {PROD_VM_NAME!r} has no public IPv4 address")


def resolve_prod_host() -> str:
    result = subprocess.run(
        ["yc", "compute", "instance", "get", "--id", PROD_VM_ID, "--format", "json"],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    instance = json.loads(result.stdout)
    if instance.get("id") != PROD_VM_ID or instance.get("folder_id") != PROD_FOLDER_ID:
        raise ValueError("Yandex Cloud returned a different project VM")
    return public_ssh_host(instance)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Resolve the project VM through Yandex Cloud")
    parser.add_argument("--ip-only", action="store_true")
    args = parser.parse_args()
    try:
        host = resolve_prod_host()
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        # Never surface cloud CLI output: it can contain account credentials.
        print(f"Cannot resolve project VM ({type(error).__name__}); check yc access.", file=sys.stderr)
        sys.exit(1)
    print(host.split("@", 1)[1] if args.ip_only else host)
