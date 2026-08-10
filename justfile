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

# Deploy the clean, committed Git snapshot to the MVP VM and verify it.
# Override host when needed: just deploy-prod user@host
deploy-prod host="lebwa82@89.169.180.0": check
    bash scripts/deploy_prod.sh "{{host}}"
