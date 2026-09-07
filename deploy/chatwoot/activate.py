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
INGRESS = Path("/etc/women-help-telegram.env")


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
    for path in [CW, AGENT, BOT, INGRESS]:
        if path.exists():
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
    handoff_schema()


def handoff_schema():
    """Register operator-visible flags without changing assignments or credentials."""
    result = rails("""
      account = Account.find(1)
      definition = account.custom_attribute_definitions.find_or_initialize_by(
        attribute_key: "reply_owner", attribute_model: :conversation_attribute)
      definition.assign_attributes(attribute_display_name: "Кто отвечает", attribute_display_type: :list,
        attribute_values: ["bot", "human"],
        attribute_description: "После подключения сотрудницы сохраняется human. Возврат — макрос «Вернуть боту», не снятие назначения.")
      definition.save!
      request = account.custom_attribute_definitions.find_or_initialize_by(
        attribute_key: "handoff_requested", attribute_model: :conversation_attribute)
      request.assign_attributes(attribute_display_name: "Запрошен дежурный", attribute_display_type: :checkbox,
        attribute_description: "Запрос отмечен в приватной заметке с уведомлением команды. Бот отвечает до назначения сотрудницы.")
      request.save!
      puts "ACTIVATION_RESULT=" + {handoff_schema_ready: true}.to_json
    """)
    print(json.dumps(result))


def queues():
    """Initial queues and native navigation, without a parallel staff database."""
    agent = env(AGENT)
    result = rails("""
      account = Account.find(cfg.fetch('account_id'))
      admin = account.account_users.where(role: :administrator).first!.user
      duty = account.teams.find(cfg.fetch('duty_id'))
      duty.update!(description: 'Общая очередь: срочные обращения, неопределённая потребность, несколько направлений.',
        allow_auto_assign: false) if duty.description.blank?
      legal = account.teams.where('LOWER(name) = ?', 'юристы').first || account.teams.new(name: 'Юристы')
      if legal.new_record?
        legal.description = 'Юридические вопросы: документы, трудовые и семейные споры, защита прав. Не экстренное реагирование.'
        legal.allow_auto_assign = false
        legal.save!
        duty.members.each { |user| legal.team_members.find_or_create_by!(user: user) }
      end
      # Navigation folders are personal in Chatwoot; create them for every
      # existing staff identity. The built-in Teams view works for future users.
      account.account_users.includes(:user).each do |membership|
        user = membership.user
        account.inboxes.each { |inbox| inbox.inbox_members.find_or_create_by!(user: user) }
        [duty, legal].each do |team|
          folder = account.custom_filters.find_or_initialize_by(
            user: user, name: team == duty ? 'Дежурные' : 'Юридическая помощь', filter_type: :conversation)
          if folder.new_record?
            folder.query = {payload: [{attribute_key: 'team_id', filter_operator: 'equal_to',
              values: [team.id.to_s], query_operator: nil, attribute_model: 'standard'}]}
            folder.save!
          end
        end
      end
      commands = [
        ['Вернуть боту', [{action_name: 'add_private_note', action_params: ['[women-help:return-to-bot]']}]],
        ['Передать юристам', [{action_name: 'assign_team', action_params: [legal.id.to_s]}]],
        ['Передать дежурным', [{action_name: 'assign_team', action_params: [duty.id.to_s]}]]
      ]
      commands.each do |name, actions|
        macro = account.macros.find_or_initialize_by(name: name, visibility: :global)
        if macro.new_record?
          macro.assign_attributes(actions: actions, created_by: admin, updated_by: admin)
          macro.save!
        end
      end
      puts 'ACTIVATION_RESULT=' + {duty_team_id: duty.id, legal_team_id: legal.id,
        legal_members: legal.members.count, folders_ready: true, macros_ready: true}.to_json
    """, {
        "account_id": int(agent.get("CHATWOOT_ACCOUNT_ID", "1")),
        "duty_id": int(agent["CHATWOOT_DUTY_TEAM_ID"]),
    })
    print(json.dumps(result))
    handoff_schema()


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
    if env(CW).get("TELEGRAM_UPDATE_TRANSPORT") == "polling":
        refresh_routes()


def prepare_ingress():
    """Minimal transport env; no LLM keys, administrator tokens or message history."""
    cw = env(CW)
    old = env(BOT) if BOT.exists() else {}
    agent = env(AGENT)
    mode = cw.get("TELEGRAM_UPDATE_TRANSPORT", "webhook")
    if mode not in {"webhook", "polling"}:
        raise ValueError("Invalid Telegram transport")
    token = old.get("TELEGRAM_BOT_TOKEN", "")
    proxy = agent.get("TELEGRAM_PROXY_URL") or old.get("TELEGRAM_PROXY_URL", "")
    if mode == "polling" and not (token and proxy):
        raise ValueError("Missing Telegram transport credentials")
    save(
        INGRESS,
        {
            "TELEGRAM_UPDATE_TRANSPORT": mode,
            "TELEGRAM_BOT_TOKEN": token,
            "TELEGRAM_PROXY_URL": proxy,
            "CHATWOOT_BASE_URL": "http://chatwoot:3000",
            "TELEGRAM_INGRESS_REDIS_URL": "redis://redis:6379/0",
        },
    )
    print(json.dumps({"telegram_ingress_env_prepared": True, "transport": mode}))


def enable_polling():
    """Explicit operator choice; does not drop updates or start a second poller."""
    token = env(BOT)["TELEGRAM_BOT_TOKEN"]
    result = rails(
        """
      channel = Account.find(1).telegram_channels.find_by!(bot_token: cfg.fetch('token'))
      raise 'Inbox missing' unless channel.inbox
      puts 'ACTIVATION_RESULT=' + {inbox_id: channel.inbox.id}.to_json
    """,
        {"token": token},
    )
    cw = env(CW)
    cw["TELEGRAM_UPDATE_TRANSPORT"] = "polling"
    save(CW, cw)
    prepare_ingress()
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


def refresh_routes():
    """Repair both persisted webhook destinations after a test VM IP change.

    Run after Rails has restarted with its new FRONTEND_URL. Re-registering a
    Telegram webhook directly preserves queued updates and the existing inbox.
    """
    cw = env(CW)
    result = rails(
        """
      frontend = cfg.fetch('frontend_url')
      raise 'Rails has not loaded the new frontend URL' unless ENV.fetch('FRONTEND_URL').chomp('/') == frontend
      account = Account.find_by(id: 1)
      bot = account&.agent_bots&.find_by(name: 'Women Help Agent')
      if bot
        health = HTTParty.get(cfg.fetch('agent_health_url'), timeout: 15)
        raise 'Agent endpoint is not ready' unless health.code == 200
      end
      updated = bot && bot.outgoing_url != cfg.fetch('agent_url')
      bot.update!(outgoing_url: cfg.fetch('agent_url')) if updated
      verified = []
      channels = account ? account.telegram_channels : []
      channels.each do |channel|
        polling = cfg.fetch('transport') == 'polling'
        expected = polling ? '' : frontend + '/webhooks/telegram/' + channel.bot_token
        info = HTTParty.get(channel.telegram_api_url + '/getWebhookInfo', timeout: 15).parsed_response
        raise 'Telegram webhook inspection failed' unless info['ok']
        if info.dig('result', 'url') != expected
          method = polling ? '/deleteWebhook' : '/setWebhook'
          body = polling ? {drop_pending_updates: false} : {url: expected, drop_pending_updates: false}
          response = HTTParty.post(channel.telegram_api_url + method, timeout: 15,
            body: body).parsed_response
          raise 'Telegram webhook registration failed' unless response['ok']
          info = HTTParty.get(channel.telegram_api_url + '/getWebhookInfo', timeout: 15).parsed_response
        end
        raise 'Telegram webhook verification failed' unless info['ok'] && info.dig('result', 'url') == expected
        verified << channel.id
      end
      puts 'ACTIVATION_RESULT=' + {agent_route_updated: !!updated,
        agent_configured: !!bot, telegram_transport: cfg.fetch('transport'),
        verified_telegram_channels: verified}.to_json
    """,
        {
            "frontend_url": f"https://{cw['CHATWOOT_HOSTNAME']}",
            "agent_url": f"https://{cw['AGENT_HOSTNAME']}/webhooks/chatwoot/agent/{cw['CHATWOOT_WEBHOOK_SECRET']}",
            "agent_health_url": f"https://{cw['AGENT_HOSTNAME']}/healthz",
            "transport": cw.get("TELEGRAM_UPDATE_TRANSPORT", "webhook"),
        },
    )
    print(json.dumps(result))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "phase",
        choices=[
            "backup",
            "prepare",
            "provision",
            "connect",
            "status",
            "refresh_routes",
            "prepare_ingress",
            "enable_polling",
            "handoff_schema",
            "queues",
        ],
    )
    args = parser.parse_args()
    try:
        globals()[args.phase]()
    except Exception as error:  # noqa: BLE001 - only safe exception metadata may leave the VM
        print(json.dumps({"error_type": type(error).__name__}))
        raise SystemExit(1) from None
