"""Bounded HTTP transport. No redirects, proxy inheritance or disabled TLS."""

from __future__ import annotations

import ipaddress
import socket
import time
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .suite import MAX_BYTES, SuiteError


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass(frozen=True)
class Target:
    origin: str
    hostname: str
    port: int
    local: bool


def validate_target(url: str, allow_remote: bool = False) -> Target:
    if any(ord(c) <= 32 or ord(c) >= 127 for c in url) or "\\" in url:
        raise SuiteError("target must be an ASCII HTTP(S) origin")
    try:
        parsed = urlsplit(url)
        port = parsed.port if parsed.port is not None else (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise SuiteError("invalid target origin") from exc
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or not 1 <= port <= 65535
            or parsed.username is not None or parsed.password is not None
            or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
        raise SuiteError("target must be an HTTP(S) origin without credentials, path, query or fragment")
    hostname = parsed.hostname
    try:
        local = ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        local = hostname.lower() == "localhost"
    if not local:
        if not allow_remote:
            raise SuiteError("non-loopback targets require --allow-remote")
        if parsed.scheme != "https":
            raise SuiteError("remote targets require HTTPS")
    return Target(f"{parsed.scheme}://{parsed.netloc}", hostname, port, local)


@dataclass(frozen=True)
class Response:
    status: int | None
    headers: dict[str, str]
    body: bytes
    elapsed_ms: float
    error: str | None = None


def request(target: Target, method: str, path: str, headers: dict[str, str],
            body: bytes | None, timeout: float) -> Response:
    start = time.monotonic()
    try:
        # Check localhost resolution every time, including after a DNS change.
        if target.local:
            addresses = socket.getaddrinfo(target.hostname, target.port, type=socket.SOCK_STREAM)
            if not addresses or any(not ipaddress.ip_address(a[4][0]).is_loopback for a in addresses):
                return Response(None, {}, b"", 0, "connection")
        req = Request(target.origin + path, data=body, headers=headers, method=method)
        opener = build_opener(ProxyHandler({}), NoRedirect())
        try:
            response = opener.open(req, timeout=timeout)
        except HTTPError as exc:
            response = exc
        with response:
            chunks, size = [], 0
            while size <= MAX_BYTES:
                if time.monotonic() - start >= timeout:
                    raise TimeoutError()
                chunk = response.read1(min(65536, MAX_BYTES + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
            payload = b"".join(chunks)
            elapsed = (time.monotonic() - start) * 1000
            if size > MAX_BYTES:
                return Response(None, {}, b"", elapsed, "response_too_large")
            return Response(response.code, {k.lower(): v for k, v in response.headers.items()},
                            payload, elapsed)
    except (TimeoutError, socket.timeout):
        error = "timeout"
    except URLError as exc:
        error = "timeout" if isinstance(exc.reason, (TimeoutError, socket.timeout)) else "connection"
    except (OSError, ValueError):
        error = "connection"
    return Response(None, {}, b"", (time.monotonic() - start) * 1000, error)

