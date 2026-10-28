"""Offline, fail-closed SQLite to PostgreSQL cutover; dry-run by default.

Run from the repository root with ``python -m agent_platform.scripts.migrate_sqlite``.
This copies data, never starts services, calls a gateway, migrates schema, or
releases a financial hold. See docs/multi-tenant-operations.md before applying.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import logging
import os
import sqlite3
import sys
import time
from collections import defaultdict
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime
from decimal import Decimal, localcontext
from itertools import groupby
from pathlib import Path
from urllib.parse import quote

from alembic.config import Config
from alembic.script import ScriptDirectory
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool

from agent_platform.infrastructure.db import metadata
from agent_platform.modules.billing.service import ExactMoney

PLATFORM = Path(__file__).resolve().parents[1]
SKIP_TABLES = {
    "platform_auth_sessions",
    "platform_stream_leases",
    "platform_service_heartbeats",
}
# Authentication/control metadata is deliberately outside tenant content RLS.
GLOBAL_TABLES = {
    "platform_tenants",
    "platform_users",
    "platform_auth_sessions",
    "platform_login_attempts",
    "platform_memberships",
    "platform_roles",
    "platform_tenant_settings",
    "platform_entitlements",
    "platform_invitations",
    "platform_operation_audits",
    "platform_tenant_creation_requests",
    "platform_service_heartbeats",
    "platform_support_grants",
    "model_deployments",
}
STRING_MONEY = {
    "platform_models": {"input_price", "output_price"},
    "platform_calls": {"cost", "provider_cost"},
    "platform_entitlements": {"max_budget"},
}
TERMINAL_RUNS = ("succeeded", "failed", "cancelled", "expired")
MANIFEST_CONFIRMATIONS = (
    "source_stopped",
    "target_services_stopped",
    "old_executors_fenced",
    "gateway_inventory_reviewed",
    "gateway_sync_held",
    "artifacts_preserved",
    "tombstones_preserved",
    "same_encryption_key_provisioned",
)


class MigrationError(Exception):
    """Only fixed codes and source-controlled table/column names are public."""


def require(condition, code):
    if not condition:
        raise MigrationError(code)


def load_schema():
    # Import trusted declarations from this release, including future table files.
    for path in sorted((PLATFORM / "infrastructure").glob("*tables.py")):
        importlib.import_module("agent_platform.infrastructure." + path.stem)
    heads = ScriptDirectory.from_config(
        Config(str(PLATFORM / "alembic.ini"))
    ).get_heads()
    require(len(heads) == 1, "release_requires_exactly_one_alembic_head")
    return list(metadata.sorted_tables), heads[0]


@contextmanager
def sqlite_snapshot(path):
    require(path.is_file(), "sqlite_file_missing")
    uri = "file:" + quote(str(path.resolve()), safe="/") + "?mode=ro"
    engine = create_engine(
        "sqlite+pysqlite://",
        creator=lambda: sqlite3.connect(uri, uri=True, timeout=5),
        poolclass=NullPool,
        hide_parameters=True,
    )
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA query_only=ON")
            connection.exec_driver_sql("BEGIN")
            yield connection
            connection.rollback()
    finally:
        engine.dispose()


def assert_schema(connection, tables, head):
    inspector = inspect(connection)
    names = set(inspector.get_table_names())
    expected = {table.name for table in tables} | {"alembic_version"}
    require(names == expected, "schema_table_set_differs_from_release")
    versions = (
        connection.execute(text("SELECT version_num FROM alembic_version"))
        .scalars()
        .all()
    )
    require(versions == [head], "schema_head_differs_from_release")
    for table in tables:
        columns = {column["name"] for column in inspector.get_columns(table.name)}
        require(columns == set(table.c.keys()), "schema_columns_differ:" + table.name)
        require(
            bool(table.primary_key.columns), "table_without_primary_key:" + table.name
        )


def assert_source_still(connection):
    runs = metadata.tables["platform_runs"]
    require(
        connection.scalar(
            select(func.count())
            .select_from(runs)
            .where(runs.c.status.not_in(TERMINAL_RUNS))
        )
        == 0,
        "source_has_nonterminal_runs",
    )
    heartbeat = metadata.tables["platform_service_heartbeats"]
    require(
        connection.scalar(
            select(func.count())
            .select_from(heartbeat)
            .where(heartbeat.c.expires_at > time.time())
        )
        == 0,
        "source_has_live_service_heartbeats",
    )
    require(
        connection.exec_driver_sql("PRAGMA foreign_key_check").first() is None,
        "source_foreign_key_violation",
    )


def set_tenant(connection, tenant_id):
    connection.execute(
        text("SELECT set_config('app.tenant_id', :tenant, true)"),
        {"tenant": tenant_id or ""},
    )


def qualified(connection, table):
    quote_identifier = connection.dialect.identifier_preparer.quote_identifier
    return quote_identifier("public") + "." + quote_identifier(table.name)


def assert_target_empty(connection, tables, *, lock=False):
    role = connection.execute(
        text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname=current_user")
    ).one()
    require(
        not role.rolsuper and not role.rolbypassrls, "target_role_must_not_bypass_rls"
    )
    # Read-only preflight also sees rows hidden by FORCE RLS. A pristine main heap
    # is stricter than count(*)=0; DELETE-empty heaps require a fresh database.
    if lock:
        connection.exec_driver_sql("SET LOCAL lock_timeout = '5s'")
        connection.exec_driver_sql(
            "LOCK TABLE "
            + ", ".join(qualified(connection, table) for table in tables)
            + ', "public"."alembic_version"'
            + " IN ACCESS EXCLUSIVE MODE"
        )
    for table in tables:
        relation = connection.execute(
            text(
                "SELECT c.relkind, c.relrowsecurity, c.relforcerowsecurity, "
                "pg_relation_size(c.oid, 'main') AS heap_bytes "
                "FROM pg_class c WHERE c.oid=to_regclass(:name)"
            ),
            {"name": qualified(connection, table)},
        ).one()
        require(relation.relkind == "r", "target_requires_regular_tables:" + table.name)
        require(relation.heap_bytes == 0, "target_is_not_pristine:" + table.name)
        if table.name not in GLOBAL_TABLES:
            require("tenant_id" in table.c, "unclassified_global_table:" + table.name)
            require(
                relation.relrowsecurity and relation.relforcerowsecurity,
                "target_force_rls_missing:" + table.name,
            )


def money_columns(table):
    return {
        column.name
        for column in table.c
        if isinstance(column.type, ExactMoney)
        or column.name in STRING_MONEY.get(table.name, ())
    }


def decimal_text(value):
    result = format(value, "f")
    if "." in result:
        result = result.rstrip("0").rstrip(".")
    return "0" if result in {"-0", ""} else result


def money(value, table, column):
    result = Decimal(str(value))
    require(result.is_finite(), "nonfinite_money:" + table.name + "." + column)
    with localcontext() as context:
        context.prec = 80
        require(
            abs(result) < Decimal("1e18")
            and result == result.quantize(Decimal("1e-12")),
            "money_exceeds_postgres_precision:" + table.name + "." + column,
        )
    return result


def canonical(value):
    if isinstance(value, Decimal):
        return decimal_text(value)
    if isinstance(value, dict):
        return {key: canonical(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [canonical(item) for item in value]
    return value


class Summary:
    """Order-independent SHA-256 multiset checksum plus exact monetary totals."""

    def __init__(self, table):
        self.table = table
        self.count = 0
        self.checksum = 0
        self.money = {name: Decimal(0) for name in sorted(money_columns(table))}

    def add(self, row):
        value = dict(row)
        for name in self.money:
            if value[name] is not None:
                amount = money(value[name], self.table, name)
                with localcontext() as context:
                    context.prec = 80
                    self.money[name] += amount
                # Normalize only typed ExactMoney; text columns retain exact text
                # in the row digest, so an unintended text rewrite is detected.
                if isinstance(self.table.c[name].type, ExactMoney):
                    value[name] = decimal_text(amount)
        encoded = json.dumps(
            canonical(value),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode()
        self.checksum = (
            self.checksum + int.from_bytes(hashlib.sha256(encoded).digest(), "big")
        ) % (1 << 256)
        self.count += 1

    def result(self):
        return {
            "rows": self.count,
            "sha256_multiset_sum": f"{self.checksum:064x}",
            "money_totals": {
                name: decimal_text(value) for name, value in self.money.items()
            },
        }


def rows(connection, table, tenant=None, *, scoped=False):
    statement = select(table)
    if scoped:
        statement = (
            statement.where(table.c.tenant_id == tenant)
            if tenant is not None
            else statement.where(table.c.tenant_id.is_(None))
        )
    # Psycopg normally buffers a full result; a server cursor bounds readback
    # memory too. SQLite already fetches incrementally and ignores this option.
    with connection.execute(
        statement.execution_options(stream_results=True, max_row_buffer=500)
    ) as result:
        yield from result.mappings()


def summarize(connection, table):
    summary = Summary(table)
    for row in rows(connection, table):
        summary.add(row)
    return summary.result()


def source_partitions(connection, table, tenant_ids):
    if "tenant_id" not in table.c:
        return [None]
    partitions = list(connection.scalars(select(table.c.tenant_id).distinct()))
    for tenant in partitions:
        # Global audits may record a platform-only operation with no tenant.
        require(
            tenant in tenant_ids
            or (
                tenant is None
                and table.c.tenant_id.nullable
                and table.name in GLOBAL_TABLES
            ),
            "orphan_tenant_reference:" + table.name,
        )
    return partitions


def financial_baseline(connection):
    """Validate existing facts without repairing money or guessing usage."""
    reservations = metadata.tables["billing_reservations"]
    wallets = metadata.tables["billing_wallets"]
    entries = metadata.tables["billing_entries"]
    transactions = metadata.tables["billing_transactions"]
    holds = defaultdict(Decimal)
    pending = defaultdict(int)
    frozen = set()
    for row in connection.execute(
        select(
            reservations.c.tenant_id, reservations.c.status, reservations.c.amount
        ).where(reservations.c.status.in_(("reserved", "unresolved")))
    ).mappings():
        holds[row["tenant_id"]] += row["amount"]
        frozen.add(row["tenant_id"])
        pending[row["status"]] += 1
    available = set()
    wallet_fingerprint = Summary(wallets)
    for row in rows(connection, wallets):
        available.add(row["tenant_id"])
        require(
            row["balance"] >= row["reserved"] >= 0, "source_wallet_invariant_failed"
        )
        require(
            row["reserved"] == holds[row["tenant_id"]],
            "source_reserved_hold_sum_mismatch",
        )
        wallet_fingerprint.add(row)
    require(set(holds) <= available, "source_hold_has_no_wallet")
    require(
        connection.scalar(
            select(func.count())
            .select_from(
                entries.outerjoin(
                    transactions,
                    (entries.c.tenant_id == transactions.c.tenant_id)
                    & (entries.c.transaction_id == transactions.c.id),
                )
            )
            .where(transactions.c.id.is_(None))
        )
        == 0,
        "source_ledger_entry_has_no_transaction",
    )
    posting_query = select(
        entries.c.tenant_id,
        entries.c.transaction_id,
        entries.c.currency,
        entries.c.amount,
    ).order_by(entries.c.tenant_id, entries.c.transaction_id, entries.c.currency)
    posting_groups = 0
    for _key, group in groupby(
        connection.execute(posting_query),
        key=lambda row: (row.tenant_id, row.transaction_id, row.currency),
    ):
        require(
            sum((row.amount for row in group), Decimal(0)) == 0,
            "source_ledger_unbalanced",
        )
        posting_groups += 1
    require(
        connection.scalar(
            select(func.count())
            .select_from(transactions)
            .where(
                ~select(entries.c.id)
                .where(
                    entries.c.tenant_id == transactions.c.tenant_id,
                    entries.c.transaction_id == transactions.c.id,
                )
                .exists()
            )
        )
        == 0,
        "source_transaction_has_no_postings",
    )
    wallet_postings = defaultdict(Decimal)
    for tenant, amount in connection.execute(
        select(entries.c.tenant_id, entries.c.amount).where(
            entries.c.account == "customer_wallet"
        )
    ):
        wallet_postings[tenant] += amount
    require(set(wallet_postings) <= available, "source_posting_has_no_wallet")
    for row in rows(connection, wallets):
        require(
            row["balance"] == wallet_postings[row["tenant_id"]],
            "source_wallet_ledger_balance_mismatch",
        )
    return frozen, {
        "pending_reservations": dict(pending),
        "balanced_posting_groups": posting_groups,
        "wallets": wallet_fingerprint.result(),
    }


def transform(table, row, frozen_tenants):
    result = dict(row)
    if {"owner", "lease_until", "fence"} <= set(table.c.keys()):
        result["owner"] = None
        result["fence"] += 1
        recovering = (
            table.name == "gateway_outbox" and row["status"] == "applying"
        ) or (table.name == "export_jobs" and row["status"] == "running")
        # Existing recovery queries use `lease_until < now`, not IS NULL.
        result["lease_until"] = 0.0 if recovering else None
    if (
        table.name == "billing_wallets"
        and row["tenant_id"] in frozen_tenants
        and not row["blocked"]
    ):
        result["blocked"] = True
        result["block_reason"] = "migration_pending_financial_review"
    return result


def file_digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def check_manifest(args, backup_digest):
    require(args.source_stopped, "apply_requires_source_stopped_confirmation")
    require(args.backup is not None, "apply_requires_backup")
    require(args.manifest is not None, "apply_requires_external_state_manifest")
    require(
        args.manifest.is_file() and args.manifest.stat().st_size <= 65536,
        "manifest_missing_or_too_large",
    )
    manifest = json.loads(args.manifest.read_text())
    expected = set(MANIFEST_CONFIRMATIONS) | {
        "schema_version",
        "backup_sha256",
        "encryption_key_sha256",
        "reviewed_at",
        "reviewed_by",
        "evidence_reference",
    }
    require(
        isinstance(manifest, dict) and set(manifest) == expected,
        "manifest_fields_invalid",
    )
    require(manifest["schema_version"] == 1, "manifest_version_invalid")
    for field in MANIFEST_CONFIRMATIONS:
        require(manifest[field] is True, "manifest_confirmation_missing:" + field)
    for field in ("reviewed_by", "evidence_reference"):
        require(
            isinstance(manifest[field], str)
            and 0 < len(manifest[field].strip()) <= 200,
            "manifest_evidence_missing",
        )
    reviewed = datetime.fromisoformat(manifest["reviewed_at"])
    require(
        reviewed.tzinfo is not None
        and 0 <= time.time() - reviewed.timestamp() <= 86400,
        "manifest_review_must_be_within_24_hours",
    )
    require(
        manifest["backup_sha256"] == backup_digest, "manifest_backup_digest_mismatch"
    )
    key = os.environ.get(args.key_env, "").strip()
    require(bool(key), "encryption_key_environment_missing")
    require(
        hashlib.sha256(key.encode()).hexdigest() == manifest["encryption_key_sha256"],
        "manifest_encryption_key_digest_mismatch",
    )
    return Fernet(key.encode())


def verify_ciphertexts(connection, cipher):
    credentials = metadata.tables["gateway_credential_versions"]
    count = 0
    for encrypted in connection.scalars(
        select(credentials.c.secret_ciphertext).where(
            credentials.c.secret_ciphertext.is_not(None)
        )
    ):
        # Only confirm decryptability. Never deserialize, display, hash, or persist
        # the plaintext, and never send it to a provider.
        cipher.decrypt(encrypted.encode())
        count += 1
    return count


def copy_table(source, target, table, partitions, frozen_tenants, batch_size):
    expected = Summary(table)
    for tenant in partitions:
        set_tenant(target, tenant)
        batch = []
        for row in rows(source, table, tenant, scoped="tenant_id" in table.c):
            item = transform(table, row, frozen_tenants)
            expected.add(item)
            batch.append(item)
            if len(batch) >= batch_size:
                target.execute(table.insert(), batch)
                batch.clear()
        if batch:
            target.execute(table.insert(), batch)
    observed = Summary(table)
    for tenant in partitions:
        set_tenant(target, tenant)
        for row in rows(target, table, tenant, scoped="tenant_id" in table.c):
            observed.add(row)
    require(
        observed.result() == expected.result(),
        "target_readback_checksum_mismatch:" + table.name,
    )
    return observed.result()


def run(args, report):
    tables, head = load_schema()
    report["schema_head"] = head
    require(1 <= args.batch_size <= 2000, "batch_size_out_of_range")
    target_url = os.environ.get(args.target_env, "")
    require(bool(target_url), "target_database_environment_missing")
    parsed = make_url(target_url)
    require(parsed.get_backend_name() == "postgresql", "target_must_be_postgresql")
    if parsed.drivername == "postgresql":
        parsed = parsed.set(drivername="postgresql+psycopg")
    engine = create_engine(
        parsed,
        hide_parameters=True,
        poolclass=NullPool,
        connect_args={"connect_timeout": 10},
    )
    try:
        with ExitStack() as stack, localcontext() as context:
            context.prec = 80
            source = stack.enter_context(sqlite_snapshot(args.source))
            assert_schema(source, tables, head)
            assert_source_still(source)
            tenant_ids = set(
                source.scalars(select(metadata.tables["platform_tenants"].c.id))
            )
            frozen, financial = financial_baseline(source)
            report["tenant_count"] = len(tenant_ids)
            report["financial_baseline"] = financial
            report["wallets_requiring_financial_review"] = len(frozen)
            partitions = {
                table.name: source_partitions(source, table, tenant_ids)
                for table in tables
            }
            report["source"] = {
                table.name: summarize(source, table) for table in tables
            }
            if args.backup:
                require(
                    args.backup.is_file() and not args.backup.samefile(args.source),
                    "backup_must_be_a_separate_existing_file",
                )
                require(
                    not Path(str(args.backup) + "-wal").exists(),
                    "backup_must_be_a_self_contained_snapshot",
                )
                backup = stack.enter_context(sqlite_snapshot(args.backup))
                assert_schema(backup, tables, head)
                backup_summaries = {
                    table.name: summarize(backup, table) for table in tables
                }
                require(
                    backup_summaries == report["source"],
                    "backup_logical_digest_differs_from_source",
                )
                backup_hash = file_digest(args.backup)
                report["backup"] = {"verified": True, "sha256": backup_hash}
            else:
                backup_hash = None
                report["backup"] = {"verified": False}
            if args.apply:
                cipher = check_manifest(args, backup_hash)
                report["encrypted_credentials_verified"] = verify_ciphertexts(
                    source, cipher
                )
            else:
                report["apply_requirements"] = [
                    "--apply",
                    "--source-stopped",
                    "--backup",
                    "--manifest",
                    "same encryption key in --key-env",
                ]
            # Even dry-run uses only a read-only target transaction.
            with engine.connect() as target:
                transaction = target.begin()
                try:
                    if not args.apply:
                        target.exec_driver_sql("SET TRANSACTION READ ONLY")
                    target.exec_driver_sql("SET LOCAL search_path = public")
                    assert_schema(target, tables, head)
                    assert_target_empty(target, tables, lock=args.apply)
                    if args.apply:
                        # Cover a concurrent migration between initial inspection
                        # and acquiring table/version locks.
                        assert_schema(target, tables, head)
                    report["target_pristine"] = True
                    report["skipped_tables"] = sorted(SKIP_TABLES)
                    report["transformations"] = {
                        "lease_owners_cleared": True,
                        "fences_incremented": True,
                        "inflight_job_leases_expired": True,
                        "pending_finance_wallets_blocked": True,
                        "outbox_state_and_phase_preserved": True,
                    }
                    report["target"] = {}
                    for table in tables:
                        if table.name in SKIP_TABLES:
                            report["target"][table.name] = Summary(table).result()
                        elif args.apply:
                            report["target"][table.name] = copy_table(
                                source,
                                target,
                                table,
                                partitions[table.name],
                                frozen,
                                args.batch_size,
                            )
                        else:
                            planned = Summary(table)
                            for row in rows(source, table):
                                planned.add(transform(table, row, frozen))
                            report["target"][table.name] = planned.result()
                    if args.apply:
                        report["commit_attempted"] = True
                        transaction.commit()
                        report["committed"] = True
                    else:
                        transaction.rollback()
                    report["status"] = "applied" if args.apply else "dry_run_ready"
                except BaseException:
                    transaction.rollback()
                    raise
    finally:
        engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        type=Path,
        required=True,
        help="SQLite database or stopped read-only snapshot",
    )
    parser.add_argument(
        "--backup",
        type=Path,
        help="Separate self-contained SQLite backup; required for --apply",
    )
    parser.add_argument(
        "--target-env",
        default="PLATFORM_MIGRATION_DATABASE_URL",
        help="Name of an environment variable, never a connection string",
    )
    parser.add_argument("--key-env", default="PLATFORM_SECRET_ENCRYPTION_KEY")
    parser.add_argument(
        "--manifest",
        type=Path,
        help="Reviewed external-state manifest; required for --apply",
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--source-stopped",
        action="store_true",
        help="Operator confirms the source and every old executor are stopped",
    )
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument(
        "--report",
        type=Path,
        help="New report file, created exclusively with permissions 0600",
    )
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    report = {
        "format_version": 1,
        "mode": "apply" if args.apply else "dry-run",
        "started_at": datetime.now(UTC).isoformat(),
        "committed": False,
        "commit_attempted": False,
    }
    # Reserve the report before a transaction can commit, never overwrite a file.
    output = None
    try:
        if args.report:
            output = os.fdopen(
                os.open(args.report, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w"
            )
        run(args, report)
        code = 0
    except (Exception, KeyboardInterrupt) as error:  # noqa: BLE001 -- redact driver errors/SQL parameters
        report["status"] = "failed"
        report["error"] = (
            str(error)
            if isinstance(error, MigrationError)
            else "operation_failed:" + type(error).__name__
        )
        # A lost connection during COMMIT has an unknown outcome. Never imply
        # rollback succeeded or invite an automatic retry against another DB.
        if report["commit_attempted"] and not report["committed"]:
            report["status"] = "commit_outcome_unknown"
        code = 1
    report["finished_at"] = datetime.now(UTC).isoformat()
    payload = json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    if output:
        try:
            with output:
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
        except OSError:
            report["report_write_failed"] = True
            payload = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
            code = 1
    sys.stdout.write(payload)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
