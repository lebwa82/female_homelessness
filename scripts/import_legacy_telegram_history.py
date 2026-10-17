"""Stage legacy text history in an isolated Chatwoot inbox on the verified VM.

No Telegram token is changed. No old process is stopped. Dry-run is the default.
The source dump is parsed locally, never executed, and never copied to the VM.
"""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
from pathlib import Path

from scripts.legacy_telegram_history import ImportConflict, load_history, summary
from scripts.resolve_prod_host import resolve_prod_host, verify_ssh_host

ROOT = Path(__file__).resolve().parents[1]
REMOTE = """
import contextlib, importlib.util, io, json, sys
s = importlib.util.spec_from_file_location('activation',
    '/opt/women-help-chatwoot/deploy/chatwoot/activate.py')
a = importlib.util.module_from_spec(s)
s.loader.exec_module(a)
try:
    request = json.load(sys.stdin)
    if request['apply']:
        with contextlib.redirect_stdout(io.StringIO()) as output:
            a.backup()
        backup = json.loads(output.getvalue())['backup']
    else:
        backup = None
    result = a.rails(request['ruby'], request['config'])
    print(json.dumps({'ok': True, 'backup': backup, **result}))
except Exception as error:
    print(json.dumps({'ok': False, 'error_type': type(error).__name__}))
    sys.exit(1)
"""


def remote_run(history: dict, *, apply: bool = False, probe: bool = False) -> dict:
    host = verify_ssh_host(resolve_prod_host())
    ruby = (ROOT / "deploy/chatwoot/import_legacy_history.rb").read_text()
    if probe:
        ruby += "\n" + (ROOT / "tests/legacy_history_import_probe.rb").read_text()
    else:
        ruby += "\nputs 'ACTIVATION_RESULT=' + LegacyHistoryImporter.new(cfg).run.to_json"
    ruby = ("begin\n" + ruby + "\nrescue => e\n"
            "code = e.message.match?(/\\A[a-z_]+\\z/) ? e.message : 'database_error'\n"
            "puts 'ACTIVATION_RESULT=' + {ok: false, error_code: code}.to_json\nend")
    config = {"account_id": 1, "apply": apply, "history": history}
    request = json.dumps({"apply": apply, "ruby": ruby, "config": config})
    # Only our maintained code is in argv. Personal data travels over encrypted
    # stdin; Rails logs and exception bodies remain captured on the VM.
    result = subprocess.run(
        ["ssh", "-o", "StrictHostKeyChecking=accept-new", "-o", "BatchMode=yes",
         "-o", "ConnectTimeout=10", host, "sudo python3 -c " + shlex.quote(REMOTE)],
        input=request, capture_output=True, text=True, timeout=180, check=False,
    )
    if result.returncode:
        raise RuntimeError("remote_import_failed_output_suppressed")
    report = json.loads(result.stdout)
    if not report.get("ok"):
        raise ImportConflict("remote_" + report.get("error_code", "import_failed"))
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dump", type=Path)
    parser.add_argument("--local-only", action="store_true", help="Inspect counts without network")
    parser.add_argument("--apply", action="store_true", help="Back up and commit the import")
    args = parser.parse_args()
    if args.local_only and args.apply:
        parser.error("--local-only cannot be combined with --apply")
    try:
        history = load_history(args.dump)
        report = {"source": summary(history)}
        if not args.local_only:
            report["destination"] = remote_run(history, apply=args.apply)
        print(json.dumps(report, ensure_ascii=False))
    except Exception as error:  # noqa: BLE001 - never print source content/PII/credentials
        print(json.dumps({"ok": False, "error_type": type(error).__name__,
                          "error_code": str(error) if isinstance(error, ImportConflict) else None}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
