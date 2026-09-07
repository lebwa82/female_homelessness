"""Resolve the current public SSH address of the production VM through Yandex Cloud."""

from __future__ import annotations

import argparse
import ipaddress
import json
import re
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


def verify_ssh_host(host: str) -> str:
    """Refuse remote mutations unless the SSH peer is the permanent project VM."""
    login, separator, address = host.rpartition("@")
    if not separator:
        login, address = DEFAULT_SSH_LOGIN, host
    if not re.fullmatch(r"[a-z_][a-z0-9_-]*", login):
        raise ValueError("Invalid SSH login")
    target = f"{login}@{ipaddress.IPv4Address(address)}"
    result = subprocess.run(
        [
            "ssh",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=10",
            target,
            (
                "curl -fsS --max-time 5 -H Metadata-Flavor:Google "
                "http://169.254.169.254/computeMetadata/v1/instance/id"
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    )
    if result.stdout.strip() != PROD_VM_ID:
        raise ValueError("SSH peer is not the project VM")
    return target


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Resolve the project VM through Yandex Cloud")
    parser.add_argument("--ip-only", action="store_true")
    parser.add_argument(
        "--verify-ssh", metavar="HOST", help="Verify VM identity over SSH before changes"
    )
    args = parser.parse_args()
    try:
        host = verify_ssh_host(args.verify_ssh) if args.verify_ssh else resolve_prod_host()
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        # Never surface cloud CLI output: it can contain account credentials.
        print(
            f"Project VM resolution/identity check failed ({type(error).__name__}).",
            file=sys.stderr,
        )
        sys.exit(1)
    print(host.split("@", 1)[1] if args.ip_only else host)
