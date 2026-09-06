"""Update generated test hostnames only; never print runtime credentials.

Standalone stdlib script, also executed as root on the VM via SSH stdin.
"""

from __future__ import annotations

import argparse
import ipaddress
import os
import re
import sys
import tempfile
from pathlib import Path


def env_values(content: str) -> dict[str, str]:
    return {
        key: value.strip().strip("\"'")
        for line in content.splitlines()
        for key, separator, value in [line.partition("=")]
        if separator and not key.startswith("#")
    }


def generated_hostname(hostname: str, prefix: str) -> bool:
    return re.fullmatch(rf"{prefix}\.\d{{1,3}}(?:-\d{{1,3}}){{3}}\.sslip\.io", hostname) is not None


def replace_values(content: str, replacements: dict[str, str]) -> str:
    return "".join(
        f"{key}={replacements[key]}\n" if key in replacements else line
        for line in content.splitlines(keepends=True)
        for key in [line.partition("=")[0]]
    )


def atomic_write(path: Path, content: str) -> None:
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def update_addresses(ip: str, platform_env: Path, agent_env: Path) -> tuple[bool, str]:
    address = str(ipaddress.IPv4Address(ip)).replace(".", "-")
    content = platform_env.read_text()
    values = env_values(content)
    hostname = values.get("CHATWOOT_HOSTNAME", "")
    if not hostname:
        raise ValueError("CHATWOOT_HOSTNAME is missing")
    replacements = {}
    for key, prefix in (("CHATWOOT_HOSTNAME", "chatwoot"), ("AGENT_HOSTNAME", "agent")):
        current = values.get(key, "")
        updated = f"{prefix}.{address}.sslip.io"
        if generated_hostname(current, prefix) and current != updated:
            replacements[key] = updated

    # Internal API URLs and custom domains are kept as configured by the operator.
    agent_changed = False
    if agent_env.is_file() and "CHATWOOT_HOSTNAME" in replacements:
        agent_content = agent_env.read_text()
        base_url = env_values(agent_content).get("CHATWOOT_BASE_URL", "")
        if base_url.rstrip("/") == f"https://{hostname}":
            agent_updated = replace_values(agent_content, {
                "CHATWOOT_BASE_URL": f"https://{replacements['CHATWOOT_HOSTNAME']}",
            })
            atomic_write(agent_env, agent_updated)
            agent_changed = True
    if replacements:
        atomic_write(platform_env, replace_values(content, replacements))
    return bool(replacements) or agent_changed, f"https://{replacements.get('CHATWOOT_HOSTNAME', hostname)}/"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ip")
    args = parser.parse_args()
    try:
        changed, url = update_addresses(
            args.ip, Path("/etc/women-help-chatwoot.env"), Path("/etc/women-help-agent.env"),
        )
    except (OSError, ValueError) as error:
        print(f"Address update failed ({type(error).__name__}).", file=sys.stderr)
        sys.exit(1)
    print("changed" if changed else "unchanged")
    print(url)


if __name__ == "__main__":
    main()
