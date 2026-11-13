"""Verified-host heartbeat transport fixtures; never open a real network socket."""

import queue
import socket
import ssl
from unittest.mock import Mock

import pytest

from app.monitoring import heartbeat

URL = "https://incidents.betterstack.com/api/v1/heartbeat/synthetic_secret"
HOST = "incidents.betterstack.com"


class Clock:
    def __init__(self):
        self.now = 0.0

    def spend(self, duration, timeout):
        if duration > timeout:
            self.now += timeout
            raise TimeoutError("synthetic transport timeout")
        self.now += duration


class RawSocket:
    def __init__(self, transport, family):
        self.transport = transport
        self.family = family
        self.timeout = None
        self.closed = False
        self.destination = None
        self.plan = None

    def settimeout(self, value):
        self.timeout = value

    def connect(self, address):
        self.destination = address
        self.plan = self.transport.plans[address[0]]
        self.transport.attempted.append(address[0])
        self.transport.connect_timeouts.append(self.timeout)
        self.transport.clock.spend(self.plan.get("connect_seconds", 0), self.timeout)
        if "connect_error" in self.plan:
            raise self.plan["connect_error"]

    def close(self):
        self.closed = True


class TLSSocket:
    def __init__(self, raw):
        self.raw = raw
        self.transport = raw.transport
        self.plan = raw.plan
        self.timeout = None
        self.closed = False
        self.request = bytearray()
        status = self.plan.get("status", 204)
        self.response = bytearray(
            self.plan.get(
                "response",
                (
                    f"HTTP/1.1 {status} Synthetic\r\nContent-Length: 0\r\n"
                    "Connection: close\r\nLocation: https://unapproved.example/\r\n\r\n"
                ).encode(),
            )
        )

    def settimeout(self, value):
        self.timeout = value

    def do_handshake(self):
        self.transport.handshake_timeouts.append(self.timeout)
        self.transport.clock.spend(self.plan.get("tls_seconds", 0), self.timeout)
        if "tls_error" in self.plan:
            raise self.plan["tls_error"]

    def send(self, data):
        self.transport.clock.spend(self.plan.get("send_seconds", 0), self.timeout)
        if "send_error" in self.plan:
            raise self.plan["send_error"]
        count = min(len(data), self.plan.get("send_chunk", len(data)))
        self.request.extend(data[:count])
        return count

    def recv_into(self, buffer):
        self.transport.clock.spend(self.plan.get("read_seconds", 0), self.timeout)
        count = min(len(buffer), len(self.response), self.plan.get("read_chunk", len(buffer)))
        buffer[:count] = self.response[:count]
        del self.response[:count]
        return count

    def close(self):
        self.closed = True


class Transport:
    def __init__(self):
        self.clock = Clock()
        self.plans = {"192.0.2.1": {}}
        self.addresses = [self.address("192.0.2.1")]
        self.attempted = []
        self.raw = []
        self.tls = []
        self.sni = []
        self.connect_timeouts = []
        self.handshake_timeouts = []
        self.context = Mock()
        self.context.check_hostname = True
        self.context.verify_mode = ssl.CERT_REQUIRED
        self.context.wrap_socket.side_effect = self.wrap
        self.resolver = Mock(side_effect=lambda *args, **kwargs: self.addresses)

    @staticmethod
    def address(ip):
        family = socket.AF_INET6 if ":" in ip else socket.AF_INET
        address = (ip, 443, 0, 0) if family == socket.AF_INET6 else (ip, 443)
        return family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", address

    def make_socket(self, family, socktype, proto):
        assert socktype == socket.SOCK_STREAM
        assert proto == socket.IPPROTO_TCP
        raw = RawSocket(self, family)
        self.raw.append(raw)
        return raw

    def wrap(self, raw, *, server_hostname, do_handshake_on_connect):
        assert do_handshake_on_connect is False
        assert self.context.check_hostname is True
        assert self.context.verify_mode == ssl.CERT_REQUIRED
        self.sni.append(server_hostname)
        secured = TLSSocket(raw)
        self.tls.append(secured)
        return secured


@pytest.fixture
def transport(monkeypatch):
    fixture = Transport()
    monkeypatch.setattr(heartbeat.time, "monotonic", lambda: fixture.clock.now)
    monkeypatch.setattr(heartbeat.socket, "getaddrinfo", fixture.resolver)
    monkeypatch.setattr(heartbeat.socket, "socket", fixture.make_socket)
    monkeypatch.setattr(
        heartbeat.socket,
        "create_connection",
        Mock(side_effect=AssertionError("unexpected fallback")),
    )
    monkeypatch.setattr(heartbeat.ssl, "create_default_context", Mock(return_value=fixture.context))
    return fixture


def test_tls_blackhole_retries_next_resolved_address_with_original_sni(transport):
    transport.plans = {"192.0.2.1": {"tls_seconds": 99}, "192.0.2.2": {}}
    transport.addresses.append(transport.address("192.0.2.2"))
    assert heartbeat.send_heartbeat(URL, True) is True
    assert transport.attempted == ["192.0.2.1", "192.0.2.2"]
    assert transport.clock.now == 1
    assert transport.sni == [HOST, HOST]
    assert all(value <= 1 for value in transport.connect_timeouts + transport.handshake_timeouts)
    assert all(raw.closed for raw in transport.raw)
    assert all(secured.closed for secured in transport.tls)


@pytest.mark.parametrize("ok,suffix", [(True, ""), (False, "/fail")])
def test_post_keeps_original_host_and_path_without_environment_proxies(
    transport, monkeypatch, ok, suffix
):
    monkeypatch.setenv("HTTPS_PROXY", "http://unapproved.example:9999")
    monkeypatch.setenv("ALL_PROXY", "socks5://unapproved.example:9998")
    assert heartbeat.send_heartbeat(URL, ok) is True
    request = bytes(transport.tls[0].request)
    assert request.startswith(
        f"POST /api/v1/heartbeat/synthetic_secret{suffix} HTTP/1.1\r\n".encode()
    )
    assert b"Host: incidents.betterstack.com\r\n" in request
    assert b"User-Agent: NevidimyMonitor/1.0\r\n" in request
    assert b"Content-Length: 0\r\n" in request
    assert b"https://" not in request
    assert "unapproved" not in str(transport.attempted)
    assert transport.resolver.call_args.args == (HOST, 443)
    assert transport.resolver.call_args.kwargs == {
        "family": socket.AF_UNSPEC,
        "type": socket.SOCK_STREAM,
        "proto": socket.IPPROTO_TCP,
    }
    heartbeat.ssl.create_default_context.assert_called_once_with()


def test_official_uptime_host_is_used_only_when_present_in_original_url(transport):
    assert heartbeat.send_heartbeat(URL.replace(HOST, "uptime.betterstack.com"), True) is True
    assert transport.sni == ["uptime.betterstack.com"]
    assert transport.resolver.call_args.args[0] == "uptime.betterstack.com"
    assert b"Host: uptime.betterstack.com\r\n" in transport.tls[0].request


def test_four_second_budget_bounds_repeated_tcp_and_tls_timeouts(transport):
    transport.plans = {
        f"192.0.2.{index}": {"connect_seconds": 0.1, "tls_seconds": 99} for index in range(1, 7)
    }
    transport.addresses = [transport.address(ip) for ip in transport.plans]
    assert heartbeat.send_heartbeat(URL, True) is False
    assert transport.clock.now == 4
    assert len(transport.attempted) == 4
    assert transport.handshake_timeouts[-1] < 1
    assert all(raw.closed for raw in transport.raw)


def test_absolute_deadline_includes_slow_trickling_response_headers(transport):
    transport.plans["192.0.2.1"] = {"read_seconds": 0.25, "read_chunk": 1}
    assert heartbeat.send_heartbeat(URL, True) is False
    assert transport.clock.now == 4
    assert transport.tls[0].closed


def test_absolute_deadline_includes_partial_request_writes(transport):
    transport.plans["192.0.2.1"] = {"send_seconds": 0.25, "send_chunk": 1}
    assert heartbeat.send_heartbeat(URL, True) is False
    assert transport.clock.now == 4


def test_dns_and_ca_preparation_time_reduce_the_request_budget(transport):
    def slow_resolver(*args, **kwargs):
        transport.clock.now += 3.75
        return transport.addresses

    transport.resolver.side_effect = slow_resolver
    transport.plans["192.0.2.1"] = {"connect_seconds": 0.1, "send_seconds": 1}
    assert heartbeat.send_heartbeat(URL, True) is False
    assert transport.clock.now == 4
    assert transport.handshake_timeouts[0] < 0.2


def test_dns_and_ca_preparation_wait_is_bounded_and_has_no_secret_path(transport, monkeypatch):
    completed = Mock()
    completed.get.side_effect = queue.Empty
    thread = Mock()
    factory = Mock(return_value=thread)
    monkeypatch.setattr(heartbeat.queue, "Queue", Mock(return_value=completed))
    monkeypatch.setattr(heartbeat.threading, "Thread", factory)
    assert heartbeat.send_heartbeat(URL, True) is False
    assert completed.get.call_args.kwargs["timeout"] == 4
    assert factory.call_args.kwargs["daemon"] is True
    assert "synthetic_secret" not in str(factory.call_args)
    assert transport.raw == []


def test_certificate_rejection_is_terminal_and_never_sends_heartbeat_path(transport):
    transport.plans = {
        "192.0.2.1": {"tls_error": ssl.SSLCertVerificationError("synthetic untrusted certificate")},
        "192.0.2.2": {},
    }
    transport.addresses.append(transport.address("192.0.2.2"))
    assert heartbeat.send_heartbeat(URL, True) is False
    assert transport.attempted == ["192.0.2.1"]
    assert transport.tls[0].request == b""


@pytest.mark.parametrize("status", [301, 302, 307, 308, 400, 401, 403, 404, 429])
def test_redirects_and_client_errors_are_never_followed_or_retried(transport, status):
    transport.plans = {"192.0.2.1": {"status": status}, "192.0.2.2": {}}
    transport.addresses.append(transport.address("192.0.2.2"))
    assert heartbeat.send_heartbeat(URL, True) is False
    assert transport.attempted == ["192.0.2.1"]
    assert transport.resolver.call_count == 1


def test_server_error_retries_only_next_address_of_original_host(transport):
    transport.plans = {"192.0.2.1": {"status": 503}, "192.0.2.2": {}}
    transport.addresses.append(transport.address("192.0.2.2"))
    assert heartbeat.send_heartbeat(URL, True) is True
    assert transport.attempted == ["192.0.2.1", "192.0.2.2"]
    assert transport.sni == [HOST, HOST]


def test_tcp_error_tries_next_address_without_changing_host(transport):
    transport.plans = {
        "192.0.2.1": {"connect_error": OSError("synthetic unreachable")},
        "192.0.2.2": {},
    }
    transport.addresses.append(transport.address("192.0.2.2"))
    assert heartbeat.send_heartbeat(URL, True) is True
    assert transport.attempted == ["192.0.2.1", "192.0.2.2"]
    assert transport.sni == [HOST]


@pytest.mark.parametrize("status", [200, 201, 299])
def test_only_successful_response_status_is_accepted(transport, status):
    transport.plans["192.0.2.1"] = {"status": status}
    assert heartbeat.send_heartbeat(URL, True) is True


def test_ipv4_is_preferred_addresses_are_deduplicated_and_at_most_four_tried(transport):
    transport.plans = {f"192.0.2.{index}": {"status": 503} for index in range(1, 6)}
    transport.plans["2001:db8::1"] = {}
    transport.addresses = [transport.address("2001:db8::1")]
    transport.addresses.extend(transport.address(ip) for ip in transport.plans if ":" not in ip)
    transport.addresses.insert(2, transport.address("192.0.2.1"))
    assert heartbeat.send_heartbeat(URL, True) is False
    assert transport.attempted == ["192.0.2.1", "192.0.2.2", "192.0.2.3", "192.0.2.4"]


def test_ipv6_can_be_used_when_it_is_the_same_host_resolved_address(transport):
    transport.plans = {"2001:db8::1": {}}
    transport.addresses = [transport.address("2001:db8::1")]
    assert heartbeat.send_heartbeat(URL, True) is True
    assert transport.attempted == ["2001:db8::1"]
    assert transport.sni == [HOST]


def test_each_call_resolves_fresh_and_does_not_cache_credentials(transport):
    assert heartbeat.send_heartbeat(URL, True) is True
    assert heartbeat.send_heartbeat(URL.replace("synthetic_secret", "second_secret"), True) is True
    assert transport.resolver.call_count == 2
    assert b"second_secret" not in transport.tls[0].request
    assert b"synthetic_secret" not in transport.tls[1].request


@pytest.mark.parametrize(
    "url",
    [
        None,
        "http://incidents.betterstack.com/api/v1/heartbeat/secret",
        "https://incidents.betterstack.com.evil.example/api/v1/heartbeat/secret",
        "https://incidents.betterstack.com@evil.example/api/v1/heartbeat/secret",
        "https://incidents.betterstack.com:443/api/v1/heartbeat/secret",
        "https://192.0.2.1/api/v1/heartbeat/secret",
        URL + "?secret=extra",
        URL + "#fragment",
        URL + "/fail",
        URL + "\n",
        "https://uptime.betterstack.com/api/v1/heartbeat/",
    ],
)
def test_unapproved_urls_are_rejected_before_resolution(transport, url):
    assert heartbeat.send_heartbeat(url, True) is False
    transport.resolver.assert_not_called()
    assert transport.raw == []


def test_unknown_status_is_not_sent(transport):
    assert heartbeat.send_heartbeat(URL, None) is False
    transport.resolver.assert_not_called()


def test_transport_failure_details_never_escape_or_print(transport, capsys):
    transport.plans["192.0.2.1"] = {"send_error": OSError(URL)}
    assert heartbeat.send_heartbeat(URL, True) is False
    assert capsys.readouterr() == ("", "")


def test_dns_failure_is_silent_and_never_attempts_a_connection(transport, capsys):
    transport.resolver.side_effect = socket.gaierror("synthetic resolver failure")
    assert heartbeat.send_heartbeat(URL, True) is False
    assert transport.raw == []
    assert capsys.readouterr() == ("", "")


def test_ca_preparation_failure_is_silent_and_never_attempts_a_connection(transport, capsys):
    heartbeat.ssl.create_default_context.side_effect = OSError("synthetic CA failure")
    assert heartbeat.send_heartbeat(URL, True) is False
    transport.resolver.assert_not_called()
    assert transport.raw == []
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize(("plan", "stage", "reason"), [
    ({"connect_error": OSError(111, URL)}, "tcp_connect", "network_error"),
    ({"tls_error": ssl.SSLCertVerificationError(URL)}, "tls_handshake",
     "certificate_verification"),
    ({"tls_seconds": 10}, "tls_handshake", "timeout"),
    ({"send_error": OSError(URL)}, "http_request", "network_error"),
    ({"response": b"invalid " + URL.encode()}, "http_response", "http_protocol"),
    ({"status": 429}, "http_response", "http_status"),
    ({"status": 503}, "http_response", "http_status"),
])
def test_delivery_errors_log_safe_stage_metadata(transport, caplog, plan, stage, reason):
    import json

    transport.plans["192.0.2.1"] = plan
    assert heartbeat.send_heartbeat(URL, True) is False
    records = [json.loads(r.getMessage()) for r in caplog.records]
    record = next(r for r in records if r["stage"] == stage)
    assert record["event"] == "heartbeat_delivery_error"
    assert record["host"] == HOST
    assert record["target_ip"] == "192.0.2.1"
    assert record["reason"] == reason
    assert type(record["elapsed_ms"]) is int
    if "status" in plan:
        assert record["status"] == plan["status"]
    if "connect_error" in plan:
        assert record["errno"] == 111
    assert URL not in caplog.text
    assert "synthetic_secret" not in caplog.text
    assert "/api/v1/heartbeat/" not in caplog.text
    assert "Traceback" not in caplog.text


@pytest.mark.parametrize("stage", ["ca_load", "dns_lookup"])
def test_preparation_errors_log_safe_metadata(transport, caplog, monkeypatch, stage):
    import json

    if stage == "ca_load":
        monkeypatch.setattr(heartbeat.ssl, "create_default_context",
                            Mock(side_effect=OSError(URL)))
    else:
        transport.resolver.side_effect = socket.gaierror(-2, URL)
    assert heartbeat.send_heartbeat(URL, True) is False
    record = json.loads(caplog.records[0].getMessage())
    assert record["stage"] == stage
    assert record["reason"] == ("dns_error" if stage == "dns_lookup" else "network_error")
    assert "synthetic_secret" not in caplog.text


def test_successful_retry_preserves_failure_diagnostics(transport, caplog):
    import json

    transport.addresses.append(transport.address("192.0.2.2"))
    transport.plans["192.0.2.1"] = {"tls_seconds": 10}
    transport.plans["192.0.2.2"] = {}
    assert heartbeat.send_heartbeat(URL, True) is True
    records = [json.loads(r.getMessage()) for r in caplog.records]
    assert len(records) == 1
    assert records[0]["reason"] == "timeout"
    assert records[0]["stage"] == "tls_handshake"
