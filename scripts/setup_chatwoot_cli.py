"""Authenticate the official CLI using the verified project VM, without exposing tokens."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from scripts.resolve_prod_host import resolve_prod_host, verify_ssh_host


def cli_binary() -> str:
    installed = shutil.which("chatwoot")
    if installed:
        return installed
    binary = Path.home() / ".local/bin/chatwoot"
    if not binary.is_file():
        raise RuntimeError("Install the official CLI: https://developers.chatwoot.com/cli")
    return str(binary)


# Only these fields leave the server, over SSH into a captured pipe. No env dump.
REMOTE_CONFIG = """
import importlib.util, json
from pathlib import Path
spec = importlib.util.spec_from_file_location(
    'activation', '/opt/women-help-chatwoot/deploy/chatwoot/activate.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
agent = module.env(Path('/etc/women-help-agent.env'))
print(json.dumps({
    'token': agent['CHATWOOT_READ_TOKEN'],
    'account_id': int(agent['CHATWOOT_ACCOUNT_ID']),
}))
"""


def login(binary: str, host: str, credentials: dict) -> None:
    address = host.rsplit("@", 1)[-1]
    base_url = f"https://chatwoot.{address.replace('.', '-')}.sslip.io"
    token = credentials["token"]
    account = int(credentials["account_id"])
    if not isinstance(token, str) or not token or any(c in token for c in "\r\n"):
        raise ValueError("Invalid API credential")
    if account != 1:
        raise ValueError("Unexpected Chatwoot account")
    # The CLI validates membership and saves its token in the OS keyring.
    # stdin (not argv or shell history) carries the API key.
    result = subprocess.run(
        [binary, "auth", "login"],
        input=f"{base_url}\n{token}\n{account}\n",
        capture_output=True, text=True, timeout=60, check=False,
    )
    if result.returncode:
        raise RuntimeError("CLI login failed; captured output was suppressed to protect credentials")
    print(json.dumps({"authenticated": True, "url": base_url, "account_id": account}))


def main() -> None:
    binary = cli_binary()
    host = verify_ssh_host(resolve_prod_host())
    result = subprocess.run(
        ["ssh", "-o", "StrictHostKeyChecking=accept-new", "-o", "BatchMode=yes",
         "-o", "ConnectTimeout=10", host, "sudo python3 -"],
        input=REMOTE_CONFIG, capture_output=True, text=True, timeout=30, check=False,
    )
    if result.returncode:
        raise RuntimeError("Could not obtain CLI credentials from the verified VM")
    login(binary, host, json.loads(result.stdout))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:  # noqa: BLE001 - no credential-bearing exception text
        print(json.dumps({"authenticated": False, "error_type": type(error).__name__}))
        raise SystemExit(1) from None
