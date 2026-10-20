"""One Compose entrypoint for the active Timeweb/managed-PostgreSQL contour.

The root-only platform env file selects TLS for both systemd and releases.
Never source it as shell code or print its contents.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = Path("/etc/women-help-chatwoot.env")
HTTP_LOCAL_DROPIN = Path("/etc/systemd/system/nevidimy.service.d/http-local.conf")
DOMAIN_HTTPS_DROPIN = Path("/etc/systemd/system/nevidimy.service.d/public-domain-https.conf")
POSTGRES_CA = Path("/opt/nevidimy/certs/postgresql-ca.crt")


def compose_files(
    env_file: Path, *, http_local: bool = False, domain_https: bool = False
) -> tuple[list[str], dict[str, str]]:
    if http_local and domain_https:
        raise ValueError("Select exactly one UI overlay")
    selectors = {"CHATWOOT_INTERNAL_TLS", "INTERNAL_TLS_DIR", "POSTGRES_TLS_CA_FILE"}
    values = {}
    webhook = ""
    for line in env_file.read_text().splitlines():
        key, separator, value = line.partition("=")
        if separator and key.strip() in selectors:
            values[key.strip()] = value.strip().strip("\"'")
        elif separator and key.strip() == "NEVIDIMY_AGENT_WEBHOOK_URL":
            webhook = value.strip().strip("\"'")
    mode = values.get("CHATWOOT_INTERNAL_TLS", "0")
    if mode not in {"0", "1"}:
        raise ValueError("CHATWOOT_INTERNAL_TLS must be 0 or 1")
    files = [
        "deploy/chatwoot/compose.yml",
        "deploy/chatwoot/compose.managed.yml",
        "deploy/timeweb/compose.yml",
    ]
    if http_local:
        files.append("deploy/timeweb/compose.http-local.yml")
    if domain_https:
        files.append("deploy/timeweb/compose.domain-https.yml")
    if mode == "0":
        if values.get("INTERNAL_TLS_DIR") or values.get("POSTGRES_TLS_CA_FILE"):
            raise ValueError("TLS paths are configured but CHATWOOT_INTERNAL_TLS is not 1")
        return files, values

    directory = Path(values.get("INTERNAL_TLS_DIR", ""))
    if not directory.is_absolute():
        raise ValueError("An absolute INTERNAL_TLS_DIR is required")
    if values.get("POSTGRES_TLS_CA_FILE") != str(POSTGRES_CA) or not POSTGRES_CA.is_file():
        raise ValueError("Timeweb managed PostgreSQL requires its existing provider CA")
    if POSTGRES_CA.resolve() == (directory / "ca.crt").resolve():
        raise ValueError("The application CA must not replace the managed PostgreSQL CA")
    if not re.fullmatch(
        r"https://agent-bot:8080/webhooks/chatwoot/agent/[A-Za-z0-9_-]{32,128}", webhook
    ):
        raise ValueError(
            "The signed internal webhook must be migrated to HTTPS before enabling TLS"
        )
    required = ["ca.crt", "ca-bundle.pem"]
    required += [
        f"{service}.{suffix}"
        for service in ("chatwoot", "agent-bot", "redis")
        for suffix in ("crt", "key")
    ]
    for name in required:
        path = directory / name
        if not path.is_file() or not path.stat().st_size:
            raise ValueError("The internal TLS certificate set is incomplete")
        if name.endswith(".key") and path.stat().st_mode & 0o077:
            raise ValueError("Internal TLS private keys must not be group/world accessible")
    files.append("deploy/chatwoot/compose.tls.yml")
    if http_local:
        files.append("deploy/timeweb/compose.tls-http-local.yml")
    if domain_https:
        files.append("deploy/timeweb/compose.tls-domain-https.yml")
    return files, values


def main() -> int:
    try:
        domain_https = DOMAIN_HTTPS_DROPIN.is_file()
        files, values = compose_files(
            ENV_FILE,
            http_local=HTTP_LOCAL_DROPIN.is_file() and not domain_https,
            domain_https=domain_https,
        )
        # Do not allow a caller to replace the persistent file selection.
        if any(
            arg.split("=", 1)[0] in {"-f", "--file", "--env-file", "--project-directory"}
            or arg.startswith("-f")
            for arg in sys.argv[1:]
        ):
            raise ValueError("Compose files and environment are selected by the platform env file")
        command = ["docker", "compose", "--env-file", str(ENV_FILE)]
        for name in files:
            command.extend(["-f", name])
        environment = dict(os.environ)
        for name in ("CHATWOOT_INTERNAL_TLS", "INTERNAL_TLS_DIR", "POSTGRES_TLS_CA_FILE"):
            environment.pop(name, None)
        environment.update(values)
        return subprocess.call([*command, *sys.argv[1:]], cwd=ROOT, env=environment)
    except (OSError, ValueError):
        # Neither environment values nor subprocess arguments belong in errors.
        print(
            "Chatwoot Compose configuration failed; check TLS mode and certificate files.",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    sys.exit(main())
