"""Test pinned LiteLLM + PostgreSQL + Redis with a local, free provider fixture.

Run from the repository root with uv. Only a unique throwaway Compose project
and its own volumes are created and removed. No real provider credentials are
read, no existing deployment is touched, and secrets are not printed.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import time
import uuid
from decimal import Decimal
from pathlib import Path

import httpx
from cryptography.fernet import Fernet

from agent_platform.modules.runtime.engine import execute_agent
from agent_platform.modules.runtime.gateway import (
    Gateway,
    GatewayError,
    UsageUnavailable,
)

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "agent_platform/deploy"


def command(args: list[str], **kwargs) -> str:
    if args[0] == "docker":
        # Shell variables override Compose --env-file values. Exclude any real
        # deployment credentials and force this run's generated test settings.
        process_env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(
                (
                    "PLATFORM_",
                    "LITELLM_",
                    "UPSTREAM_",
                    "GATEWAY_",
                    "POSTGRES_",
                    "COMPOSE_",
                    "UI_",
                    "OPENAI_",
                    "ANTHROPIC_",
                    "DOCKER_",
                )
            )
        }
        if args[:2] == ["docker", "compose"]:
            env_file = Path(args[args.index("--env-file") + 1])
            process_env.update(
                line.split("=", 1) for line in env_file.read_text().splitlines()
            )
        kwargs["env"] = process_env
    result = subprocess.run(args, capture_output=True, text=True, check=False, **kwargs)
    if result.returncode:
        # Do not include env-sensitive compose configuration or provider logs.
        details = (
            re.sub(r"://[^/@]+:[^/@]+@", "://***@", result.stderr)
            if "alembic" in args
            else ""
        )
        raise RuntimeError(
            f"Command failed ({result.returncode}): {' '.join(args[:3])}\n{details}"
        )
    return result.stdout.strip()


async def verify_gateway(base: str, key: str) -> None:
    gateway = Gateway(base + "/v1", key)
    events = [
        event
        async for event in gateway.stream(
            [{"role": "user", "content": "hello"}], "platform-chat", 64, 0, []
        )
    ]
    result = events[-1].data
    assert result.content == "Answer: 4"
    assert (result.input_tokens, result.output_tokens) == (12, 4)
    print("PASS: actual LiteLLM streaming text and complete usage", flush=True)
    try:
        _ = [
            event
            async for event in gateway.stream(
                [{"role": "user", "content": "missing-usage-test"}],
                "platform-chat",
                64,
                0,
                [],
            )
        ]
    except UsageUnavailable:
        pass
    else:
        raise AssertionError("Responses without usage must remain unsettled")
    print("PASS: native Responses without usage remain unsettled", flush=True)

    async def emit(_name: str, _data: dict) -> None:
        pass

    async def cancelled() -> bool:
        return False

    async def invoke(messages: list[dict], tools: list[dict]):
        result = None
        async for event in gateway.stream(messages, "platform-chat", 64, 0, tools):
            if event.type == "result":
                result = event.data
        assert result is not None
        return result

    answer = await execute_agent(
        {"max_steps": 3, "tools": ["calculator"]},
        [{"role": "user", "content": "calculate 2+2"}],
        invoke,
        emit,
        cancelled,
    )
    assert answer == "Answer: 4"
    print("PASS: actual LiteLLM tool fragments through native LangGraph v3", flush=True)

    async with httpx.AsyncClient(headers={"Authorization": "Bearer " + key}) as client:
        denied = await client.post(
            base + "/key/generate", json={"models": ["platform-chat"]}
        )
        assert denied.status_code in {401, 403}, denied.status_code
        denied = await client.post(
            base + "/v1/responses",
            json={
                "model": "forbidden-alias",
                "input": "hello",
            },
        )
        assert denied.status_code in {401, 403}, denied.status_code
        denied = await client.post(
            base + "/v1/chat/completions",
            json={
                "model": "platform-chat",
                "messages": [{"role": "user", "content": "hello"}],
            },
        )
        assert denied.status_code in {401, 403}, denied.status_code
    print(
        "PASS: runtime virtual key rejects management, unlisted model and Chat Completions",
        flush=True,
    )
    try:
        _ = [
            event
            async for event in gateway.stream(
                [{"role": "user", "content": "retry-test"}], "platform-chat", 64, 0, []
            )
        ]
    except GatewayError:
        pass
    else:
        raise AssertionError("Expected failed provider request")


def verify_platform(base: str, password: str) -> None:
    """Exercise current v2 grants/cost APIs; the container Worker owns execution."""
    with httpx.Client(base_url=base, timeout=45) as client:
        assert client.get("/api/v1/ready").status_code == 200
        login = client.post(
            "/api/v1/auth/login",
            json={"email": "admin@example.com", "password": password},
        )
        login.raise_for_status()
        actor = login.json()["user"]
        assert {"platform_admin", "platform_finance"} <= set(actor["platform_roles"])
        client.headers["X-CSRF-Token"] = login.json()["csrf_token"]
        # Containers keep SaaS Secure cookies. This loopback-only fixture has no
        # TLS proxy, so explicitly send its freshly issued test cookie over HTTP.
        cookie = client.cookies.get("agent_platform_session")
        assert cookie and "secure" in login.headers["set-cookie"].lower()
        client.headers["Cookie"] = "agent_platform_session=" + cookie
        memberships = actor["memberships"]
        assert memberships
        tenant_id = memberships[0]["tenant_id"]
        tenant_path = "/api/v2/tenants/" + tenant_id
        platform_path = "/api/v2/platform/tenants/" + tenant_id

        def api(method: str, path: str, **kwargs):
            response = client.request(method, path, **kwargs)
            response.raise_for_status()
            return response.json()

        wallet = api("GET", tenant_path + "/billing/wallet")
        assert Decimal(wallet["balance"]) == 0
        deployment = api(
            "POST",
            "/api/v2/platform/deployments",
            json={
                "name": "Free smoke deployment",
                "internal_route": "platform-chat",
                "base_url": "http://litellm:4000/v1",
                "capabilities": ["chat", "tools"],
            },
        )
        # Customer prices and supplier prices are configured independently.
        api(
            "PUT",
            "/api/v2/platform/costs/budgets/platform/platform",
            json={"limit": "10", "expected_version": 0},
        )
        api(
            "PUT",
            "/api/v2/platform/costs/budgets/deployment/" + deployment["id"],
            json={"limit": "5", "expected_version": 0},
        )
        api(
            "POST",
            "/api/v2/platform/deployments/" + deployment["id"] + "/cost-prices",
            json={"input_price": "3", "output_price": "4", "expected_version": 0},
        )
        model = api(
            "POST",
            platform_path + "/models",
            json={
                "deployment_id": deployment["id"],
                "name": "Smoke model",
                "alias": "smoke-customer-alias",
                "input_price": "1",
                "output_price": "2",
                "context_window": 32768,
                "max_output_tokens": 256,
            },
        )
        initial_grants = api("GET", platform_path + "/model-grants")
        assert initial_grants["gateway"]["status"] == "not_enrolled"
        api("POST", platform_path + "/gateway/enroll")
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            grants = api("GET", platform_path + "/model-grants")
            ready = next(row for row in grants["items"] if row["id"] == model["id"])
            if ready["sync_status"] == "ready":
                assert (
                    grants["gateway"]["applied_version"]
                    == grants["gateway"]["desired_version"]
                )
                break
            time.sleep(0.2)
        else:
            raise AssertionError("Tenant gateway grant did not converge")
        agent = api(
            "POST",
            tenant_path + "/agents",
            json={
                "name": "Smoke Agent",
                "model_id": model["id"],
                "system_prompt": "Use the calculator to answer.",
                "max_tokens": 64,
                "max_steps": 3,
                "tools": ["calculator"],
            },
        )
        assert (
            api("POST", tenant_path + f"/agents/{agent['id']}/publish")["version"] == 1
        )
        api(
            "POST",
            platform_path + "/credits",
            headers={"Idempotency-Key": "smoke-credit"},
            json={"amount": "10", "description": "Free container smoke credit"},
        )
        session = api("POST", tenant_path + "/sessions", json={"agent_id": agent["id"]})
        run = api(
            "POST",
            tenant_path + f"/sessions/{session['id']}/runs",
            headers={"Idempotency-Key": "smoke-run"},
            json={"message": "calculate 2+2"},
        )
        stream = client.get(tenant_path + f"/runs/{run['id']}/events")
        stream.raise_for_status()
        assert "event: tool.finished" in stream.text
        assert "event: run.completed" in stream.text
        assert "Answer: 4" in stream.text
        event_ids = [
            int(line[4:])
            for line in stream.text.splitlines()
            if line.startswith("id: ")
        ]
        assert event_ids == list(range(1, len(event_ids) + 1))
        replay = client.get(
            tenant_path + f"/runs/{run['id']}/events",
            headers={"Last-Event-ID": str(event_ids[-2])},
        )
        replay.raise_for_status()
        assert (
            "event: run.completed" in replay.text
            and "event: run.queued" not in replay.text
        )
        saved = api("GET", tenant_path + f"/runs/{run['id']}")
        assert saved["status"] == "succeeded"
        assert Decimal(saved["cost"]) == Decimal("0.000040")
        usage = api("GET", tenant_path + "/usage/calls")["items"]
        assert len(usage) == 2
        assert all(
            item["status"] == "confirmed"
            and item["input_tokens"] == 12
            and item["output_tokens"] == 4
            and item["usage_source"] == "litellm_gateway"
            for item in usage
        )
        for call in usage:
            receipts = api(
                "GET", platform_path + f"/billing/calls/{call['id']}/receipts"
            )["items"]
            assert len(receipts) == 1 and receipts[0]["attempt_id"]
            assert (
                receipts[0]["input_tokens"] == 12 and receipts[0]["output_tokens"] == 4
            )
        wallet = api("GET", tenant_path + "/billing/wallet")
        assert (
            Decimal(wallet["balance"]) == Decimal("9.999960")
            and Decimal(wallet["reserved"]) == 0
        )
        ledger = api("GET", tenant_path + "/billing/ledger")["items"]
        assert len(ledger) == 3
        detail = api("GET", tenant_path + f"/sessions/{session['id']}")
        assert [message["role"] for message in detail["messages"]] == [
            "user",
            "assistant",
        ]
        assert detail["messages"][-1]["content"] == "Answer: 4"
        dashboard = api("GET", tenant_path + "/dashboard")
        assert dashboard["requests"] == 2 and dashboard["tokens"] == 32
        assert Decimal(dashboard["cost"]) == Decimal("0.000040")
        costs = api("GET", "/api/v2/platform/costs")
        assert len(costs["budgets"]) == 2
        assert all(
            Decimal(row["spent"]) == Decimal("0.000104")
            and Decimal(row["reserved"]) == 0
            and not row["blocked"]
            for row in costs["budgets"]
        )
        diagnostics = api("GET", platform_path + "/diagnostics")
        assert diagnostics["queue"]["count"] == 0
        assert diagnostics["unresolved"]["count"] == 0
        assert diagnostics["supplier_frozen"]["count"] == 0
    print(
        "PASS: independent API/Worker/gateway-sync roles, v2 grant → enroll → tool/SSE → immutable usage → customer/supplier accounting",
        flush=True,
    )


def verify_runtime_roles(compose: list[str]) -> None:
    for service, role in (
        ("api", "platform_api"),
        ("worker", "platform_worker"),
        ("gateway-sync", "platform_gateway_sync"),
        ("maintenance", "platform_maintenance"),
    ):
        code = f"""
import os
from urllib.parse import urlsplit
from agent_platform.infrastructure.config import Settings
from agent_platform.infrastructure.db import Database
settings = Settings.from_env(role={service!r})
assert settings.mode == 'saas' and not settings.embedded_worker
assert urlsplit(settings.database_url).username == {role!r}
assert not settings.litellm_key
for variable in ('LITELLM_MASTER_KEY', 'UPSTREAM_API_KEY', 'OPENAI_API_KEY',
                 'PLATFORM_ADMIN_PASSWORD', 'POSTGRES_PASSWORD',
                 'PLATFORM_MIGRATION_DB_PASSWORD'):
    assert variable not in os.environ
if {service!r} != 'gateway-sync':
    assert 'PLATFORM_GATEWAY_CONTROL_KEY' not in os.environ
else:
    assert settings.gateway_control_key
db = Database(settings.database_url)
try:
    db.assert_schema(saas=True)
finally:
    db.close()
print('isolated')
"""
        assert (
            command([*compose, "exec", "-T", service, "python", "-c", code])
            == "isolated"
        )
    print(
        "PASS: each service uses its own non-owner/RLS database identity; API/Worker receive no gateway master or legacy runtime key",
        flush=True,
    )


def main() -> None:
    transport = command(
        ["docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"]
    )
    if not transport.startswith(("unix://", "npipe://")):
        raise RuntimeError(
            "Smoke requires a local Docker socket, not a remote Docker context"
        )
    project = "agent-platform-smoke-" + uuid.uuid4().hex[:10]
    image = project + ":local"
    with tempfile.TemporaryDirectory(prefix=project) as directory:
        root = Path(directory)
        bootstrap = root / "bootstrap"
        bootstrap.mkdir()
        passwords = {
            name: secrets.token_hex(24)
            for name in [
                "POSTGRES_PASSWORD",
                "PLATFORM_API_DB_PASSWORD",
                "PLATFORM_WORKER_DB_PASSWORD",
                "PLATFORM_GATEWAY_SYNC_DB_PASSWORD",
                "PLATFORM_MAINTENANCE_DB_PASSWORD",
                "PLATFORM_MIGRATION_DB_PASSWORD",
                "LITELLM_DB_PASSWORD",
                "LITELLM_SALT_KEY",
                "PLATFORM_ADMIN_PASSWORD",
                "UI_PASSWORD",
            ]
        }
        env = {
            **passwords,
            "LITELLM_MASTER_KEY": "sk-" + secrets.token_hex(24),
            "UPSTREAM_MODEL": "openai/gpt-4o-mini",
            "UPSTREAM_API_BASE": "http://mock:8080/v1",
            "UPSTREAM_API_KEY": "smoke-only",
            "LITELLM_MODEL_ALIAS": "platform-chat",
            "PLATFORM_HTTP_PORT": "0",
            "LITELLM_HTTP_PORT": "0",
            "GATEWAY_BOOTSTRAP_DIR": str(bootstrap),
            "PLATFORM_SECRET_ENCRYPTION_KEY": Fernet.generate_key().decode(),
            "PLATFORM_OPERATOR_EMAILS": "admin@example.com",
            "PLATFORM_TOMBSTONE_VOLUME": project + "-tombstones",
        }
        env_file = root / ".env"
        env_file.write_text("".join(f"{key}={value}\n" for key, value in env.items()))
        env_file.chmod(0o600)
        override = root / "smoke.json"
        override.write_text(
            json.dumps(
                {
                    "services": {
                        **{
                            role: {"image": image}
                            for role in (
                                "api",
                                "worker",
                                "init",
                                "gateway-sync",
                                "maintenance",
                            )
                        },
                        "postgres": {"ports": ["127.0.0.1::5432"]},
                        "mock": {
                            "image": "python:3.13-slim-bookworm",
                            "command": ["python", "/mock.py"],
                            "volumes": [f"{DEPLOY / 'mock_provider.py'}:/mock.py:ro"],
                        },
                    },
                    "volumes": {
                        "tenant-tombstones": {
                            "external": False,
                            "name": env["PLATFORM_TOMBSTONE_VOLUME"],
                        }
                    },
                }
            )
        )
        compose = [
            "docker",
            "compose",
            "--project-name",
            project,
            "--env-file",
            str(env_file),
            "-f",
            str(DEPLOY / "compose.yml"),
            "-f",
            str(override),
        ]
        image_built = False
        try:
            command([*compose, "config", "--quiet"])
            print(
                "Starting isolated PostgreSQL, Redis, pinned LiteLLM and local mock…",
                flush=True,
            )
            command(
                [
                    *compose,
                    "up",
                    "-d",
                    "--wait",
                    "--wait-timeout",
                    "240",
                    "postgres",
                    "redis",
                    "mock",
                    "litellm",
                ],
                timeout=300,
            )
            version = command(
                [
                    *compose,
                    "exec",
                    "-T",
                    "litellm",
                    "python",
                    "-c",
                    "import importlib.metadata; print(importlib.metadata.version('litellm'))",
                ]
            )
            assert version == "1.83.0", version
            print("PASS: pinned container runs LiteLLM 1.83.0", flush=True)
            command(
                [
                    *compose,
                    "exec",
                    "-T",
                    "-e",
                    f"GATEWAY_KEY_UID={os.getuid()}",
                    "-e",
                    f"GATEWAY_KEY_GID={os.getgid()}",
                    "litellm",
                    "python",
                    "/app/platform-bootstrap-key.py",
                ]
            )
            key = (bootstrap / ".env.gateway").read_text().strip().split("=", 1)[1]
            litellm_address = command([*compose, "port", "litellm", "4000"])
            asyncio.run(verify_gateway("http://" + litellm_address, key))
            counts = json.loads(
                command(
                    [
                        *compose,
                        "exec",
                        "-T",
                        "litellm",
                        "python",
                        "-c",
                        "import urllib.request; print(urllib.request.urlopen('http://mock:8080/stats').read().decode())",
                    ]
                )
            )
            assert counts["retry_test"] == 1, counts
            print(
                "PASS: provider 500 receives exactly one attempt (SDK and Proxy retries disabled)",
                flush=True,
            )

            address = command([*compose, "port", "postgres", "5432"])
            db_env = {
                key: value
                for key, value in os.environ.items()
                if not key.startswith(
                    (
                        "PLATFORM_",
                        "LITELLM_",
                        "UPSTREAM_",
                        "POSTGRES_",
                        "OPENAI_",
                        "ANTHROPIC_",
                    )
                )
            }
            db_env["PLATFORM_DATABASE_URL"] = (
                f"postgresql+psycopg://platform_migrator:{passwords['PLATFORM_MIGRATION_DB_PASSWORD']}@{address}/platform"
            )
            for action in [
                ("upgrade", "head"),
                ("check",),
                ("downgrade", "base"),
                ("upgrade", "head"),
                ("check",),
            ]:
                command(
                    [
                        sys.executable,
                        "-m",
                        "alembic",
                        "-c",
                        str(ROOT / "agent_platform/alembic.ini"),
                        *action,
                    ],
                    env=db_env,
                    cwd=ROOT,
                )
            print(
                "PASS: PostgreSQL migration upgrade/check/downgrade/re-upgrade",
                flush=True,
            )
            print(
                "Building isolated backend image from root uv.lock…",
                flush=True,
            )
            command(
                [
                    "docker",
                    "build",
                    "-f",
                    str(DEPLOY / "Dockerfile"),
                    "-t",
                    image,
                    str(ROOT),
                ],
                timeout=600,
            )
            image_built = True
            command(
                [
                    *compose,
                    "up",
                    "-d",
                    "--no-build",
                    "--wait",
                    "--wait-timeout",
                    "120",
                    "api",
                    "worker",
                    "gateway-sync",
                    "maintenance",
                ],
                timeout=180,
            )
            verify_runtime_roles(compose)
            api_address = command([*compose, "port", "api", "8000"])
            verify_platform(
                "http://" + api_address, passwords["PLATFORM_ADMIN_PASSWORD"]
            )
        finally:
            command([*compose, "down", "--volumes", "--remove-orphans"], timeout=120)
            if image_built:
                command(["docker", "image", "rm", image], timeout=30)


if __name__ == "__main__":
    main()
