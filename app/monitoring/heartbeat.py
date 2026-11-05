"""Deadline-bounded HTTPS heartbeats to the configured, verified official host."""

import http.client
import io
import ipaddress
import queue
import re
import socket
import ssl
import threading
import time

_URL = re.compile(
    r"https://(?P<host>incidents\.betterstack\.com|uptime\.betterstack\.com)"
    r"(?P<path>/api/v1/heartbeat/[A-Za-z0-9_-]+)"
)
_DEADLINE_SECONDS = 4.0
_CONNECT_SECONDS = 1.0
_MAX_ADDRESSES = 4
_USER_AGENT = "NevidimyMonitor/1.0"


def _remaining(deadline: float, cap: float | None = None) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("heartbeat_timeout")
    return remaining if cap is None else min(remaining, cap)


def _prepare(host: str, deadline: float) -> tuple:
    """Bound CA loading and a resolver without a platform DNS timeout option.

    A timed-out resolver can finish only its DNS lookup in the daemon thread;
    it never receives the secret path and cannot open an HTTPS connection.
    """
    completed = queue.Queue(maxsize=1)

    def lookup():
        try:
            context = ssl.create_default_context()
            addresses = socket.getaddrinfo(
                host,
                443,
                family=socket.AF_UNSPEC,
                type=socket.SOCK_STREAM,
                proto=socket.IPPROTO_TCP,
            )
            result = context, addresses
        except Exception:  # noqa: BLE001 - a worker must not print external failure details
            result = None, []
        completed.put(result)

    threading.Thread(target=lookup, name="heartbeat-dns", daemon=True).start()
    try:
        context, result = completed.get(timeout=_remaining(deadline))
    except queue.Empty:
        return None, []
    # Every attempted address comes from this lookup for the original host.
    # Deduplicate before limiting so repeated resolver rows do not waste retries.
    addresses = []
    seen = set()
    for family, socktype, proto, _, address in result:
        if family not in (socket.AF_INET, socket.AF_INET6):
            continue
        if socktype != socket.SOCK_STREAM or proto != socket.IPPROTO_TCP or address[1] != 443:
            continue
        ip = ipaddress.ip_address(address[0])
        if ip.version != (4 if family == socket.AF_INET else 6):
            continue
        key = family, address
        if key not in seen:
            seen.add(key)
            addresses.append((family, socktype, proto, address))
    addresses.sort(key=lambda item: item[0] != socket.AF_INET)
    return context, addresses[:_MAX_ADDRESSES]


class _DeadlineReader(io.RawIOBase):
    """Apply the absolute deadline to every header read, including slow trickles."""

    def __init__(self, connection):
        super().__init__()
        self.connection = connection

    def readable(self):
        return True

    def readinto(self, buffer):
        self.connection.socket.settimeout(_remaining(self.connection.deadline))
        return self.connection.socket.recv_into(buffer)


class _DeadlineSocket:
    def __init__(self, connection, deadline):
        self.socket = connection
        self.deadline = deadline

    def sendall(self, data):
        pending = memoryview(data)
        while pending:
            self.socket.settimeout(_remaining(self.deadline))
            sent = self.socket.send(pending)
            if sent <= 0:
                raise OSError("heartbeat_connection_closed")
            pending = pending[sent:]

    def makefile(self, mode):
        if mode != "rb":
            raise ValueError("heartbeat_stream_mode")
        return io.BufferedReader(_DeadlineReader(self))

    def close(self):
        self.socket.close()


def _post(context, host: str, path: str, address: tuple, deadline: float) -> int:
    family, socktype, proto, destination = address
    raw = socket.socket(family, socktype, proto)
    secured = None
    connection = None
    response = None
    try:
        raw.settimeout(_remaining(deadline, _CONNECT_SECONDS))
        raw.connect(destination)
        raw.settimeout(_remaining(deadline, _CONNECT_SECONDS))
        secured = context.wrap_socket(raw, server_hostname=host, do_handshake_on_connect=False)
        secured.settimeout(_remaining(deadline, _CONNECT_SECONDS))
        secured.do_handshake()
        connection = http.client.HTTPSConnection(host, 443, context=context)
        # HTTPSConnection must never resolve/connect on its own: the socket has
        # already completed certificate verification for the original hostname.
        connection.sock = _DeadlineSocket(secured, deadline)
        connection.request(
            "POST",
            path,
            body=b"",
            headers={
                "Host": host,
                "User-Agent": _USER_AGENT,
                "Connection": "close",
            },
        )
        response = connection.getresponse()
        _remaining(deadline)
        return response.status
    finally:
        if response is not None:
            response.close()
        if connection is not None:
            connection.close()
        if secured is not None:
            secured.close()
        raw.close()


def send_heartbeat(url: str, ok: bool) -> bool:
    """POST one configured official heartbeat; suppress all transport details.

    Fresh DNS addresses of this exact hostname are tried within four seconds.
    Certificate errors, redirects, and client errors are terminal. Network
    failures and server errors may retry the idempotent heartbeat on the next
    resolved address. No proxy, fallback hostname, redirect, or global cache is used.
    """
    if not isinstance(url, str) or type(ok) is not bool:
        return False
    match = _URL.fullmatch(url)
    if match is None:
        return False
    host = match["host"]
    path = match["path"] + ("" if ok else "/fail")
    deadline = time.monotonic() + _DEADLINE_SECONDS
    try:
        context, addresses = _prepare(host, deadline)
        for address in addresses:
            try:
                status = _post(context, host, path, address, deadline)
            except ssl.SSLCertVerificationError:
                return False
            except (OSError, http.client.HTTPException):
                _remaining(deadline)
                continue
            if 200 <= status < 300:
                return True
            if not 500 <= status < 600:
                return False
    except Exception:  # noqa: BLE001 - URLs and transport errors must never escape or be logged
        return False
    return False
