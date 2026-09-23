default:
    @just --list

# Install locked Python dependencies.
setup:
    uv sync --all-groups

# Start the local PostgreSQL container.
db-up:
    podman compose up -d postgres

# Stop the local PostgreSQL container without removing its data.
db-down:
    podman compose stop postgres

# Show the local PostgreSQL container status.
db-status:
    podman compose ps

# Verify additive PostgreSQL schema migrations without creating or deleting user rows.
db-assure:
    uv run python -m scripts.postgres_assurance

# Stop only a previous local instance of this bot's polling command.
stop-local:
    @pids="$(pgrep -f '^uv run python -m app\.bot$' || true)"; if [ -n "$pids" ]; then kill -TERM $pids; sleep 1; fi

# Start the Telegram bot in the foreground.
run: stop-local db-up
    mkdir -p .runtime
    uv run python -m app.bot 2>&1 | tee -a .runtime/bot.log

# Follow the local bot log (last 100 lines first).
logs:
    mkdir -p .runtime
    touch .runtime/bot.log
    tail -n 100 -f .runtime/bot.log

# Run the test suite.
test:
    uv run pytest

# Run the linter.
lint:
    uv run ruff check .

# Run all local checks.
check: lint test

# Send one short, anonymized request to verify Yandex AI Studio access.
llm-health:
    uv run python -m scripts.llm_health_check

# Run deterministic aid and crisis paths without Telegram or LLM access.
scenario-smoke:
    uv run python -m scripts.scenario_smoke

# Replay the versioned behavior suite with deterministic fixture results.
eval-dialogues:
    uv run pytest tests/test_behavior_dataset.py tests/test_dialogue_eval.py -q
    uv run python -m scripts.dialogue_eval --fixtures tests/fixtures/dialogue_agent_outputs.jsonl tests/fixtures/dialogue_scenarios.jsonl

# Run the anonymized behavior suite against the configured Qwen model.
eval-dialogues-live:
    uv run python -m scripts.dialogue_eval --live tests/fixtures/dialogue_scenarios.jsonl

# Run the compact red-flag and ordinary-conversation suite against configured Qwen.
eval-safety-live:
    uv run python -m scripts.dialogue_eval --live tests/fixtures/live_safety_scenarios.jsonl

# Deploy the current Chatwoot contour, never the retired standalone bot.
# The current public IP is resolved by VM ID and checked over SSH before writes.
deploy-prod host="": check
    bash scripts/deploy_chatwoot_test.sh "{{host}}"

# Show the current SSH target without relying on a saved IP address.
prod-host:
    uv run python -m scripts.resolve_prod_host

# Deploy the isolated Chatwoot test contour. It does not switch the live Telegram bot.
deploy-chatwoot-test host="":
    bash scripts/deploy_chatwoot_test.sh "{{host}}"

# Inspect the Chatwoot test contour without reading or printing its secrets.
chatwoot-check host="":
    bash scripts/chatwoot_test_status.sh "{{host}}"

# Generate 15+15 neutral invalid PDF certificates locally.
certificates-generate-test output="output/test-certificates":
    uv run python -m scripts.generate_test_certificate_pdfs "{{output}}" --count 15

# Securely upload and import a directory of PDF certificates.
certificates-import-pdf directory host="":
    bash scripts/import_chatwoot_certificate_pdfs_prod.sh "{{directory}}" "{{host}}"

# Return only test certificates to the available pool.
certificates-reset-test host="":
    bash scripts/manage_chatwoot_test_certificates_prod.sh reset "{{host}}"

# Permanently remove all test certificate rows and their private S3 objects.
certificates-purge-test host="":
    bash scripts/manage_chatwoot_test_certificates_prod.sh purge "{{host}}"

# Create/reuse the duty team and Agent Bot after the dashboard inbox is created.
chatwoot-bootstrap host="":
    bash scripts/bootstrap_chatwoot_agent.sh "{{host}}"

# Follow service-level logs only; conversation bodies stay in the Chatwoot dashboard.
chatwoot-logs host="" service="women-help-chatwoot":
    host="{{host}}"; if [ -z "$host" ]; then host="$(uv run python -m scripts.resolve_prod_host --ip-only)"; fi; ssh -o StrictHostKeyChecking=accept-new -l lebwa82 "$host" "sudo journalctl -u {{service}} -n 100 -f"

# Resolve the current VM IP, refresh generated HTTPS names and check the panel.
chatwoot-refresh-address host="":
    bash scripts/refresh_chatwoot_address.sh "{{host}}"

# Включить приём Telegram через прокси (Chatwoot сохраняет всю переписку).
chatwoot-enable-polling:
    bash scripts/enable_chatwoot_polling.sh
