"""Disposable HTTPS peer; production still rejects loopback and requires system trust."""

import json
import socket
import ssl
import threading
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from agent_platform.modules.runtime.contracts import canonical_digest


@contextmanager
def https_peer():
    with TemporaryDirectory(prefix="remote-https-test-") as directory:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name(
            [x509.NameAttribute(NameOID.COMMON_NAME, "remote.example.test")]
        )
        certificate = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(datetime.now(UTC) - timedelta(minutes=1))
            .not_valid_after(datetime.now(UTC) + timedelta(hours=1))
            .add_extension(
                x509.SubjectAlternativeName([x509.DNSName("remote.example.test")]),
                critical=False,
            )
            .add_extension(
                x509.BasicConstraints(ca=True, path_length=None), critical=True
            )
            .sign(key, hashes.SHA256())
        )
        cert = Path(directory) / "certificate.pem"
        private = Path(directory) / "private.pem"
        cert.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
        private.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        private.chmod(0o600)
        server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_context.load_cert_chain(cert, private)
        client_context = ssl.create_default_context(cafile=str(cert))
        state = {
            "descriptor": None,
            "requests": [],
            "submissions": [],
            "sni": [],
            "lose_post": True,
            "status_hook": None,
            "cancelled": False,
        }
        server_context.set_servername_callback(
            lambda sock, hostname, context: state["sni"].append(hostname)
        )

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def answer(self, value):
                payload = json.dumps(value).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def status(self):
                request = state["submissions"][-1]
                value = {"value": "from HTTPS peer"}
                payload = {
                    "request_id": request["request_id"],
                    "state": "succeeded",
                    "result": {"value": value, "digest": canonical_digest(value)},
                    "fee": {
                        "receipt_id": "https-fee",
                        "request_id": request["request_id"],
                        "quote_id": request["quote_id"],
                        "amount": "0.01",
                        "final": True,
                    },
                }
                if state["cancelled"]:
                    payload.pop("result")
                    payload["state"] = "cancelled"
                    return payload
                return (
                    state["status_hook"](payload) if state["status_hook"] else payload
                )

            def do_GET(self):
                state["requests"].append(("GET", self.path))
                if (
                    self.headers.get("Authorization")
                    != "Bearer temporary-remote-test-secret"
                ):
                    self.send_error(401)
                    return
                if self.path.endswith("/health"):
                    descriptor = state["descriptor"]
                    self.answer(
                        {
                            "remote_agent_id": descriptor.remote_agent_id,
                            "remote_version": descriptor.remote_version,
                            "descriptor_digest": descriptor.digest,
                            "healthy": True,
                        }
                    )
                elif state["submissions"] and self.path.endswith(
                    "/tasks/" + state["submissions"][-1]["request_id"]
                ):
                    self.answer(self.status())
                else:
                    self.send_error(404)

            def do_POST(self):
                state["requests"].append(("POST", self.path))
                if (
                    self.headers.get("Authorization")
                    != "Bearer temporary-remote-test-secret"
                ):
                    self.send_error(403)
                    return
                request = json.loads(
                    self.rfile.read(int(self.headers["Content-Length"]))
                )
                if state["submissions"] and self.path.endswith(
                    "/tasks/" + state["submissions"][-1]["request_id"] + "/cancel"
                ):
                    if request != {
                        "request_id": state["submissions"][-1]["request_id"]
                    }:
                        self.send_error(400)
                        return
                    state["cancelled"] = True
                    self.answer(self.status())
                    return
                if not self.path.endswith("/tasks"):
                    self.send_error(404)
                    return
                state["submissions"].append(request)
                if state["lose_post"]:
                    state["lose_post"] = False
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                    return
                self.answer(self.status())

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.socket = server_context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        state["origin"] = f"https://remote.example.test:{server.server_port}"
        try:
            # Only the test substitutes resolution and trusts this temporary CA.
            # TLS, SNI, hostname verification, request bytes and response-loss
            # behavior all pass through the actual production transport.
            with (
                patch(
                    "agent_platform.modules.remote_agents.transport.public_addresses",
                    return_value=["127.0.0.1"],
                ),
                patch(
                    "agent_platform.modules.remote_agents.transport.ssl.create_default_context",
                    return_value=client_context,
                ),
            ):
                yield state
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
