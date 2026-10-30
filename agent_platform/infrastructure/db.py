"""SQLAlchemy storage with serial transactions on SQLite and PostgreSQL."""

import hashlib
import importlib
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from decimal import Decimal
from pathlib import Path
from threading import RLock

from sqlalchemy import MetaData, create_engine, event, text
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.pool import StaticPool

metadata = MetaData()
_tenant = ContextVar("platform_tenant", default=None)


class Database:
    def __init__(self, url: str):
        parsed = make_url(url)
        if parsed.drivername == "postgresql":
            parsed = parsed.set(drivername="postgresql+psycopg")
        sqlite = parsed.get_backend_name() == "sqlite"
        if sqlite and parsed.database and parsed.database != ":memory:":
            Path(parsed.database).resolve().parent.mkdir(parents=True, exist_ok=True)
        options = {"pool_pre_ping": True}
        if sqlite:
            options["connect_args"] = {"check_same_thread": False, "timeout": 30}
            if parsed.database in (None, "", ":memory:"):
                options["poolclass"] = StaticPool
        self.engine = create_engine(parsed, **options)
        self.sqlite = sqlite
        # StaticPool shares a physical connection, including its transaction.
        self._memory_lock = RLock() if options.get("poolclass") is StaticPool else None
        if sqlite:

            @event.listens_for(self.engine, "connect")
            def pragmas(connection, _record):
                class ExactSum:
                    def __init__(self):
                        self.total = Decimal(0)

                    def step(self, value):
                        if value is not None:
                            self.total += Decimal(str(value))

                    def finalize(self):
                        return str(self.total)

                connection.create_aggregate("exact_sum", 1, ExactSum)
                cursor = connection.cursor()
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA busy_timeout=30000")
                cursor.close()

    def create_schema(self) -> None:
        # Import table declarations before metadata.create_all; no business writes.
        for module in (
            "tables",
            "tenancy_tables",
            "gateway_tables",
            "runtime_tables",
            "collaboration_tables",
            "journal_tables",
            "checkpoint_tables",
            "task_lease_tables",
            "remote_agent_tables",
            "remote_call_tables",
            "operations_tables",
            "support_tables",
            "metering_tables",
        ):
            importlib.import_module("agent_platform.infrastructure." + module)
        importlib.import_module("agent_platform.modules.billing.service")

        metadata.create_all(self.engine)
        if self.sqlite:
            with self.engine.connect() as connection:
                connection.exec_driver_sql("PRAGMA journal_mode=WAL")
                connection.commit()

    @contextmanager
    def tenant_scope(self, tenant_id: str):
        """Bind one request/job. Context is task-local and reset even on cancellation."""
        if not tenant_id or len(tenant_id) > 64:
            raise ValueError("Invalid tenant context")
        token = _tenant.set(tenant_id)
        try:
            yield
        finally:
            _tenant.reset(token)

    def set_tenant(self, connection: Connection, tenant_id: str) -> None:
        if not self.sqlite:
            connection.execute(
                text("SELECT set_config('app.tenant_id', :tenant, true)"),
                {"tenant": tenant_id},
            )

    def assert_schema(self, *, saas: bool = False) -> None:
        """Runtime never migrates production, and must not run with an RLS bypass role."""
        with self.engine.connect() as connection:
            head = connection.scalar(text("SELECT version_num FROM alembic_version"))
            from alembic.config import Config
            from alembic.script import ScriptDirectory

            config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
            expected = ScriptDirectory.from_config(config).get_current_head()
            if head != expected:
                raise RuntimeError(
                    "Run platform Alembic migrations before starting this release"
                )
            if saas:
                role = connection.execute(
                    text(
                        "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname=current_user"
                    )
                ).one()
                owner = connection.scalar(
                    text(
                        "SELECT pg_get_userbyid(relowner)=current_user FROM pg_class WHERE oid='platform_runs'::regclass"
                    )
                )
                if role[0] or role[1] or owner:
                    raise RuntimeError(
                        "SaaS runtime must use a non-owner role without SUPERUSER/BYPASSRLS"
                    )
                for module in (
                    "tables",
                    "gateway_tables",
                    "runtime_tables",
                    "collaboration_tables",
                    "journal_tables",
                    "checkpoint_tables",
                    "task_lease_tables",
                    "remote_agent_tables",
                    "remote_call_tables",
                    "operations_tables",
                    "metering_tables",
                ):
                    importlib.import_module("agent_platform.infrastructure." + module)
                control = {
                    "platform_users",
                    "platform_memberships",
                    "platform_tenant_settings",
                    "platform_entitlements",
                    "platform_invitations",
                    "platform_operation_audits",
                    "platform_tenant_creation_requests",
                    "platform_support_grants",
                }
                protected = [
                    name
                    for name, table in metadata.tables.items()
                    if "tenant_id" in table.c and name not in control
                ]
                rows = connection.execute(
                    text(
                        "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity, pg_get_userbyid(c.relowner)=current_user AS owned FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=current_schema() AND c.relname=ANY(:names)"
                    ),
                    {"names": protected},
                ).all()
                if len(rows) != len(protected) or any(
                    not row[1] or not row[2] or row[3] for row in rows
                ):
                    raise RuntimeError(
                        "SaaS tenant tables require FORCE RLS and a non-owner runtime role"
                    )

    @contextmanager
    def read(self) -> Iterator[Connection]:
        with (
            self._memory_lock if self._memory_lock else nullcontext(),
            self.engine.connect() as connection,
        ):
            if _tenant.get():
                self.set_tenant(connection, _tenant.get())
            yield connection

    @contextmanager
    def transaction(self, scope: str = "platform") -> Iterator[Connection]:
        notifications = ()
        with (
            self._memory_lock if self._memory_lock else nullcontext(),
            self.engine.connect() as connection,
        ):
            # Domain writes coalesce wakeups here. Dispatch only after the
            # transaction, connection and SQLite memory lock are released.
            connection.info["platform_commit_notifications"] = {}
            try:
                if self.sqlite:
                    connection.exec_driver_sql("BEGIN IMMEDIATE")
                else:
                    connection.begin()
                    tenant_id = (
                        scope[7:] if scope.startswith("tenant:") else _tenant.get()
                    )
                    if tenant_id:
                        self.set_tenant(connection, tenant_id)
                    lock_id = int.from_bytes(
                        hashlib.sha256(scope.encode()).digest()[:8], "big", signed=True
                    )
                    connection.execute(
                        text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_id}
                    )
                yield connection
                connection.commit()
                notifications = tuple(
                    connection.info["platform_commit_notifications"].items()
                )
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.info.pop("platform_commit_notifications", None)
        if notifier := getattr(self, "event_notifier", None):
            for (tenant_id, run_id), sequence in notifications:
                notifier.publish(tenant_id, run_id, sequence)

    def lock(self, connection: Connection, scope: str) -> None:
        """Additional lock in the caller's transaction; tenant precedes cost locks."""
        if not self.sqlite:
            lock_id = int.from_bytes(
                hashlib.sha256(scope.encode()).digest()[:8], "big", signed=True
            )
            connection.execute(
                text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_id}
            )

    def close(self) -> None:
        if notifier := getattr(self, "event_notifier", None):
            notifier.close()
        self.engine.dispose()
