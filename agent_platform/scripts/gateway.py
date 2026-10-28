"""Private, persistent LiteLLM infrastructure for the local platform supervisor."""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = ROOT / "agent_platform/deploy/compose.yml"
SERVICES = ("postgres", "redis", "litellm")
CONTROL_SECRETS = (
    "POSTGRES_PASSWORD",
    "PLATFORM_DB_PASSWORD",
    "LITELLM_DB_PASSWORD",
    "LITELLM_MASTER_KEY",
    "LITELLM_SALT_KEY",
    "UI_USERNAME",
    "UI_PASSWORD",
)

SERVICE_DATABASE_SECRETS = (
    "PLATFORM_API_DB_PASSWORD",
    "PLATFORM_WORKER_DB_PASSWORD",
    "PLATFORM_GATEWAY_SYNC_DB_PASSWORD",
    "PLATFORM_MAINTENANCE_DB_PASSWORD",
    "PLATFORM_MIGRATION_DB_PASSWORD",
)
SERVICE_DATABASE_URLS = (
    "PLATFORM_API_DATABASE_URL",
    "PLATFORM_WORKER_DATABASE_URL",
    "PLATFORM_GATEWAY_SYNC_DATABASE_URL",
    "PLATFORM_MAINTENANCE_DATABASE_URL",
    "PLATFORM_RUNTIME_DATABASE_URL",
)


def platform_environment(env: dict[str, str]) -> dict[str, str]:
    """Allow only application settings and essential OS runtime configuration."""
    excluded = {
        *CONTROL_SECRETS,
        *SERVICE_DATABASE_SECRETS,
        *SERVICE_DATABASE_URLS,
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "UPSTREAM_API_KEY",
        "UPSTREAM_API_BASE",
        "UPSTREAM_MODEL",
        "PLATFORM_GATEWAY_CONTROL_KEY",
        "PLATFORM_MIGRATION_DB_PASSWORD",
        "PLATFORM_MIGRATION_DATABASE_URL",
        "PLATFORM_ADMIN_PASSWORD",
    }
    operating_system = {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "LANG",
        "TZ",
        "TMPDIR",
        "TMP",
        "TEMP",
        "VIRTUAL_ENV",
        "PYTHONPATH",
        "PYTHONUNBUFFERED",
        "PYTHONDONTWRITEBYTECODE",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
    }
    return {
        key: value
        for key, value in env.items()
        if key not in excluded
        and (key.startswith(("PLATFORM_", "LC_")) or key in operating_system)
    }


class LocalGateway:
    def __init__(self, state_dir: Path, env: dict[str, str], port: int):
        if not 1 <= port <= 65535:
            raise RuntimeError("LITELLM_PORT 必须在 1 到 65535 之间。")
        self.directory = state_dir.resolve() / "gateway"
        self.control_file = self.directory / ".env.control"
        self.bootstrap_directory = self.directory / "bootstrap"
        self.key_file = self.bootstrap_directory / ".env.gateway"
        self.legacy_key_file = self.directory / ".env.gateway"
        self.project = (
            "agent-platform-local-"
            + hashlib.sha256(str(state_dir.resolve()).encode()).hexdigest()[:12]
        )
        self.env = env
        self.port = port
        self.started = False
        self.compose_env: dict[str, str] = {}

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def command(self) -> list[str]:
        return [
            "docker",
            "compose",
            "--project-name",
            self.project,
            "--env-file",
            str(self.control_file),
            "-f",
            str(COMPOSE_FILE),
        ]

    def prepare(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.bootstrap_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        legacy_key_file = self.legacy_key_file
        if legacy_key_file.is_file() and not self.key_file.exists():
            # Preserve the existing restricted inference key, while keeping the
            # control-password directory outside LiteLLM's bootstrap mount.
            fd = os.open(self.key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as target:
                target.write(legacy_key_file.read_bytes())
        if not self.control_file.exists():
            controls = {
                name: self.env.get(name) or secrets.token_hex(24)
                for name in CONTROL_SECRETS
            }
            controls["LITELLM_MASTER_KEY"] = self.env.get(
                "LITELLM_MASTER_KEY"
            ) or "sk-" + secrets.token_hex(32)
            controls["UI_USERNAME"] = self.env.get("UI_USERNAME") or "admin"
            # Quote dotenv strings; credentials are configuration, never shell code.
            lines = []
            for name, value in controls.items():
                escaped = value.replace("\\", "\\\\").replace("'", "\\'")
                lines.append(f"{name}='{escaped}'\n")
            fd = os.open(self.control_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as target:
                target.writelines(lines)
        controls = {
            key: value
            for key, value in dotenv_values(self.control_file).items()
            if value is not None
        }
        if any(not controls.get(name) for name in CONTROL_SECRETS):
            raise RuntimeError(
                "本地网关控制配置不完整，请检查 .env.control；不会覆盖已有数据库凭据。"
            )
        # Add new service credentials without rotating any existing PostgreSQL
        # password. An old local volume still needs explicit DBA provisioning
        # before it can be used as a SaaS platform database.
        additions = {
            name: self.env.get(name) or secrets.token_hex(24)
            for name in SERVICE_DATABASE_SECRETS
            if not controls.get(name)
        }
        if additions:
            with self.control_file.open("a") as target:
                for name, value in additions.items():
                    escaped = value.replace("\\", "\\\\").replace("'", "\\'")
                    target.write(f"{name}='{escaped}'\n")
            controls.update(additions)
        if controls["UI_PASSWORD"] == controls["LITELLM_MASTER_KEY"]:
            raise RuntimeError("LiteLLM 管理页密码必须独立于网关 master key。")
        self.control_file.chmod(0o600)
        alias = (
            self.env.get("PLATFORM_DEFAULT_MODEL")
            or self.env.get("MODEL")
            or "platform-chat"
        )
        explicit_upstream = bool(self.env.get("UPSTREAM_API_KEY"))
        upstream_model = (
            self.env.get("UPSTREAM_MODEL", "")
            if explicit_upstream
            else "openai/" + (self.env.get("MODEL") or alias)
        )
        self.compose_env = {
            **os.environ,
            **self.env,
            **controls,
            "COMPOSE_ANSI": "never",
            "COMPOSE_PROGRESS": "plain",
            "GATEWAY_BOOTSTRAP_DIR": str(self.bootstrap_directory),
            "LITELLM_HTTP_PORT": str(self.port),
            "LITELLM_MODEL_ALIAS": alias,
            "UPSTREAM_MODEL": upstream_model,
            "UPSTREAM_API_BASE": (
                self.env.get("UPSTREAM_API_BASE", "")
                if explicit_upstream
                else self.env.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
            ),
            "UPSTREAM_API_KEY": (
                self.env.get("UPSTREAM_API_KEY", "")
                if explicit_upstream
                else self.env.get("OPENAI_API_KEY", "")
            ),
            # Local mode only starts infrastructure containers. These values
            # satisfy Compose's full-file interpolation without exposing them to
            # the API or Worker. Existing local PostgreSQL volumes are preserved.
            "PLATFORM_TOMBSTONE_VOLUME": f"{self.project}-tenant-tombstones",
        }

    def start(
        self,
        run: Callable[[list[str], dict[str, str]], None],
        check_port: Callable[[str, int], None],
    ) -> dict[str, str]:
        if not shutil.which("docker"):
            raise RuntimeError(
                "启动 LiteLLM 管理页需要 Docker。请启动 Docker 后重试；已有外部网关可使用 GATEWAY=external。"
            )
        self.prepare()
        available = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
        if available.returncode:
            raise RuntimeError("Docker 尚未就绪，请启动 Docker 后再次运行 make start。")
        owned = subprocess.run(
            [*self.command, "ps", "--quiet", "litellm"],
            env=self.compose_env,
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
        if owned.returncode:
            raise RuntimeError("无法读取本地网关容器状态，请检查 Docker Compose 配置。")
        same_port = False
        if owned.stdout.strip():
            ports = subprocess.run(
                [*self.command, "port", "litellm", "4000"],
                env=self.compose_env,
                capture_output=True,
                text=True,
                check=False,
                timeout=15,
            )
            same_port = ports.stdout.strip() == f"127.0.0.1:{self.port}"
        if not same_port:
            try:
                check_port("127.0.0.1", self.port)
            except RuntimeError:
                raise RuntimeError(
                    f"LiteLLM 端口 {self.port} 已被占用，请用 LITELLM_PORT=4001 指定其他端口。"
                ) from None
        # Only this deterministic Compose project is managed, including cleanup
        # after a partial start. Persistent database volumes are never removed.
        self.started = True
        # Compose validates external volume declarations even for infrastructure
        # selections. The local project owns its own durable, isolated name.
        run(
            [
                "docker",
                "volume",
                "create",
                self.compose_env["PLATFORM_TOMBSTONE_VOLUME"],
            ],
            self.compose_env,
        )
        run(
            [
                *self.command,
                "up",
                "-d",
                "--wait",
                "--wait-timeout",
                "240",
                *SERVICES,
            ],
            self.compose_env,
        )
        run(
            [
                *self.command,
                "exec",
                "-T",
                "-e",
                f"GATEWAY_KEY_UID={os.getuid()}",
                "-e",
                f"GATEWAY_KEY_GID={os.getgid()}",
                "litellm",
                "python",
                "/app/platform-bootstrap-key.py",
                "--reuse-existing",
            ],
            self.compose_env,
        )
        runtime_key = dotenv_values(self.key_file).get("PLATFORM_LITELLM_KEY")
        if not runtime_key or runtime_key == self.compose_env["LITELLM_MASTER_KEY"]:
            raise RuntimeError("未生成有效的 LiteLLM 受限调用密钥。")
        self.key_file.chmod(0o600)
        redis_url = self.env.get("PLATFORM_REDIS_URL", "").strip()
        if not redis_url:
            # API and Worker are separate host processes. An in-process notifier
            # cannot wake the API after a worker commit; discover our loopback
            # Redis mapping instead of falling back to the two-second DB poll.
            result = subprocess.run(
                [*self.command, "port", "redis", "6379"],
                env=self.compose_env,
                capture_output=True,
                text=True,
                check=False,
                timeout=15,
            )
            binding = re.fullmatch(r"127\.0\.0\.1:(\d{1,5})", result.stdout.strip())
            if result.returncode or not binding or not 1 <= int(binding[1]) <= 65535:
                raise RuntimeError(
                    "无法读取本地 Redis 的回环端口，请检查网关容器配置。"
                )
            redis_url = f"redis://127.0.0.1:{binding[1]}/0"
        return {
            **platform_environment(self.env),
            "PLATFORM_REDIS_URL": redis_url,
            "PLATFORM_LITELLM_URL": self.url + "/v1",
            "PLATFORM_LITELLM_KEY": runtime_key,
            "PLATFORM_LITELLM_ADMIN_URL": self.url + "/ui",
            "PLATFORM_DEFAULT_MODEL": self.compose_env["LITELLM_MODEL_ALIAS"],
        }

    def synchronization_environment(self, runtime: dict[str, str]) -> dict[str, str]:
        """Only this dedicated process receives the LiteLLM master credential."""
        return {
            **platform_environment(runtime),
            "PLATFORM_GATEWAY_CONTROL_URL": self.url,
            "PLATFORM_GATEWAY_CONTROL_KEY": self.compose_env["LITELLM_MASTER_KEY"],
        }

    def stop(self) -> None:
        if self.started:
            try:
                result = subprocess.run(
                    [*self.command, "stop", "--timeout", "10", *SERVICES],
                    env=self.compose_env,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=45,
                )
                stopped = result.returncode == 0
            except (subprocess.TimeoutExpired, OSError):
                stopped = False
            if not stopped:
                print(
                    "本地网关未能完全停止，请检查 Docker；数据库卷已保留。", flush=True
                )
            self.started = False
