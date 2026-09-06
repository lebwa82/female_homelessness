"""Root-only, idempotent activation on the project VM. Never prints credentials.

Uses Chatwoot's Rails models for administrator bootstrap; runtime uses its API.
Run phases in order: backup, prepare, provision, connect, status.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

CW = Path("/etc/women-help-chatwoot.env")
AGENT = Path("/etc/women-help-agent.env")
BOT = Path("/etc/women-help-bot.env")


def env(path):
    result = {}
    for line in path.read_text().splitlines():
        if line.strip() and not line.lstrip().startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            result[k.strip()] = v.strip().strip('"').strip("'")
    return result


def save(path, values):
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    with os.fdopen(fd, "w") as out:
        for key, value in values.items():
            if "\n" in value or "\r" in value:
                raise ValueError("Invalid environment value")
            out.write(f"{key}={value}\n")
    os.replace(temp, path)


def command(args, **kwargs):
    result = subprocess.run(
        args, capture_output=True, text=True, timeout=120, check=False, **kwargs
    )
    if result.returncode:
        raise RuntimeError(f"Operation failed: {args[0]} exit={result.returncode}")
    return result.stdout


def container(service):
    ids = command(
        [
            "podman",
            "ps",
            "--filter",
            f"label=com.docker.compose.service={service}",
            "--filter",
            "label=io.podman.compose.project=women-help-chatwoot",
            "--format",
            "{{.ID}}",
        ]
    ).split()
    if len(ids) != 1:
        raise RuntimeError(f"Expected one {service} container")
    return ids[0]


def rails(script, config=None):
    wrapper = (
        'require "json"; cfg = JSON.parse(STDIN.read); begin; '
        + script
        + '; rescue => e; puts "ACTIVATION_ERROR=" + e.class.name; exit 1; end'
    )
    result = subprocess.run(
        [
            "podman",
            "exec",
            "-i",
            container("chatwoot"),
            "bundle",
            "exec",
            "rails",
            "runner",
            wrapper,
        ],
        input=json.dumps(config or {}),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    for line in result.stdout.splitlines():
        if line.startswith("ACTIVATION_ERROR="):
            raise RuntimeError(line)
        if line.startswith("ACTIVATION_RESULT="):
            return json.loads(line.split("=", 1)[1])
    raise RuntimeError(f"Rails activation failed: exit={result.returncode}")


def backup():
    directory = Path(tempfile.mkdtemp(prefix="chatwoot-connect.", dir="/opt/women-help-backups"))
    for path in [CW, AGENT, BOT]:
        shutil.copy2(path, directory / path.name)
    for service, database, username in [("postgres", "chatwoot", "chatwoot")]:
        with (directory / f"{database}.dump").open("wb") as out:
            result = subprocess.run(
                ["podman", "exec", container(service), "pg_dump", "-Fc", "-U", username, database],
                stdout=out,
                stderr=subprocess.PIPE,
                timeout=120,
                check=False,
            )
            if result.returncode:
                raise RuntimeError("Backup failed")
    (directory / "previous-release.txt").write_text(str(Path("/opt/women-help-chatwoot").resolve()))
    print(json.dumps({"backup": str(directory)}))


def prepare():
    old, agent, cw = env(BOT), env(AGENT), env(CW)
    for key in ["YANDEX_AI_API_KEY", "TELEGRAM_PROXY_URL", "LLM_ENABLED"]:
        if key in old:
            agent[key] = old[key]
    agent.update(
        CHATWOOT_BASE_URL="http://chatwoot:3000",
        CHATWOOT_ACCOUNT_ID="1",
        APP_ENV="production",
        CHATWOOT_WEBHOOK_SECRET=cw["CHATWOOT_WEBHOOK_SECRET"],
    )
    save(AGENT, agent)
    cw["ENABLE_ACCOUNT_SIGNUP"] = "false"
    save(CW, cw)
    print(json.dumps({"runtime_prepared": True}))


def provision():
    cw, agent = env(CW), env(AGENT)
    data = rails(
        """
      account = Account.find(1)
      admin = account.account_users.where(role: :administrator).first!.user
      team = account.teams.find_or_create_by!(name: "Дежурные") { |t| t.allow_auto_assign = false }
      team.team_members.find_or_create_by!(user: admin)
      bot = account.agent_bots.find_or_initialize_by(name: "Women Help Agent")
      bot.outgoing_url = cfg.fetch("url"); bot.save!
      definition = account.custom_attribute_definitions.find_or_initialize_by(attribute_key: "reply_owner", attribute_model: 0)
      definition.assign_attributes(attribute_display_name: "Кто отвечает", attribute_display_type: 6,
        attribute_values: ["bot", "human"], attribute_description: "bot — отвечает робот; human — отвечает дежурный")
      definition.save!
      puts "ACTIVATION_RESULT=" + {bot_id: bot.id, team_id: team.id,
        read_token: admin.access_token.token, bot_token: bot.access_token.token, signature: bot.secret}.to_json
    """,
        {
            "url": f"https://{cw['AGENT_HOSTNAME']}/webhooks/chatwoot/agent/{cw['CHATWOOT_WEBHOOK_SECRET']}"
        },
    )
    agent.update(
        CHATWOOT_READ_TOKEN=data["read_token"],
        CHATWOOT_BOT_TOKEN=data["bot_token"],
        CHATWOOT_DUTY_TEAM_ID=str(data["team_id"]),
        CHATWOOT_WEBHOOK_HMAC_SECRET=data["signature"],
    )
    save(AGENT, agent)
    cw["CHATWOOT_WEBHOOK_HMAC_SECRET"] = data["signature"]
    save(CW, cw)
    print(json.dumps({"bot_id": data["bot_id"], "team_id": data["team_id"]}))


def connect():
    result = rails(
        """
      account = Account.find(1)
      bot = account.agent_bots.find_by!(name: "Women Help Agent")
      admin = account.account_users.where(role: :administrator).first!.user
      ActiveRecord::Base.transaction do
        channel = account.telegram_channels.find_or_create_by!(bot_token: cfg.fetch("token"))
        inbox = channel.inbox || account.inboxes.create!(name: "Telegram — Невидимый фонд", channel: channel,
          enable_auto_assignment: false, lock_to_single_conversation: true)
        inbox.inbox_members.find_or_create_by!(user: admin)
        binding = inbox.agent_bot_inbox || inbox.build_agent_bot_inbox
        binding.update!(agent_bot: bot, status: :active)
        puts "ACTIVATION_RESULT=" + {inbox_id: inbox.id, bot_name: channel.bot_name}.to_json
      end
    """,
        {"token": env(BOT)["TELEGRAM_BOT_TOKEN"]},
    )
    print(json.dumps(result))


def status():
    print(
        json.dumps(
            rails("""
      puts "ACTIVATION_RESULT=" + {inboxes: Inbox.count, telegram_channels: Channel::Telegram.count,
        agent_bots: AgentBot.count, bindings: AgentBotInbox.active.count,
        conversations: Conversation.count, messages: Message.count}.to_json
    """)
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=["backup", "prepare", "provision", "connect", "status"])
    args = parser.parse_args()
    try:
        globals()[args.phase]()
    except Exception as error:  # noqa: BLE001 - only safe exception metadata may leave the VM
        print(json.dumps({"error_type": type(error).__name__}))
        raise SystemExit(1) from None
