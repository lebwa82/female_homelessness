"""Check SOCKS through the private SSH socket without credentials or bot API."""

import socket
import time

SOCKET_PATH = "/opt/nevidimy/run/proxy-tunnel/socks.sock"


def ready() -> bool:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(2)
            connection.connect(SOCKET_PATH)
            connection.sendall(b"\x05\x01\x02")
            reply = b""
            while len(reply) < 2:
                chunk = connection.recv(2 - len(reply))
                if not chunk:
                    return False
                reply += chunk
            return reply == b"\x05\x02"
    except OSError:
        return False


def wait_ready() -> bool:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if ready():
            return True
        time.sleep(0.25)
    return False


if __name__ == "__main__":
    raise SystemExit(0 if wait_ready() else 1)
