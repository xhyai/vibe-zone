"""Bounded, direct HTTP client for hostile contestant responses (stdlib only)."""
import http.client
import json
import socket
import threading
import time
from urllib.parse import urlsplit

# Twenty 280-character astral-Unicode posts can exceed 64 KiB when JSON-escaped.
MAX_RESPONSE = 128 * 1024


def request_json(url, method="GET", body=None, raw=None, content_type="application/json", timeout=15):
    target = urlsplit(url)
    if target.scheme != "http" or not target.hostname or target.username or target.fragment:
        raise ValueError("Probe requires a direct local HTTP URL")
    payload = raw if raw is not None else json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    connection = http.client.HTTPConnection(target.hostname, target.port or 80, timeout=timeout)
    deadline = time.monotonic() + timeout
    owned_socket = None
    def expire():
        if owned_socket is not None:
            try:
                owned_socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
    timer = threading.Timer(timeout, expire)
    timer.daemon = True
    timer.start()
    try:
        connection.connect()
        owned_socket = connection.sock
        if time.monotonic() >= deadline:
            raise TimeoutError("HTTP connect deadline exceeded")
        connection.request(method, target.path + ("?" + target.query if target.query else ""), payload,
                           {"Content-Type": content_type})
        response = connection.getresponse()
        if 300 <= response.status < 400:
            raise ValueError("Redirects are forbidden")
        if response.getheader("Content-Encoding", "identity") != "identity":
            raise ValueError("Compressed probe responses are forbidden")
        if response.getheader("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise ValueError("Response must be JSON")
        if int(response.getheader("Content-Length", "0")) > MAX_RESPONSE:
            raise ValueError("Response exceeds 128 KiB")
        data = bytearray()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("HTTP response deadline exceeded")
            # HTTPResponse retains the socket even for Connection: close.
            response.fp.raw._sock.settimeout(remaining)
            chunk = response.read1(min(8192, MAX_RESPONSE + 1 - len(data)))
            data.extend(chunk)
            if len(data) > MAX_RESPONSE:
                raise ValueError("Response exceeds 128 KiB")
            if not chunk:
                break
            if response.isclosed():
                break
        value = json.loads(data.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("Response must be a JSON object")
        return response.status, value
    except Exception:
        if time.monotonic() >= deadline:
            raise TimeoutError("HTTP response deadline exceeded") from None
        raise
    finally:
        timer.cancel()
        connection.close()
