"""Run candidate or deployed-webhook acceptance on the verified VM, secrets stay there."""

import argparse
import json
import os
import shlex
import subprocess
import tarfile
import tempfile
import uuid
from pathlib import Path

from scripts.resolve_prod_host import resolve_prod_host, verify_ssh_host

CONTAINER = "women-help-chatwoot_agent-bot_1"


def run(mode: str, suites: list[str]) -> int:
    host = verify_ssh_host(resolve_prod_host())
    run_id = uuid.uuid4().hex
    directory = Path(".runtime/chatwoot-tests") / run_id
    directory.mkdir(parents=True, mode=0o700)
    remote = f"/tmp/women-help-acceptance-{run_id}"
    ssh = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host]
    with tempfile.TemporaryDirectory(prefix="fh-acceptance-") as temporary:
        archive = Path(temporary) / "candidate.tar.gz"
        with tarfile.open(archive, "w:gz") as bundle:
            for source in ("app", "scripts", "tests", "skills", "knowledge",
                           "docs/nevidimy-bot-scenario-final.html", "pyproject.toml"):
                bundle.add(source, filter=lambda item: None if "__pycache__" in item.name else item)
        subprocess.run(["scp", "-q", str(archive), f"{host}:{remote}.tar.gz"], check=True)
        subprocess.run([*ssh,
            (f"sudo podman cp {remote}.tar.gz {CONTAINER}:{remote}.tar.gz && "
            f"sudo podman exec {CONTAINER} mkdir -m 700 {remote} && "
            f"sudo podman exec {CONTAINER} tar -xzf {remote}.tar.gz -C {remote}")], check=True)
    command = ["sudo", "podman", "exec", "-w", remote, CONTAINER, "/app/.venv/bin/python",
               "-m", "scripts.chatwoot_full_acceptance", "--mode", mode,
               "--output", f"{remote}/report.json"]
    if suites:
        command += ["--suites", *suites]
    result = subprocess.run([*ssh, shlex.join(command)], check=False)
    # Synthetic transcripts remain private and outside Git; preserve failures too.
    subprocess.run([*ssh,
        (f"sudo podman cp {CONTAINER}:{remote}/report.json {remote}.json && "
        f"sudo chown lebwa82:lebwa82 {remote}.json && sudo chmod 0600 {remote}.json")], check=True)
    report = directory / "report.json"
    subprocess.run(["scp", "-q", f"{host}:{remote}.json", str(report)], check=True)
    report.chmod(0o600)
    print(json.dumps({"mode": mode, "report": str(report.resolve()), "exit_code": result.returncode}))
    return result.returncode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["candidate", "webhook"], default="webhook")
    parser.add_argument("--suites", nargs="+", choices=[
        "journeys", "ownership", "classifications", "screens", "timers", "contextual", "attachments",
    ], default=[])
    args = parser.parse_args()
    os.umask(0o077)
    try:
        code = run(args.mode, args.suites)
    except Exception as error:  # noqa: BLE001 - never echo authentication/transport error bodies
        print(json.dumps({"passed": False, "error_type": type(error).__name__}))
        code = 1
    raise SystemExit(code)


if __name__ == "__main__":
    main()
