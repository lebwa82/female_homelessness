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

# Start the Telegram bot in the foreground.
run: db-up
    uv run python -m app.bot

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
