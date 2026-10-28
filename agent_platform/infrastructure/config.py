"""Explicit platform configuration, separate from existing demos."""

import math
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .token_defaults import DEFAULT_CONTEXT_WINDOW, DEFAULT_OUTPUT_TOKENS


@dataclass(frozen=True)
class Settings:
    service_role: str = "api"
    mode: str = "local"
    operator_emails: tuple[str, ...] = ()
    secret_encryption_key: str = ""
    gateway_control_url: str = ""
    gateway_control_key: str = ""
    gateway_sync_interval: float = 2.0
    worker_concurrency: int = 4
    maintenance_interval: float = 30.0
    export_ttl_seconds: int = 86400
    maintenance_batch_size: int = 100
    deletion_retention_days: int = 30
    tombstone_path: Path = (
        Path(__file__).resolve().parents[1] / ".data/tenant-tombstones.jsonl"
    )
    export_directory: Path = Path(__file__).resolve().parents[1] / ".data/exports"
    database_url: str = "sqlite:///agent_platform/.data/platform.db"
    redis_url: str | None = None
    litellm_url: str = ""
    litellm_key: str = ""
    litellm_admin_url: str = ""
    gateway_local_address: str | None = None
    default_model: str = ""
    default_output_tokens: int = DEFAULT_OUTPUT_TOKENS
    default_context_window: int = DEFAULT_CONTEXT_WINDOW
    default_model_input_price: str = ""
    default_model_output_price: str = ""
    admin_email: str = "admin@example.com"
    admin_password: str = ""
    secure_cookies: bool = False
    embedded_worker: bool = False
    session_hours: int = 24
    queue_timeout: int = 300
    run_timeout: int = 300
    model_timeout: int = 90
    max_run_cost: str = "1"
    worker_poll_seconds: float = 0.5
    web_directory: Path = Path(__file__).resolve().parents[1] / "apps/web/dist"

    def __post_init__(self) -> None:
        if self.service_role not in {
            "api",
            "worker",
            "gateway-sync",
            "maintenance",
            "init",
        }:
            raise ValueError("Unknown platform service role")
        if self.mode not in {"local", "saas"}:
            raise ValueError("PLATFORM_MODE must be local or saas")
        if self.worker_concurrency < 1:
            raise ValueError("PLATFORM_WORKER_CONCURRENCY must be positive")
        if (
            not 1
            <= self.default_output_tokens
            <= self.default_context_window
            <= 2000000
        ):
            raise ValueError(
                "Default output tokens must fit the default context window (at most 2000000 tokens)"
            )
        for name in ("queue_timeout", "run_timeout"):
            value = getattr(self, name)
            if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"PLATFORM_{name.upper()} must be finite and positive")
        if self.mode == "saas":
            if not self.database_url.startswith("postgresql"):
                raise ValueError("SaaS mode requires PostgreSQL")
            if self.service_role in {"api", "worker", "init"} and not self.redis_url:
                raise ValueError("SaaS mode requires PLATFORM_REDIS_URL")
            if self.embedded_worker:
                raise ValueError("SaaS requires separate workers")
            if self.service_role == "api" and not self.secure_cookies:
                raise ValueError("SaaS API requires secure cookies")
            if not self.secret_encryption_key:
                raise ValueError("SaaS requires PLATFORM_SECRET_ENCRYPTION_KEY")
        if self.secret_encryption_key:
            from cryptography.fernet import Fernet

            try:
                Fernet(self.secret_encryption_key.encode())
            except (TypeError, ValueError):
                raise ValueError(
                    "PLATFORM_SECRET_ENCRYPTION_KEY must be a valid Fernet key"
                ) from None
        if not self.litellm_admin_url:
            return
        # This address is returned to the browser as an external link. Reject
        # credentials and ambiguous browser URL syntax before it can be exposed.
        try:
            url = urlsplit(self.litellm_admin_url)
            valid = (
                url.scheme in {"http", "https"}
                and bool(url.hostname)
                and url.username is None
                and url.password is None
                and not any(char in self.litellm_admin_url for char in "\\?#")
                and not any(
                    char.isspace() or ord(char) < 32 or ord(char) == 127
                    for char in self.litellm_admin_url
                )
                and "%" not in url.netloc
                and (url.port is None or 0 < url.port <= 65535)
            )
        except ValueError:
            valid = False
        if not valid:
            raise ValueError(
                "PLATFORM_LITELLM_ADMIN_URL must be an HTTP(S) URL without "
                "credentials, query parameters or fragments."
            )

    @classmethod
    def from_env(cls, *, role: str = "api") -> "Settings":
        return cls(
            service_role=role,
            mode=os.getenv("PLATFORM_MODE", "local"),
            operator_emails=tuple(
                x.strip().lower()
                for x in os.getenv("PLATFORM_OPERATOR_EMAILS", "").split(",")
                if x.strip()
            ),
            secret_encryption_key=os.getenv("PLATFORM_SECRET_ENCRYPTION_KEY", ""),
            gateway_control_url=os.getenv("PLATFORM_GATEWAY_CONTROL_URL", ""),
            gateway_control_key=os.getenv("PLATFORM_GATEWAY_CONTROL_KEY", ""),
            gateway_sync_interval=float(
                os.getenv("PLATFORM_GATEWAY_SYNC_INTERVAL", "2")
            ),
            worker_concurrency=int(os.getenv("PLATFORM_WORKER_CONCURRENCY", "4")),
            maintenance_interval=float(
                os.getenv("PLATFORM_MAINTENANCE_INTERVAL", "30")
            ),
            export_ttl_seconds=int(os.getenv("PLATFORM_EXPORT_TTL_SECONDS", "86400")),
            maintenance_batch_size=int(
                os.getenv("PLATFORM_MAINTENANCE_BATCH_SIZE", "100")
            ),
            deletion_retention_days=int(
                os.getenv("PLATFORM_DELETION_RETENTION_DAYS", "30")
            ),
            tombstone_path=Path(
                os.getenv("PLATFORM_TOMBSTONE_PATH", str(cls.tombstone_path))
            ),
            export_directory=Path(
                os.getenv("PLATFORM_EXPORT_DIRECTORY", str(cls.export_directory))
            ),
            database_url=os.getenv("PLATFORM_DATABASE_URL", cls.database_url),
            redis_url=os.getenv("PLATFORM_REDIS_URL") or None,
            litellm_url=os.getenv("PLATFORM_LITELLM_URL", ""),
            litellm_key=os.getenv("PLATFORM_LITELLM_KEY", ""),
            litellm_admin_url=os.getenv("PLATFORM_LITELLM_ADMIN_URL", "").strip(),
            gateway_local_address=os.getenv("PLATFORM_GATEWAY_LOCAL_ADDRESS") or None,
            default_model=os.getenv("PLATFORM_DEFAULT_MODEL", "").strip(),
            default_output_tokens=int(
                os.getenv("PLATFORM_DEFAULT_OUTPUT_TOKENS", str(DEFAULT_OUTPUT_TOKENS))
            ),
            default_context_window=int(
                os.getenv(
                    "PLATFORM_DEFAULT_CONTEXT_WINDOW", str(DEFAULT_CONTEXT_WINDOW)
                )
            ),
            default_model_input_price=os.getenv(
                "PLATFORM_DEFAULT_MODEL_INPUT_PRICE", ""
            ).strip(),
            default_model_output_price=os.getenv(
                "PLATFORM_DEFAULT_MODEL_OUTPUT_PRICE", ""
            ).strip(),
            admin_email=os.getenv("PLATFORM_ADMIN_EMAIL", "admin@example.com"),
            admin_password=os.getenv("PLATFORM_ADMIN_PASSWORD", ""),
            secure_cookies=os.getenv("PLATFORM_SECURE_COOKIES", "false").lower()
            == "true",
            embedded_worker=os.getenv("PLATFORM_EMBEDDED_WORKER", "false").lower()
            == "true",
            queue_timeout=int(os.getenv("PLATFORM_QUEUE_TIMEOUT", "300")),
            run_timeout=int(os.getenv("PLATFORM_RUN_TIMEOUT", "300")),
            model_timeout=int(os.getenv("PLATFORM_MODEL_TIMEOUT", "90")),
            max_run_cost=os.getenv("PLATFORM_MAX_RUN_COST", "1"),
        )
