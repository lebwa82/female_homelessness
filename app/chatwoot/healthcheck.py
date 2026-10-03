"""Silent container liveness probe, without shell-embedded Python or credentials."""

from urllib.request import urlopen


def is_healthy(port: int) -> bool:
    try:
        with urlopen(f"http://127.0.0.1:{port}/healthz", timeout=4) as response:
            return response.status == 200
    except (OSError, ValueError):
        return False


def main() -> int:
    from app.config import settings

    return 0 if is_healthy(settings.chatwoot_listen_port) else 1


if __name__ == "__main__":
    raise SystemExit(main())
