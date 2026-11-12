"""Bounded HTTPS JSON transport for provider model discovery with pinned DNS."""

import http.client
import ipaddress
import json
import re
import socket
import ssl
from dataclasses import dataclass
from urllib.parse import urlsplit

from agent_platform.infrastructure.errors import PlatformError

MAX_BODY_BYTES = 1024 * 1024


def invalid_endpoint():
    return PlatformError(
        422,
        "invalid_provider_endpoint",
        "服务商地址必须是已允许的 HTTPS 地址，不能包含凭据、查询参数或特殊路径。",
    )


@dataclass(frozen=True)
class Endpoint:
    host: str
    port: int
    authority: str
    path: str

    @property
    def origin(self):
        return f"https://{self.authority}"

    @classmethod
    def parse(cls, url):
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
            endpoint = cls(host, port, authority, path)
        except (ValueError, TypeError):
            raise invalid_endpoint() from None
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
                "provider_address_blocked",
                "服务商不能访问本地、内网或保留地址。",
            )
        if address.compressed not in addresses:
            addresses.append(address.compressed)
    if not addresses:
        raise PlatformError(502, "provider_dns_empty", "服务商地址解析失败。")
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
