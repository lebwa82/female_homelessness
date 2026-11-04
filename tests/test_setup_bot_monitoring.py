"""Deterministic HTTP fixtures: never use credentials or the live Better Stack API."""

import copy
import io
import json
import os
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

import pytest

from scripts import setup_bot_monitoring as setup

TEAM = "Project team"
SECRET = "sensitive-api-key"


def row(identifier, kind, **attributes):
    return {"id": str(identifier), "type": kind, "attributes": attributes}


class FakeHTTP:
    def __init__(self):
        self.calls = []
        self.groups = []
        self.heartbeats = []
        self.members = [row(10, "team_member", email=setup.OWNER, role="admin")]
        self.calendars = [row(20, "on_call_calendar", team_name=TEAM, default_calendar=True)]
        self.calendars[0]["relationships"] = {
            "on_call_users": {
                "data": [
                    {
                        "id": "10",
                        "type": "user",
                        "meta": {"email": setup.OWNER, "on_holiday": False},
                    }
                ]
            }
        }
        self.policy = row(50, "policy", team_name=TEAM, repeat_count=0)
        self.policy["steps"] = [
            {
                "type": "escalation",
                "wait_before": 0,
                "urgency_id": 60,
                "step_members": [{"type": "user", "email": setup.OWNER}],
            }
        ]
        self.severities = [row(60, "urgency", team_name=TEAM, **setup.CHANNELS)]
        self.next_url = None

    def open(self, request, timeout):
        assert request.get_header("Authorization") == "Bearer " + SECRET
        assert parse_qs(urlsplit(request.full_url).query)["team_name"] == [TEAM]
        path = urlsplit(request.full_url).path
        body = json.loads(request.data) if request.data else None
        self.calls.append((request.get_method(), path, body))
        if request.get_method() == "GET":
            collection = {
                "/api/v2/team-members": self.members,
                "/api/v2/on-calls": self.calendars,
                "/api/v2/heartbeat-groups": self.groups,
                "/api/v2/heartbeats": self.heartbeats,
                "/api/v2/urgencies": self.severities,
            }
            if path == "/api/v3/policies/50":
                result = {"data": self.policy}
            else:
                result = {"data": collection[path], "pagination": {"next": self.next_url}}
        elif request.get_method() == "POST" and path == "/api/v2/heartbeat-groups":
            result = {"data": row(100, "heartbeat_group", **body)}
            self.groups.append(result["data"])
        elif request.get_method() == "POST" and path == "/api/v2/heartbeats":
            identifier = len(self.heartbeats) + 200
            result = {
                "data": row(
                    identifier,
                    "heartbeat",
                    **body,
                    url=f"https://incidents.betterstack.com/api/v1/heartbeat/secret{identifier}",
                )
            }
            self.heartbeats.append(result["data"])
        elif request.get_method() == "PATCH" and path.startswith("/api/v2/heartbeats/"):
            target = next(h for h in self.heartbeats if h["id"] == path.rsplit("/", 1)[1])
            target["attributes"].update(body)
            result = {"data": target}
        else:
            pytest.fail(f"Unexpected endpoint or mutation: {request.get_method()} {path}")
        return io.StringIO(json.dumps(result))

    def client(self):
        return setup.Client(SECRET, TEAM, opener=self)

    def existing(self, count=5):
        self.groups = [row(100, "heartbeat_group", name=setup.GROUP, team_name=TEAM)]
        for index, name in enumerate(list(setup.CHECKS.values())[:count]):
            self.heartbeats.append(
                row(
                    index + 200,
                    "heartbeat",
                    name=name,
                    team_name=TEAM,
                    heartbeat_group_id=100,
                    period=60,
                    grace=60,
                    policy_id=None,
                    team_wait=None,
                    paused=False,
                    **setup.CHANNELS,
                    url=f"https://incidents.betterstack.com/api/v1/heartbeat/secret{index}",
                )
            )


def test_default_plan_has_no_writes_or_secret_output(tmp_path):
    api = FakeHTTP()
    result = setup.reconcile(api.client(), output=tmp_path / "monitor.env")
    assert result["mode"] == "plan"
    assert set(result["checks"].values()) == {"create"}
    assert all(method == "GET" for method, _, _ in api.calls)
    assert not list(tmp_path.iterdir())
    assert SECRET not in json.dumps(result)
    assert "secret200" not in json.dumps(result)


def test_apply_is_idempotent_and_persists_secure_mapping(tmp_path):
    api, output = FakeHTTP(), tmp_path / "monitor.env"
    setup.reconcile(api.client(), apply=True, output=output)
    assert output.stat().st_mode & 0o777 == 0o600
    entries = setup.load_output(output)
    assert set(entries["BOT_MONITOR_HEARTBEAT_URLS_JSON"]) == set(setup.CHECKS)
    assert entries["BOT_MONITOR_TELEGRAM_CHAT_ID"] == "-1004392684857"
    assert entries["BOT_MONITOR_DASHBOARD_URL"] == setup.DASHBOARD
    first_writes = [c for c in api.calls if c[0] != "GET"]
    assert len(first_writes) == 6
    api.calls.clear()
    result = setup.reconcile(api.client(), apply=True, output=output)
    assert set(result["checks"].values()) == {"keep"}
    assert all(method == "GET" for method, _, _ in api.calls)
    assert len(api.heartbeats) == 5
    for _, path, body in first_writes[1:]:
        assert path == "/api/v2/heartbeats"
        assert body["period"] == body["grace"] == 60
        assert all(body[key] is value for key, value in setup.CHANNELS.items())
        assert body["metadata"]["Owner"] == setup.OWNER


def test_partial_group_only_creates_missing_and_updates_owned(tmp_path):
    api = FakeHTTP()
    api.existing(2)
    api.heartbeats[0]["attributes"]["period"] = 300
    result = setup.reconcile(api.client(), apply=True, output=tmp_path / "monitor.env")
    assert list(result["checks"].values()).count("create") == 3
    assert list(result["checks"].values()).count("update") == 1
    assert len(api.groups) == 1
    assert [c[1] for c in api.calls if c[0] == "PATCH"] == ["/api/v2/heartbeats/200"]


@pytest.mark.parametrize(
    "problem,code",
    [
        ("duplicate_group", "ambiguous_group_name"),
        ("duplicate_heartbeat", "ambiguous_heartbeat_name"),
        ("outside_group", "heartbeat_outside_owned_group"),
    ],
)
def test_ambiguous_names_stop_before_writes(tmp_path, problem, code):
    api = FakeHTTP()
    api.existing()
    if problem == "duplicate_group":
        api.groups.append(copy.deepcopy(api.groups[0]) | {"id": "101"})
    elif problem == "duplicate_heartbeat":
        api.heartbeats.append(copy.deepcopy(api.heartbeats[0]) | {"id": "999"})
    else:
        api.heartbeats[0]["attributes"]["heartbeat_group_id"] = 999
    with pytest.raises(setup.SetupError, match=code):
        setup.reconcile(api.client(), apply=True, output=tmp_path / "monitor.env")
    assert all(c[0] == "GET" for c in api.calls)


@pytest.mark.parametrize(
    "problem", ["invitation", "different_email", "billing", "holiday", "other_member", "no_primary"]
)
def test_unverified_owner_routing_blocks_apply_but_plan_is_useful(tmp_path, problem):
    api = FakeHTTP()
    if problem == "invitation":
        api.members[0]["type"] = "team_member_invitation"
    elif problem == "different_email":
        api.members[0]["attributes"]["email"] = "someone@example.com"
    elif problem == "billing":
        api.members[0]["attributes"]["role"] = "billing_admin"
    elif problem == "holiday":
        api.calendars[0]["relationships"]["on_call_users"]["data"][0]["meta"]["on_holiday"] = True
    elif problem == "other_member":
        api.members.append(row(11, "team_member", email="other@example.com", role="responder"))
    else:
        api.calendars.clear()
    plan = setup.reconcile(api.client())
    assert plan["routing"] == "unverified" and plan["apply_blocked"]
    with pytest.raises(setup.SetupError):
        setup.reconcile(api.client(), apply=True, output=tmp_path / "monitor.env")
    assert all(c[0] == "GET" for c in api.calls)


def test_existing_policy_is_read_only_and_requires_email_only(tmp_path):
    api = FakeHTTP()
    setup.reconcile(api.client(), apply=True, output=tmp_path / "monitor.env", policy_id="50")
    assert all(h["attributes"]["policy_id"] == "50" for h in api.heartbeats)
    assert all(
        path.startswith("/api/v2/heartbeat") for method, path, _ in api.calls if method != "GET"
    )
    api.severities[0]["attributes"]["sms"] = True
    with pytest.raises(setup.SetupError, match="policy_email_channels_unverified"):
        setup.reconcile(api.client(), apply=True, output=tmp_path / "monitor.env", policy_id="50")


def test_refuse_unrelated_existing_output_before_remote_writes(tmp_path):
    api, output = FakeHTTP(), tmp_path / "monitor.env"
    output.write_text("UNRELATED=secret\n")
    output.chmod(0o600)
    with pytest.raises(setup.SetupError, match="output_file_unrecognized"):
        setup.reconcile(api.client(), apply=True, output=output)
    assert api.calls == []
    assert output.read_text() == "UNRELATED=secret\n"


def test_output_mapping_must_match_resource_ids(tmp_path):
    api, output = FakeHTTP(), tmp_path / "monitor.env"
    setup.reconcile(api.client(), apply=True, output=output)
    api.heartbeats[0]["id"] = "9999"
    api.calls.clear()
    with pytest.raises(setup.SetupError, match="output_resource_ids_mismatch"):
        setup.reconcile(api.client(), apply=True, output=output)
    assert all(c[0] == "GET" for c in api.calls)


@pytest.mark.parametrize("kind", ["mode", "symlink", "hardlink"])
def test_private_files_required(tmp_path, kind):
    path = tmp_path / "token"
    path.write_text(SECRET)
    path.chmod(0o600)
    if kind == "mode":
        path.chmod(0o644)
    elif kind == "symlink":
        alias = tmp_path / "alias"
        alias.symlink_to(path)
        path = alias
    else:
        os.link(path, tmp_path / "hardlink")
    with pytest.raises(setup.SetupError):
        setup.private_read(path)


def test_http_errors_never_expose_upstream_body_or_token(monkeypatch, capsys):
    class FailedHTTP:
        def open(self, request, timeout):
            raise HTTPError(request.full_url, 403, SECRET, {}, io.BytesIO(SECRET.encode()))

    monkeypatch.setenv("BETTER_STACK_API_TOKEN", SECRET)
    monkeypatch.setattr(setup, "build_opener", lambda *args: FailedHTTP())
    assert setup.main(["--team-name", TEAM]) == 1
    output = capsys.readouterr()
    assert SECRET not in output.out + output.err
    assert '"status": 403' in output.err


def test_suspicious_pagination_is_not_followed():
    api = FakeHTTP()
    api.next_url = "https://attacker.example/steal?page=2"
    with pytest.raises(setup.SetupError, match="unsafe_pagination"):
        api.client().listing(setup.MEMBERS)
    assert len(api.calls) == 1


def test_client_has_no_billing_statuspage_invite_delete_mutations():
    api = FakeHTTP()
    client = api.client()
    for method, endpoint in [
        ("POST", "/status-pages"),
        ("DELETE", "/heartbeats/10"),
        ("POST", "/team-members"),
        ("POST", "/urgencies"),
        ("POST", "/billing"),
        ("PATCH", "/heartbeat-groups/100"),
    ]:
        with pytest.raises(setup.SetupError, match="unsupported_mutation"):
            client.request(method, setup.API + endpoint, {})
    assert api.calls == []


def test_cli_argument_errors_do_not_repeat_possible_secrets(capsys):
    with pytest.raises(SystemExit):
        setup.main(["--team-name", TEAM, "--token", SECRET])
    assert SECRET not in capsys.readouterr().err


def test_paused_owned_heartbeat_is_resumed(tmp_path):
    api = FakeHTTP()
    api.existing()
    api.heartbeats[0]["attributes"]["paused"] = True
    result = setup.reconcile(api.client(), apply=True, output=tmp_path / "monitor.env")
    assert list(result["checks"].values()).count("update") == 1
    assert api.heartbeats[0]["attributes"]["paused"] is False


def test_partial_failed_creation_can_be_reconciled_without_duplicates(tmp_path):
    api = FakeHTTP()
    original = api.open

    def interrupted(request, timeout):
        result = original(request, timeout)
        if request.get_method() == "POST" and len(api.heartbeats) == 2:
            raise TimeoutError(SECRET)
        return result

    api.open = interrupted
    output = tmp_path / "monitor.env"
    with pytest.raises(setup.SetupError, match="api_transport_error"):
        setup.reconcile(api.client(), apply=True, output=output)
    assert not output.exists()
    api.open = original
    setup.reconcile(api.client(), apply=True, output=output)
    assert len(api.heartbeats) == 5 and len(api.groups) == 1
