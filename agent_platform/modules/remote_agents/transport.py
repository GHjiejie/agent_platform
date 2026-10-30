"""Bounded HTTPS JSON transport with explicit origins and pinned public addresses.

This module is not a general URL fetcher. Callers still authorize the tenant,
reserve the request budget and persist dispatch intent before POST. No redirects,
proxy environment, DNS re-resolution during connect, or automatic retries occur.
"""

import asyncio
import http.client
import ipaddress
import json
import re
import socket
import ssl
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

from agent_platform.infrastructure.errors import PlatformError

MAX_BODY_BYTES = 1024 * 1024


def invalid_endpoint():
    return PlatformError(
        422,
        "invalid_remote_endpoint",
        "远程 Agent 地址必须是已允许的 HTTPS 地址，不能包含凭据、查询参数或特殊路径。",
    )


@dataclass(frozen=True)
class Endpoint:
    host: str
    port: int
    authority: str
    origin: str
    path: str

    @classmethod
    def parse(cls, url, *, allowed_origins=None):
        if (
            not isinstance(url, str)
            or not 1 <= len(url) <= 2048
            or any(ord(char) < 33 or ord(char) > 126 for char in url)
        ):
            raise invalid_endpoint()
        try:
            parsed = urlsplit(url)
            host = parsed.hostname
            port = parsed.port if parsed.port is not None else 443
            if (
                parsed.scheme != "https"
                or not host
                or parsed.username is not None
                or parsed.password is not None
                or parsed.netloc.endswith(":")
                or "?" in url
                or "#" in url
                or "\\" in url
                or not 1 <= port <= 65535
            ):
                raise ValueError("Invalid URL")
            try:
                address = ipaddress.ip_address(host)
                host = address.compressed
            except ValueError:
                if not re.fullmatch(
                    r"[a-zA-Z0-9](?:[a-zA-Z0-9.-]{0,251}[a-zA-Z0-9])?", host
                ):
                    raise ValueError("Invalid hostname") from None
                if any(
                    not label
                    or len(label) > 63
                    or label.startswith("-")
                    or label.endswith("-")
                    for label in host.split(".")
                ):
                    raise ValueError("Invalid hostname") from None
            host = host.lower()
            authority = f"[{host}]" if ":" in host else host
            if port != 443:
                authority += f":{port}"
            path = parsed.path.rstrip("/")
            if path and (
                not path.startswith("/")
                or any(
                    not re.fullmatch(r"[a-zA-Z0-9._~-]+", part) or part in {".", ".."}
                    for part in path[1:].split("/")
                )
            ):
                raise ValueError("Invalid path")
            endpoint = cls(host, port, authority, f"https://{authority}", path)
        except (ValueError, TypeError):
            raise invalid_endpoint() from None
        if allowed_origins is not None:
            allowed = set()
            for value in allowed_origins:
                entry = cls.parse(value)
                if entry.path:
                    raise invalid_endpoint()
                allowed.add(entry.origin)
            if endpoint.origin not in allowed:
                raise PlatformError(
                    403,
                    "remote_origin_not_allowed",
                    "远程 Agent 地址未列入平台允许范围。",
                )
        return endpoint


def public_addresses(host, port):
    """Resolve once; refuse the entire answer if any destination is not public."""
    addresses = []
    for _family, _kind, _proto, _name, destination in socket.getaddrinfo(
        host, port, type=socket.SOCK_STREAM
    ):
        address = ipaddress.ip_address(destination[0])
        if (
            not address.is_global
            or address.is_multicast
            or address.is_reserved
            or isinstance(address, ipaddress.IPv6Address)
            and (
                address.sixtofour
                or address.teredo
                or address.scope_id
                or address.is_site_local
                or address.ipv4_mapped
                or address in ipaddress.ip_network("64:ff9b::/96")
                or address in ipaddress.ip_network("64:ff9b:1::/48")
            )
        ):
            raise PlatformError(
                403,
                "remote_address_blocked",
                "远程 Agent 不能访问本地、内网或保留地址。",
            )
        if address.compressed not in addresses:
            addresses.append(address.compressed)
    if not addresses:
        raise PlatformError(502, "remote_dns_empty", "远程 Agent 地址解析失败。")
    return addresses


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, endpoint, address, timeout):
        super().__init__(
            endpoint.host,
            endpoint.port,
            timeout=timeout,
            context=ssl.create_default_context(),
        )
        self.address = address

    def connect(self):
        # Keep the original hostname for certificate verification and SNI while
        # connecting only to the exact IP checked by public_addresses().
        stream = socket.create_connection((self.address, self.port), self.timeout)
        try:
            self.sock = self._context.wrap_socket(stream, server_hostname=self.host)
        except BaseException:
            stream.close()
            raise


def decode_json(data):
    def unique_pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result

    def constant(_value):
        raise ValueError("Non-finite JSON number")

    return json.loads(
        data.decode("utf-8"), object_pairs_hook=unique_pairs, parse_constant=constant
    )


class RemoteTransport:
    def __init__(self, allowed_origins):
        self.allowed_origins = tuple(allowed_origins)

    async def request(
        self,
        url,
        route,
        *,
        method="GET",
        token,
        body=None,
        timeout=20,
        authorize_send=None,
    ):
        endpoint = Endpoint.parse(url, allowed_origins=self.allowed_origins)
        if method not in {"GET", "POST"} or not re.fullmatch(
            r"/(?:health|tasks(?:/[a-zA-Z0-9_-]{1,64}(?:/cancel)?)?)", route
        ):
            raise ValueError("Unsupported remote protocol route")
        if method == "POST" and authorize_send is None:
            raise ValueError("Remote writes require a dispatch authorization callback")
        expected_method = (
            "POST" if route == "/tasks" or route.endswith("/cancel") else "GET"
        )
        if method != expected_method or method == "GET" and body is not None:
            raise ValueError("Unsupported remote protocol method or body")
        if (
            not isinstance(token, str)
            or not 1 <= len(token) <= 8192
            or any(ord(c) < 33 or ord(c) > 126 for c in token)
        ):
            raise ValueError("Invalid remote credential")
        if isinstance(timeout, bool) or not 0 < timeout <= 60:
            raise ValueError("Remote request timeout must be in (0, 60] seconds")
        payload = (
            json.dumps(body, ensure_ascii=False, allow_nan=False).encode()
            if body is not None
            else None
        )
        if payload is not None and len(payload) > MAX_BODY_BYTES:
            raise PlatformError(
                422, "remote_request_too_large", "远程任务请求超过大小限制。"
            )
        deadline = time.monotonic() + timeout
        try:
            async with asyncio.timeout(timeout):
                addresses = await asyncio.to_thread(
                    public_addresses, endpoint.host, endpoint.port
                )
                return await asyncio.to_thread(
                    self._send,
                    endpoint,
                    addresses[0],
                    route,
                    method,
                    token,
                    payload,
                    deadline,
                    authorize_send,
                )
        except PlatformError:
            raise
        except (
            OSError,
            TimeoutError,
            ValueError,
            UnicodeError,
            RecursionError,
            http.client.HTTPException,
        ):
            # An exception is not proof that a remote POST did not execute.
            raise PlatformError(
                502,
                "remote_transport_failed",
                "远程 Agent 请求失败，不能据此判断是否已执行。",
            ) from None

    @staticmethod
    def _send(
        endpoint, address, route, method, token, payload, deadline, authorize_send
    ):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError()
        connection = PinnedHTTPSConnection(endpoint, address, remaining)
        try:
            connection.connect()
            if authorize_send:
                authorize_send()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError()
            connection.sock.settimeout(remaining)
            connection.request(
                method,
                endpoint.path + route,
                body=payload,
                headers={
                    "Host": endpoint.authority,
                    "Authorization": "Bearer " + token,
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
            )
            response = connection.getresponse()
            if not 200 <= response.status < 300:
                raise PlatformError(
                    502,
                    "remote_http_failure",
                    "远程 Agent 未返回有效成功响应；不自动重试或跟随跳转。",
                )
            if (
                response.getheader("Content-Type", "").split(";", 1)[0].strip().lower()
                != "application/json"
            ):
                raise ValueError("Expected JSON response")
            length = response.getheader("Content-Length")
            if length is not None and (
                not length.isdigit() or int(length) > MAX_BODY_BYTES
            ):
                raise ValueError("Invalid response size")
            chunks, size = [], 0
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError()
                if connection.sock:
                    connection.sock.settimeout(remaining)
                chunk = response.read(min(65536, MAX_BODY_BYTES + 1 - size))
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_BODY_BYTES:
                    raise ValueError("Remote response exceeds limit")
                chunks.append(chunk)
            return decode_json(b"".join(chunks))
        finally:
            connection.close()
