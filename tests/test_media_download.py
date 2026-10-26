from __future__ import annotations

import collections
import gzip
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import pytest
import requests

from app import media


BODY = b"\x89PNG\r\n\x1a\n" + b"test-pixels" * 100
TIMEOUT = .12


@pytest.fixture
def network(monkeypatch):
    """Both the origin and account HTTP proxy are owned loopback servers."""
    records = []
    counts = collections.Counter()
    behavior = {}
    sessions = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def log_message(self, *args):
            pass

        def do_GET(self):
            path = urlsplit(self.path).path
            route = self.server.route
            counts[route, path] += 1
            records.append({"route": route, "path": path, "headers": dict(self.headers)})
            mode = behavior.get((route, path), behavior.get(path, "ok"))
            try:
                if mode == "headers_timeout":
                    time.sleep(.3)
                if isinstance(mode, tuple) and mode[0] in {"redirect", "redirect_cookie"}:
                    self.send_response(302)
                    self.send_header("Location", mode[1])
                    self.send_header("Content-Length", "0")
                    if mode[0] == "redirect_cookie":
                        self.send_header("Set-Cookie", "source_session=not-forwarded; Path=/")
                    self.end_headers()
                    return
                if isinstance(mode, int):
                    self.send_response(mode)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                payload = BODY
                if mode == "gzip":
                    payload = gzip.compress(BODY)
                if mode in {"html", "mislabeled_html"}:
                    payload = b"<!DOCTYPE html><html>error document</html>"
                if mode == "drip":
                    payload = b"12345678"
                self.send_response(200)
                mime = "text/html" if mode == "html" else "application/octet-stream" if mode == "octet" else "image/png"
                self.send_header("Content-Type", mime)
                self.send_header("Content-Disposition", 'attachment; filename="../../untrusted.html"')
                if mode == "gzip":
                    self.send_header("Content-Encoding", "gzip")
                length = 2 * media.DOWNLOAD_CHUNK_BYTES if mode in {"body_timeout", "truncated"} else len(payload)
                self.send_header("Content-Length", str(length))
                self.end_headers()
                if mode in {"body_timeout", "truncated"}:
                    self.wfile.write(b"x" * media.DOWNLOAD_CHUNK_BYTES)
                    self.wfile.flush()
                    if mode == "body_timeout":
                        time.sleep(.3)
                        self.wfile.write(b"x" * media.DOWNLOAD_CHUNK_BYTES)
                    else:
                        self.wfile.write(b"short")
                    return
                if mode == "drip":
                    for value in payload:
                        self.wfile.write(bytes([value]))
                        self.wfile.flush()
                        time.sleep(.03)
                else:
                    self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

    origin = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    proxy = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    origin.route, proxy.route = "direct", "account_proxy"
    origin.daemon_threads = proxy.daemon_threads = True
    threads = [threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
               for server in (origin, proxy)]
    for thread in threads:
        thread.start()
    base = f"http://127.0.0.1:{origin.server_port}"
    proxy_url = f"http://127.0.0.1:{proxy.server_port}"
    real_session = requests.Session

    class LocalSession(real_session):
        def __init__(self):
            super().__init__()
            self.test_closed = False
            sessions.append(self)

        def get(self, url, **kwargs):
            assert url.startswith(base + "/"), "a non-owned network destination was attempted"
            assert kwargs["timeout"] == (15, 90)
            kwargs["timeout"] = (.2, TIMEOUT)
            return super().get(url, **kwargs)

        def close(self):
            self.test_closed = True
            super().close()

    monkeypatch.setattr(media.requests, "Session", LocalSession)
    monkeypatch.setattr(media, "_public_direct_url", lambda url: url.startswith(base + "/"))
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:1")
    try:
        yield {"base": base, "proxy": proxy_url, "behavior": behavior,
               "records": records, "counts": counts, "sessions": sessions}
    finally:
        for server in (origin, proxy):
            server.shutdown()
            server.server_close()


def download(network, path="/asset.png", kind="images"):
    return media._download(network["base"] + path, kind, network["proxy"])


def test_public_media_direct_success_is_anonymous_and_ignores_machine_proxy(network):
    assert download(network) == (BODY, "image/png", "asset.png")
    assert [row["route"] for row in network["records"]] == ["direct"]
    assert len(network["sessions"]) == 1
    for session in network["sessions"]:
        assert session.trust_env is False and session.auth is None and session.test_closed
    headers = {key.lower(): value for key, value in network["records"][0]["headers"].items()}
    assert headers["accept-encoding"] == "identity"
    assert not set(headers) & {"authorization", "cookie", "dfp", "x-api-key", "token"}


def test_transient_direct_failure_uses_original_account_proxy(network):
    network["behavior"]["direct", "/asset.png"] = 503
    assert download(network)[0] == BODY
    assert [row["route"] for row in network["records"]] == ["direct", "account_proxy"]
    assert len(network["sessions"]) == 2 and all(x.test_closed for x in network["sessions"])


def test_both_routes_fail_with_three_attempt_limit_and_safe_diagnostics(network):
    network["behavior"]["/asset.png"] = 503
    secret = "signed-secret-should-not-appear"
    with pytest.raises(media.MediaError) as raised:
        download(network, "/asset.png?token=" + secret)
    assert [row["route"] for row in network["records"]] == ["direct", "account_proxy", "direct"]
    assert len(raised.value.details["attempts"]) == 3
    assert "http_status" in str(raised.value) and "HTTP 503" in str(raised.value)
    assert secret not in str(raised.value) and "/asset.png" not in str(raised.value)
    assert "source_sha256=" in str(raised.value)


@pytest.mark.parametrize("status", [401, 403, 404])
def test_permanent_http_rejections_are_not_retried(network, status):
    network["behavior"]["/asset.png"] = status
    with pytest.raises(media.MediaError) as raised:
        download(network)
    assert len(network["records"]) == 1
    assert raised.value.details["attempts"][0]["stage"] == "http_status"


def test_partial_body_is_discarded_before_proxy_retry_and_no_range_is_used(network):
    network["behavior"]["direct", "/asset.png"] = "truncated"
    assert download(network)[0] == BODY
    assert [row["route"] for row in network["records"]] == ["direct", "account_proxy"]
    assert all("range" not in {key.lower() for key in row["headers"]} for row in network["records"])


def test_header_timeout_retains_original_exception_and_stage(network):
    network["behavior"]["/asset.png"] = "headers_timeout"
    with pytest.raises(media.MediaError) as raised:
        download(network)
    assert len(network["records"]) == 3
    assert all(row["stage"] == "headers" and row["bytes"] == 0 for row in raised.value.details["attempts"])
    assert "ReadTimeout" in str(raised.value)


def test_body_read_timeout_wrapped_as_connection_error_is_retried(network):
    network["behavior"]["/asset.png"] = "body_timeout"
    with pytest.raises(media.MediaError) as raised:
        download(network)
    attempts = raised.value.details["attempts"]
    assert len(attempts) == 3
    assert all(row["stage"] == "body" and row["bytes"] == media.DOWNLOAD_CHUNK_BYTES for row in attempts)
    assert all("ConnectionError" in row["classes"] and "ReadTimeoutError" in row["classes"] for row in attempts)
    assert "read timeout" in str(raised.value)


def test_short_content_length_keeps_incomplete_read_diagnostics(network):
    network["behavior"]["/asset.png"] = "truncated"
    with pytest.raises(media.MediaError) as raised:
        download(network)
    assert len(network["records"]) == 3
    assert all("IncompleteRead" in row["classes"] for row in raised.value.details["attempts"])
    assert all(row["expected"] == 2 * media.DOWNLOAD_CHUNK_BYTES for row in raised.value.details["attempts"])


@pytest.mark.parametrize("mode", ["html", "mislabeled_html"])
def test_html_is_a_nonretryable_format_error(network, mode):
    network["behavior"]["/asset.png"] = mode
    with pytest.raises(media.MediaError, match="wrong content type"):
        download(network)
    assert len(network["records"]) == 1


def test_declared_size_limit_is_not_retried(network, monkeypatch):
    monkeypatch.setattr(media, "MAX_MEDIA_BYTES", 32)
    with pytest.raises(media.MediaError, match="reference media exceeds"):
        download(network)
    assert len(network["records"]) == 1


def test_gzip_is_decoded_without_comparing_decoded_length_to_wire_length(network):
    network["behavior"]["/asset.png"] = "gzip"
    assert len(gzip.compress(BODY)) != len(BODY)
    assert download(network)[0] == BODY
    assert len(network["records"]) == 1


def test_redirect_uses_validated_final_filename_and_ignores_content_disposition(network):
    network["behavior"]["/start.html"] = ("redirect", "/final.png")
    network["behavior"]["/final.png"] = "octet"
    assert download(network, "/start.html") == (BODY, "image/png", "final.png")
    assert [row["path"] for row in network["records"]] == ["/start.html", "/final.png"]


def test_direct_redirect_to_private_destination_is_blocked_before_request(network):
    network["behavior"]["/asset.png"] = ("redirect", "http://192.168.1.1/private.png")
    with pytest.raises(media.MediaError, match="redirect destination is not public"):
        download(network)
    assert len(network["records"]) == 1


def test_redirect_does_not_forward_source_set_cookie_to_next_hop(network):
    network["behavior"]["/asset.png"] = ("redirect_cookie", "/final.png")
    assert download(network)[0] == BODY
    assert [row["path"] for row in network["records"]] == ["/asset.png", "/final.png"]
    assert all("cookie" not in {key.lower() for key in row["headers"]} for row in network["records"])


def test_redirect_chain_is_bounded_and_is_not_retried(network):
    for index in range(6):
        network["behavior"][f"/r/{index}"] = ("redirect", f"/r/{index+1}")
    with pytest.raises(media.MediaError, match="redirect limit"):
        download(network, "/r/0")
    assert len(network["records"]) == media.MAX_DOWNLOAD_REDIRECTS + 1
    assert all(row["route"] == "direct" for row in network["records"])


def test_private_original_source_keeps_account_proxy_only(network, monkeypatch):
    monkeypatch.setattr(media, "_public_direct_url", lambda url: False)
    assert download(network)[0] == BODY
    assert [row["route"] for row in network["records"]] == ["account_proxy"]


def test_read_timeout_is_an_idle_timeout_and_does_not_claim_a_total_deadline(network):
    network["behavior"]["/asset.png"] = "drip"
    started = time.monotonic()
    assert download(network)[0] == b"12345678"
    assert time.monotonic() - started > TIMEOUT
    assert len(network["records"]) == 1


def test_account_proxy_remains_required_even_for_public_source():
    with pytest.raises(media.MediaError, match="account proxy is required"):
        media._download("https://public.example.test/a.png", "images")


@pytest.mark.parametrize("source", [
    "http://127.0.0.1/a.png", "http://10.0.0.1/a.png", "http://192.168.1.1/a.png",
    "http://169.254.169.254/a.png", "http://[::1]/a.png", "http://224.0.0.1/a.png",
    "http://localhost/a.png", "http://x.localhost/a.png", "http://camera.local/a.png",
    "https://elevenlabs.io/apis/points/query/summary", "https://passport.elevenlabs.io/a.png",
    "https://intl-upload.iqiyi.com/upload",
])
def test_nonpublic_and_account_api_origins_are_never_direct_eligible(source):
    assert not media._public_direct_url(source)


def test_dns_private_and_mixed_answers_are_not_direct_eligible(monkeypatch):
    def address(value):
        return (socket.AF_INET, socket.SOCK_STREAM, 6, "", (value, 443))
    monkeypatch.setattr(media.socket, "getaddrinfo", lambda *args, **kwargs: [address("10.2.3.4")])
    assert not media._public_direct_url("https://private-answer.example.test/a.png")
    monkeypatch.setattr(media.socket, "getaddrinfo", lambda *args, **kwargs: [address("8.8.8.8"), address("10.2.3.4")])
    assert not media._public_direct_url("https://mixed-answer.example.test/a.png")
    monkeypatch.setattr(media.socket, "getaddrinfo", lambda *args, **kwargs: [address("8.8.8.8")])
    assert media._public_direct_url("https://public-answer.example.test/a.png")


def test_socks_proxy_fallback_preserves_remote_dns_scheme(monkeypatch):
    monkeypatch.setattr(media, "_public_direct_url", lambda source: False)
    calls = []
    def attempt(source, kind, route, proxy):
        calls.append((route, proxy))
        return BODY, "image/png", "asset.png"
    monkeypatch.setattr(media, "_http_download_attempt", attempt)
    assert media._download("https://source.example.test/asset.png", "images", "socks5://127.0.0.1:9080")[0] == BODY
    assert calls == [("account_proxy", "socks5h://127.0.0.1:9080")]
