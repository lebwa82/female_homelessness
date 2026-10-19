"""Silent container liveness probe, without shell-embedded Python or credentials."""

from urllib.request import urlopen


def is_healthy(port: int, *, tls: bool = False) -> bool:
    try:
        scheme = "https" if tls else "http"
        with urlopen(f"{scheme}://127.0.0.1:{port}/healthz", timeout=4) as response:
            return response.status == 200
    except (OSError, ValueError):
        return False


def main() -> int:
    from app.config import settings

    return 0 if is_healthy(settings.chatwoot_listen_port, tls=bool(settings.chatwoot_tls_cert_file)) else 1


if __name__ == "__main__":
    raise SystemExit(main())
