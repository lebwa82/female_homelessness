"""Safely reconcile the five Better Stack heartbeats; default mode only reads.

Official API references (checked 2026-11-04):
https://betterstack.com/docs/uptime/api/create-a-hearbeat/
https://betterstack.com/docs/uptime/api/create-a-new-heartbeat-group/
https://betterstack.com/docs/uptime/api/team-members/list/
https://betterstack.com/docs/uptime/api/list-all-existing-on-call-calendars/
https://betterstack.com/docs/uptime/api/get-a-single-escalation-policy/
https://betterstack.com/docs/uptime/api/list-all-severities/

``email`` means notify the on-call responder, not an arbitrary e-mail address.
We verify membership and routing before writes, never invite users or change billing,
calendars, policies, severities or status pages. Personal notification overrides and
mail delivery cannot be verified through these APIs; run a controlled delivery drill.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
import tempfile
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

API = "https://incidents.betterstack.com/api/v2"
MEMBERS = "https://betterstack.com/api/v2/team-members"
POLICIES = "https://incidents.betterstack.com/api/v3/policies"
DASHBOARD = "https://uptime.betterstack.com"
OWNER = "igor.cherentsov@gmail.com"
GROUP = "Невидимый фонд"
CHECKS = {
    "telegram_poll": "Телеграм: получение сообщений",
    "telegram_proxy": "Телеграм: прокси",
    "agent": "Агент поддержки",
    "queues": "Очереди сообщений",
    "host": "Сервер",
}
CHANNELS = {"email": True, "call": False, "sms": False, "push": False, "critical_alert": False}
ENV_KEYS = {
    "BOT_MONITOR_HEARTBEAT_URLS_JSON",
    "BETTER_STACK_HEARTBEAT_IDS_JSON",
    "BETTER_STACK_GROUP_ID",
    "BOT_MONITOR_DASHBOARD_URL",
    "BOT_MONITOR_OWNER_EMAIL",
    "BOT_MONITOR_TELEGRAM_CHAT_ID",
}


class SetupError(Exception):
    """Only a fixed error code may be displayed; never upstream response text."""

    def __init__(self, code: str, status: int | None = None):
        self.code, self.status = code, status
        super().__init__(code)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def resource_id(value) -> str:
    value = str(value)
    if not re.fullmatch(r"[0-9]+", value):
        raise SetupError("invalid_resource_id")
    return value


class Client:
    def __init__(self, token: str, team: str, opener=None):
        self._token = token
        self.team = team
        self._opener = opener or build_opener(NoRedirect())

    def request(self, method: str, url: str, body: dict | None = None) -> dict:
        # Defense in depth: no credential-bearing redirect, arbitrary URL or write endpoint.
        parsed = urlsplit(url)
        allowed = url.startswith((API + "/", POLICIES + "/")) or url == MEMBERS
        if not allowed or parsed.query or parsed.fragment or parsed.username:
            raise SetupError("unsafe_api_url")
        path = parsed.path
        if method != "GET" and not (
            method == "POST"
            and path in {"/api/v2/heartbeats", "/api/v2/heartbeat-groups"}
            or method == "PATCH"
            and re.fullmatch(r"/api/v2/heartbeats/[0-9]+", path)
        ):
            raise SetupError("unsupported_mutation")
        url += "?" + urlencode({"team_name": self.team})
        data = None if body is None else json.dumps(body).encode()
        req = Request(
            url,
            data=data,
            method=method,
            headers={"Authorization": "Bearer " + self._token, "Content-Type": "application/json"},
        )
        try:
            with self._opener.open(req, timeout=20) as response:
                return json.load(response)
        except HTTPError as error:
            raise SetupError("api_http_error", error.code) from None
        except URLError, TimeoutError, OSError:
            raise SetupError("api_transport_error") from None
        except ValueError, TypeError:
            raise SetupError("api_response_invalid") from None

    def listing(self, url: str) -> list[dict]:
        # Fetch pages ourselves; never follow a response URL with our Authorization header.
        result, seen = [], set()
        page = 1
        while page <= 100:
            # Pagination's only dynamic input is a bounded integer.
            original_team = self.team
            endpoint = url + "?" + urlencode({"team_name": original_team, "page": page})
            req = Request(endpoint, headers={"Authorization": "Bearer " + self._token})
            try:
                with self._opener.open(req, timeout=20) as response:
                    payload = json.load(response)
            except HTTPError as error:
                raise SetupError("api_http_error", error.code) from None
            except URLError, TimeoutError, OSError:
                raise SetupError("api_transport_error") from None
            except ValueError, TypeError:
                raise SetupError("api_response_invalid") from None
            rows = payload.get("data")
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise SetupError("api_response_invalid")
            for row in rows:
                key = (row.get("type"), resource_id(row.get("id")))
                if key in seen:
                    raise SetupError("duplicate_resource_id")
                seen.add(key)
            result.extend(rows)
            next_url = payload.get("pagination", {}).get("next")
            if not next_url:
                return result
            parsed, expected = urlsplit(next_url), urlsplit(url)
            query = parse_qs(parsed.query)
            if (parsed.scheme, parsed.netloc, parsed.path) != (
                expected.scheme,
                expected.netloc,
                expected.path,
            ) or query.get("page") != [str(page + 1)]:
                raise SetupError("unsafe_pagination")
            page += 1
        raise SetupError("pagination_limit")


def configured_value(attributes: dict, key: str):
    value = attributes.get(key)
    if key == "policy_id" and value is not None:
        return resource_id(value)
    return value


def attrs(resource: dict) -> dict:
    result = resource.get("attributes")
    if not isinstance(result, dict):
        raise SetupError("api_response_invalid")
    return result


def verify_routing(client: Client, policy_id: str | None) -> str:
    members = client.listing(MEMBERS)
    owners = [m for m in members if attrs(m).get("email", "").casefold() == OWNER]
    if len(owners) != 1 or owners[0].get("type") != "team_member":
        raise SetupError("owner_membership_unverified")
    owner = owners[0]
    if attrs(owner).get("role") not in {"admin", "team_lead", "responder"}:
        raise SetupError("owner_alert_capability_unverified")
    if policy_id:
        policy = client.request("GET", POLICIES + "/" + resource_id(policy_id))["data"]
        if attrs(policy).get("team_name") != client.team:
            raise SetupError("routing_team_mismatch")
        steps = policy.get("steps")
        if not isinstance(steps, list) or len(steps) != 1:
            raise SetupError("policy_routing_unverified")
        step = steps[0]
        recipients = step.get("step_members", [])
        if (
            step.get("type") != "escalation"
            or step.get("wait_before") != 0
            or len(recipients) != 1
            or recipients[0].get("type") != "user"
            or attrs(policy).get("repeat_count") != 0
            or attrs(policy).get("fallback_policy_id") is not None
        ):
            raise SetupError("policy_routing_unverified")
        target = recipients[0]
        if ("id" in target and str(target["id"]) != str(owner["id"])) or (
            "email" in target and target["email"].casefold() != OWNER
        ):
            raise SetupError("policy_recipient_mismatch")
        if not (
            (target.get("email", "").casefold() == OWNER)
            or str(target.get("id")) == str(owner["id"])
        ):
            raise SetupError("policy_recipient_mismatch")
        severities = client.listing(API + "/urgencies")
        severity = [s for s in severities if str(s["id"]) == str(step.get("urgency_id"))]
        if (
            len(severity) != 1
            or attrs(severity[0]).get("team_name") != client.team
            or any(attrs(severity[0]).get(k) is not v for k, v in CHANNELS.items())
        ):
            raise SetupError("policy_email_channels_unverified")
        return "existing_policy_email_only"
    eligible = [
        m
        for m in members
        if m.get("type") == "team_member" and attrs(m).get("role") != "billing_admin"
    ]
    if len(eligible) != 1 or eligible[0]["id"] != owner["id"]:
        raise SetupError("default_routing_requires_sole_owner")
    calendars = client.listing(API + "/on-calls")
    defaults = [c for c in calendars if attrs(c).get("default_calendar") is True]
    if len(defaults) != 1 or attrs(defaults[0]).get("team_name") != client.team:
        raise SetupError("primary_responder_unverified")
    users = defaults[0].get("relationships", {}).get("on_call_users", {}).get("data", [])
    if (
        len(users) != 1
        or users[0].get("meta", {}).get("email", "").casefold() != OWNER
        or users[0].get("meta", {}).get("on_holiday") is not False
    ):
        raise SetupError("primary_responder_unverified")
    return "sole_owner_primary_responder"


def private_read(path: Path) -> str:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "r") as source:
            info = os.fstat(source.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_uid != os.getuid()
                or info.st_nlink != 1
            ):
                raise SetupError("private_file_required")
            return source.read()
    except OSError:
        raise SetupError("private_file_unavailable") from None


def load_output(path: Path | None) -> dict | None:
    if path is None or not os.path.lexists(path):
        return None
    try:
        entries = dict(line.split("=", 1) for line in private_read(path).splitlines())
        if set(entries) != ENV_KEYS or entries["BOT_MONITOR_OWNER_EMAIL"] != OWNER:
            raise SetupError("output_file_unrecognized")
        # This is a strict data file, never a shell script or evaluated dotenv.
        for key in ("BOT_MONITOR_HEARTBEAT_URLS_JSON", "BETTER_STACK_HEARTBEAT_IDS_JSON"):
            entries[key] = json.loads(entries[key])
        if (
            entries["BOT_MONITOR_DASHBOARD_URL"] != DASHBOARD
            or set(entries["BOT_MONITOR_HEARTBEAT_URLS_JSON"]) != set(CHECKS)
            or set(entries["BETTER_STACK_HEARTBEAT_IDS_JSON"]) != set(CHECKS)
        ):
            raise SetupError("output_file_unrecognized")
        return entries
    except ValueError, TypeError:
        raise SetupError("output_file_unrecognized") from None


def heartbeat_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.netloc not in {"incidents.betterstack.com", "uptime.betterstack.com"}
        or not re.fullmatch(r"/api/v1/heartbeat/[a-zA-Z0-9_-]+", parsed.path)
        or parsed.query
        or parsed.fragment
    ):
        raise SetupError("heartbeat_url_invalid")
    return value


def save_output(path: Path, group_id: str, resources: dict, previous: dict | None) -> None:
    ids = {check: resource_id(row["id"]) for check, row in resources.items()}
    urls = {check: heartbeat_url(attrs(row)["url"]) for check, row in resources.items()}
    lines = [
        "BOT_MONITOR_TELEGRAM_CHAT_ID=-1004392684857",
        f"BOT_MONITOR_OWNER_EMAIL={OWNER}",
        f"BOT_MONITOR_DASHBOARD_URL={DASHBOARD}",
        f"BETTER_STACK_GROUP_ID={resource_id(group_id)}",
        "BETTER_STACK_HEARTBEAT_IDS_JSON=" + json.dumps(ids, sort_keys=True),
        "BOT_MONITOR_HEARTBEAT_URLS_JSON=" + json.dumps(urls, sort_keys=True),
    ]
    # Recheck before atomic replace; refuse unrelated or concurrently changed files.
    if load_output(path) != previous:
        raise SetupError("output_file_changed")
    fd, temporary = tempfile.mkstemp(prefix=".bot-monitor-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as target:
            os.fchmod(target.fileno(), 0o600)
            target.write("\n".join(lines) + "\n")
            target.flush()
            os.fsync(target.fileno())
        if previous is None:
            os.link(temporary, path)  # Atomic no-clobber creation.
        else:
            os.replace(temporary, path)
    except OSError:
        raise SetupError("output_write_failed") from None
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def reconcile(
    client: Client, *, apply: bool = False, output: Path | None = None, policy_id: str | None = None
) -> dict:
    previous = load_output(output)
    if apply and output is None:
        raise SetupError("output_env_required")
    routing_error = None
    try:
        routing = verify_routing(client, policy_id)
    except SetupError as error:
        if error.code.startswith("api_"):
            raise
        routing, routing_error = "unverified", error.code
    groups = client.listing(API + "/heartbeat-groups")
    matching = [g for g in groups if attrs(g).get("name") == GROUP]
    if len(matching) > 1:
        raise SetupError("ambiguous_group_name")
    group = matching[0] if matching else None
    if group and attrs(group).get("team_name") != client.team:
        raise SetupError("group_team_mismatch")
    group_id = resource_id(group["id"]) if group else None
    all_rows = client.listing(API + "/heartbeats")
    resources = {}
    for check, name in CHECKS.items():
        rows = [r for r in all_rows if attrs(r).get("name") == name]
        if len(rows) > 1:
            raise SetupError("ambiguous_heartbeat_name")
        if rows:
            row = rows[0]
            if (
                group_id is None
                or str(attrs(row).get("heartbeat_group_id")) != group_id
                or attrs(row).get("team_name") != client.team
            ):
                raise SetupError("heartbeat_outside_owned_group")
            resources[check] = row
    if previous and (
        previous["BETTER_STACK_GROUP_ID"] != group_id
        or any(
            check not in resources or str(resources[check]["id"]) != identifier
            for check, identifier in previous["BETTER_STACK_HEARTBEAT_IDS_JSON"].items()
        )
    ):
        raise SetupError("output_resource_ids_mismatch")
    desired_base = {
        "period": 60,
        "grace": 60,
        **CHANNELS,
        "team_wait": None,
        "paused": False,
        "policy_id": resource_id(policy_id) if policy_id else None,
    }
    actions = {}
    for check in CHECKS:
        existing = resources.get(check)
        actions[check] = (
            "create"
            if existing is None
            else "update"
            if any(
                configured_value(attrs(existing), k) != value for k, value in desired_base.items()
            )
            else "keep"
        )
    result = {
        "mode": "apply" if apply else "plan",
        "group": GROUP,
        "group_action": "keep" if group else "create",
        "checks": actions,
        "owner_email": OWNER,
        "routing": routing,
        "dashboard_url": DASHBOARD,
        "delivery_verified": False,
    }
    if routing_error:
        result["apply_blocked"] = routing_error
    if not apply:
        return result
    if routing_error:
        raise SetupError(routing_error)
    if group is None:
        group = client.request(
            "POST", API + "/heartbeat-groups", {"name": GROUP, "team_name": client.team}
        )["data"]
        group_id = resource_id(group["id"])
        if attrs(group).get("name") != GROUP or attrs(group).get("team_name") != client.team:
            raise SetupError("group_readback_mismatch")
    for check, name in CHECKS.items():
        desired = {**desired_base, "heartbeat_group_id": int(group_id), "name": name}
        if actions[check] == "create":
            client.request(
                "POST",
                API + "/heartbeats",
                {
                    **desired,
                    "team_name": client.team,
                    "metadata": {"Owner": OWNER, "Check": check},
                },
            )
        elif actions[check] == "update":
            identifier = resource_id(resources[check]["id"])
            client.request("PATCH", API + "/heartbeats/" + identifier, desired)
    # Fresh listing verifies uncertain writes before exposing URLs or replacing a mapping.
    rows = client.listing(API + "/heartbeats")
    verified = {}
    for check, name in CHECKS.items():
        matching = [r for r in rows if attrs(r).get("name") == name]
        if len(matching) != 1:
            raise SetupError("heartbeat_readback_mismatch")
        row = matching[0]
        if (
            attrs(row).get("team_name") != client.team
            or str(attrs(row).get("heartbeat_group_id")) != group_id
            or any(configured_value(attrs(row), k) != value for k, value in desired_base.items())
            or (check in resources and row["id"] != resources[check]["id"])
        ):
            raise SetupError("heartbeat_readback_mismatch")
        verified[check] = row
    save_output(output, group_id, verified, previous)
    return result


def main(argv: list[str] | None = None) -> int:
    class SafeParser(argparse.ArgumentParser):
        def error(self, message):
            self.exit(2, "Invalid CLI arguments; use --help.\n")

    parser = SafeParser(description=__doc__.split("\n")[0], allow_abbrev=False)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--plan", action="store_true", help="read-only (default)")
    modes.add_argument("--apply", action="store_true", help="explicitly create/update heartbeats")
    parser.add_argument("--team-name", required=True, help="exact existing Better Stack team")
    parser.add_argument("--token-file", type=Path, help="owner-only 0600 file; otherwise env token")
    parser.add_argument("--output-env", type=Path, help="private mapping file, required for apply")
    parser.add_argument("--policy-id", help="optional existing single-recipient email-only policy")
    args = parser.parse_args(argv)
    try:
        token = (
            private_read(args.token_file).strip()
            if args.token_file
            else os.environ.get("BETTER_STACK_API_TOKEN", "").strip()
        )
        if not token or any(c in token for c in "\r\n"):
            raise SetupError("token_required")
        if args.policy_id:
            resource_id(args.policy_id)
        result = reconcile(
            Client(token, args.team_name),
            apply=args.apply,
            output=args.output_env,
            policy_id=args.policy_id,
        )
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as error:  # noqa: BLE001 - suppress credential-bearing exception text
        safe = {"error_type": type(error).__name__}
        if isinstance(error, SetupError):
            safe["error_code"] = error.code
            if error.status is not None:
                safe["status"] = error.status
        print(json.dumps(safe), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
