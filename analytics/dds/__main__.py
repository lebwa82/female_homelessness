"""Explicit local/operator commands. No implicit migrations or app configuration access."""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import os
import re
import ssl
import stat
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import asyncpg

from analytics.dds.catalog import load_catalog, validate_catalog
from analytics.dds.pipeline import run_once
from analytics.dds.source import Scope
from analytics.dds.store import migrate, read_state


def validate_dsn(value: str) -> None:
    """Remote connections must authenticate both the CA and the expected hostname."""
    u = urlsplit(value)
    if u.scheme not in ("postgresql", "postgres") or not u.hostname or not u.path.strip("/"):
        raise ValueError("invalid PostgreSQL connection configuration")
    host = u.hostname
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?", host):
            raise ValueError("one canonical PostgreSQL host is required")
    if u.port is not None and not 1 <= u.port <= 65535:
        raise ValueError("invalid PostgreSQL port")
    authority = u.netloc.rsplit("@", 1)[-1]
    canonical_host = "[" + host + "]" if ":" in host else host
    expected = canonical_host + (":" + str(u.port) if u.port is not None else "")
    if authority.lower() != expected.lower() or u.netloc.count("@") > 1:
        raise ValueError("one complete canonical PostgreSQL authority is required")
    query = parse_qs(u.query, keep_blank_values=True)
    if set(query) - {"sslmode", "sslrootcert"} or any(len(v) != 1 for v in query.values()):
        raise ValueError("connection endpoint overrides are not allowed")
    mode = query.get("sslmode", [None])[0]
    if host in ("127.0.0.1", "localhost", "::1"):
        if mode not in (None, "disable", "verify-full"):
            raise ValueError("local connection must use disable or verify-full TLS mode")
    elif mode != "verify-full":
        raise ValueError("remote PostgreSQL requires sslmode=verify-full")


def read_identity_key(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise ValueError("identity key must be a regular private file")
    info = path.stat()
    if stat.S_IMODE(info.st_mode) & 0o077 or info.st_uid != os.getuid():
        raise ValueError("identity key file must be owned by the worker with mode0600")
    key = path.read_bytes()
    if not 32 <= len(key) <= 4096:
        raise ValueError("identity key must contain 32 to4096 bytes")
    return key


def _config(path):
    if not path:
        raise ValueError("--config is required")
    config = json.loads(Path(path).read_text())
    scope = Scope(
        config["account_id"],
        tuple(config["inbox_ids"]),
        tuple(config.get("excluded_contact_ids", [])),
        tuple(config.get("label_titles", [])),
    )
    batch = config.get("batch_size", 500)
    if type(batch) is not int or not 1 <= batch <= 1000:
        raise ValueError("invalid batch size")
    return config, scope


def _dsn(config, kind):
    name = config.get(kind + "_dsn_env", "DDS_" + kind.upper() + "_DSN")
    if not isinstance(name, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", name):
        raise ValueError("invalid environment variable name")
    value = os.environ.get(name)
    if not value:
        raise ValueError("required connection environment variable is missing")
    validate_dsn(value)
    return value


async def _connect(dsn, readonly=False):
    validate_dsn(dsn)
    query = parse_qs(urlsplit(dsn).query)
    tls = False
    if query.get("sslmode") == ["verify-full"]:
        ca = query.get("sslrootcert", [os.environ.get("PGSSLROOTCERT")])[0]
        tls = ssl.create_default_context(cafile=ca)
    return await asyncpg.connect(
        dsn,
        ssl=tls,
        timeout=10,
        server_settings={
            "application_name": "nevidimy_dds",
            "statement_timeout": "30000",
            "default_transaction_read_only": "on" if readonly else "off",
        },
    )


async def _execute(args):
    config, scope = _config(args.config)
    if args.command == "load" and config.get("scope_confirmed") is not True:
        raise ValueError("review account/inbox/test scope and set scope_confirmed=true")
    target = await _connect(_dsn(config, "target"))
    try:
        if args.command == "migrate":
            await migrate(target)
            return {"schema_version": load_catalog()["version"], "migrated": True}
        if args.command == "status":
            state = await read_state(target)
            last = await target.fetchrow("""SELECT captured_at,published_at,mode,issue_count
                FROM ctl.etl_run ORDER BY published_at DESC LIMIT 1""")
            return {
                "initialized": state is not None,
                "watermark": state["watermark"].isoformat() if state else None,
                "last_run": dict(last) if last else None,
            }
        source = await _connect(_dsn(config, "source"), readonly=True)
        try:
            secret_path = Path(config["identity_key_file"]).expanduser()
            if not secret_path.is_absolute():
                secret_path = Path(args.config).resolve().parent / secret_path
            return await run_once(
                source,
                target,
                scope,
                read_identity_key(secret_path),
                full=args.full,
                batch_size=config.get("batch_size", 500),
                source_identity=config.get("source_identity", "chatwoot"),
                overlap_hours=config.get("overlap_hours", 48),
            )
        finally:
            await source.close()
    finally:
        await target.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Metadata-only Chatwoot DDS tools")
    parser.add_argument("--config")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("validate")
    commands.add_parser("status")
    migration = commands.add_parser("migrate")
    migration.add_argument("--apply", action="store_true")
    load = commands.add_parser("load")
    load.add_argument("--apply", action="store_true")
    load.add_argument("--full", action="store_true", help="Full scoped reconciliation")
    args = parser.parse_args(argv)
    if args.command in ("migrate", "load") and not args.apply:
        print(json.dumps({"error": "explicit --apply is required; no connections opened"}))
        return 2
    try:
        if args.command == "validate":
            errors = validate_catalog()
            if errors:
                raise ValueError("catalog validation failed")
            if args.config:
                _config(args.config)
            result = {"valid": True, "tables": len(load_catalog()["tables"]), "requirements": 105}
        else:
            result = asyncio.run(_execute(args))
        print(json.dumps(result, default=str, ensure_ascii=False, sort_keys=True))
        return 0
    except Exception as exc:  # noqa: BLE001 - CLI boundary must redact driver/credential errors.
        # Driver messages can contain DSNs, credentials, SQL or source payloads.
        # Values and tracebacks are deliberately not printed to stdout/stderr.
        print(
            json.dumps(
                {
                    "error_type": type(exc).__name__,
                    "error": "DDS command failed; check private configuration and service access",
                }
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
