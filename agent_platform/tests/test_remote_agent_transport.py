"""Remote descriptors cannot become a general URL fetch capability."""

import asyncio
import io
import socket
import unittest
from unittest.mock import Mock, patch

from agent_platform.infrastructure.errors import PlatformError
from agent_platform.modules.remote_agents.transport import (
    MAX_BODY_BYTES,
    Endpoint,
    PinnedHTTPSConnection,
    RemoteTransport,
    decode_json,
    public_addresses,
)

MODULE = "agent_platform.modules.remote_agents.transport"


class Response:
    def __init__(
        self,
        body=b'{"ok":true}',
        *,
        status=200,
        content_type="application/json",
        length=None,
    ):
        self.body = io.BytesIO(body)
        self.status = status
        self.headers = {"Content-Type": content_type}
        if length is not None:
            self.headers["Content-Length"] = length

    def getheader(self, name, default=None):
        return self.headers.get(name, default)

    def read(self, size):
        return self.body.read(size)


class RemoteTransportTests(unittest.TestCase):
    def test_only_exact_https_origins_and_plain_paths_are_accepted(self):
        endpoint = Endpoint.parse(
            "https://EXAMPLE.test:443/agent/v1/",
            allowed_origins=("https://example.test",),
        )
        self.assertEqual(endpoint.origin, "https://example.test")
        self.assertEqual(endpoint.path, "/agent/v1")
        for url in (
            "http://example.test",
            "https://name:secret@example.test",
            "https://example.test?secret=1",
            "https://example.test#fragment",
            "https://example.test/%2e%2e/admin",
            "https://example.test/../admin",
            "https://example.test/a//b",
            "https://example.test\\@localhost",
            "https://example.test:0",
            "https://example.test:",
            "https://example.test:99999",
            "https://example.test\n",
            "https://example.test.evil/path",
            "https://example.test:8443/agent",
            "https://-example.test",
        ):
            with self.subTest(url=url), self.assertRaises(PlatformError):
                Endpoint.parse(url, allowed_origins=("https://example.test",))
        with self.assertRaises(PlatformError):
            Endpoint.parse("https://example.test", allowed_origins=())

    def test_dns_answers_reject_private_reserved_mixed_and_transition_addresses(self):
        for address in (
            "127.0.0.1",
            "10.0.0.1",
            "169.254.169.254",
            "192.168.1.1",
            "0.0.0.0",
            "224.0.0.1",
            "::1",
            "fc00::1",
            "::ffff:127.0.0.1",
            "2002:7f00:1::",
            "64:ff9b::7f00:1",
            "fec0::1",
        ):
            answer = [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
                (
                    socket.AF_INET6 if ":" in address else socket.AF_INET,
                    socket.SOCK_STREAM,
                    6,
                    "",
                    (address, 443),
                ),
            ]
            with (
                self.subTest(address=address),
                patch(MODULE + ".socket.getaddrinfo", return_value=answer),
                self.assertRaises(PlatformError),
            ):
                public_addresses("agent.example.test", 443)

    def test_https_connect_pins_ip_and_preserves_sni_and_certificate_name(self):
        endpoint = Endpoint.parse("https://agent.example.test:8443/agent")
        raw, secured, tls = Mock(), Mock(), Mock()
        tls.wrap_socket.return_value = secured
        with (
            patch(MODULE + ".ssl.create_default_context", return_value=tls),
            patch(MODULE + ".socket.create_connection", return_value=raw) as connect,
        ):
            conn = PinnedHTTPSConnection(endpoint, "8.8.8.8", 2)
            conn.connect()
            connect.assert_called_once_with(("8.8.8.8", 8443), 2)
            tls.wrap_socket.assert_called_once_with(
                raw, server_hostname="agent.example.test"
            )
            self.assertIs(conn.sock, secured)

    def request(
        self, response, *, method="GET", route="/health", body=None, authorize=None
    ):
        connection = Mock()
        connection.getresponse.return_value = response
        transport = RemoteTransport(("https://agent.example.test",))
        with (
            patch(MODULE + ".public_addresses", return_value=["8.8.8.8"]) as resolve,
            patch(
                MODULE + ".PinnedHTTPSConnection", return_value=connection
            ) as constructor,
        ):
            result = asyncio.run(
                transport.request(
                    "https://agent.example.test/base",
                    route,
                    method=method,
                    token="temporary-test-credential",
                    body=body,
                    authorize_send=authorize,
                )
            )
            resolve.assert_called_once_with("agent.example.test", 443)
            self.assertEqual(constructor.call_args.args[1], "8.8.8.8")
        return result, connection

    def test_authorized_post_is_sent_once_with_pinned_address(self):
        authorized = Mock()
        result, connection = self.request(
            Response(),
            method="POST",
            route="/tasks",
            body={"request_id": "stable-id"},
            authorize=authorized,
        )
        self.assertEqual(result, {"ok": True})
        authorized.assert_called_once()
        connection.connect.assert_called_once()
        connection.request.assert_called_once()
        self.assertEqual(connection.request.call_args.args, ("POST", "/base/tasks"))
        headers = connection.request.call_args.kwargs["headers"]
        self.assertEqual(headers["Host"], "agent.example.test")
        self.assertEqual(headers["Authorization"], "Bearer temporary-test-credential")
        connection.close.assert_called_once()

    def test_authorization_revoked_after_connect_prevents_post(self):
        connection = Mock()
        with (
            patch(MODULE + ".public_addresses", return_value=["8.8.8.8"]),
            patch(MODULE + ".PinnedHTTPSConnection", return_value=connection),
            self.assertRaises(PlatformError),
        ):
            asyncio.run(
                RemoteTransport(("https://agent.example.test",)).request(
                    "https://agent.example.test",
                    "/tasks",
                    method="POST",
                    token="test",
                    body={},
                    authorize_send=Mock(
                        side_effect=PlatformError(403, "revoked", "Revoked")
                    ),
                )
            )
        connection.connect.assert_called_once()
        connection.request.assert_not_called()
        connection.close.assert_called_once()

    def test_redirects_invalid_content_duplicates_and_oversized_responses_fail_closed(
        self,
    ):
        for response in (
            Response(status=302),
            Response(status=429),
            Response(content_type="text/html"),
            Response(length=str(MAX_BODY_BYTES + 1)),
            Response(b"x" * (MAX_BODY_BYTES + 1)),
            Response(b'{"status":"ok","status":"other"}'),
            Response(b'{"value":NaN}'),
            Response(b"not json"),
            Response(b"\xff"),
        ):
            with self.subTest(response=response), self.assertRaises(PlatformError):
                self.request(response)
        with self.assertRaises(ValueError):
            decode_json(b'{"nested":{"x":1,"x":2}}')

    def test_unsupported_routes_methods_and_unmetered_writes_do_not_resolve(self):
        transport = RemoteTransport(("https://agent.example.test",))
        for route, method, body, token, authorize in (
            ("/tasks", "POST", {}, "test", None),
            ("/tasks/../admin", "GET", None, "test", None),
            ("/health", "POST", None, "test", lambda: None),
            ("/tasks/id/cancel", "GET", None, "test", None),
            ("/health", "GET", {}, "test", None),
            ("/health", "GET", None, "test\r\nHost: evil", None),
        ):
            with (
                self.subTest(route=route, method=method),
                patch(MODULE + ".public_addresses") as resolve,
                self.assertRaises(ValueError),
            ):
                asyncio.run(
                    transport.request(
                        "https://agent.example.test",
                        route,
                        method=method,
                        token=token,
                        body=body,
                        authorize_send=authorize,
                    )
                )
            resolve.assert_not_called()
