"""Private, bounded exports and resumable tenant closure maintenance.

Exports contain a fixed metadata projection. Files are immutable chunks written
before a fenced DB commit, so a crash can orphan a chunk but cannot duplicate rows
or publish a stale worker's output. Financial tables are never purged.
"""

from __future__ import annotations

import asyncio
import csv
import fcntl
import hashlib
import io
import json
import os
import re
import stat
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from sqlalchemy import (
    String,
    and_,
    case,
    cast,
    delete,
    func,
    literal,
    or_,
    select,
    update,
)

from agent_platform.infrastructure import gateway_tables as gt
from agent_platform.infrastructure import operations_tables as ot
from agent_platform.infrastructure import tables as t
from agent_platform.infrastructure import tenancy_tables as nt
from agent_platform.infrastructure.errors import PlatformError
from agent_platform.modules.authorization import require_capability
from agent_platform.modules.billing.service import reservations_table

CALL_COLUMNS = (
    "id",
    "created_at",
    "model",
    "agent_name",
    "user_email",
    "status",
    "input_tokens",
    "output_tokens",
    "cost",
    "price_version",
    "finished_at",
)
RUN_COLUMNS = ("id", "created_at", "agent_name", "status", "finished_at")
ACTIVE_EXPORTS = ("queued", "running")
IDENTIFIER = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
FILENAME = re.compile(r"^[a-f0-9]{32}-[0-9]+-[a-f0-9]{32}\.csv$")


def _now():
    return datetime.now(UTC).isoformat()


def _id():
    return uuid4().hex


def _digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _cell(value):
    if value is None:
        return ""
    value = str(value)
    # CSV readers must not interpret untrusted names/aliases as formulas.
    if value.lstrip(" \t\r\n\x00").startswith(("=", "+", "-", "@")):
        value = "'" + value
    return value


class OperationsService:
    def __init__(self, platform):
        self.platform = platform
        self.db, self.settings = platform.db, platform.settings
        self.owner = _id()
        self.batch_size = max(
            1, min(1000, int(getattr(self.settings, "maintenance_batch_size", 100)))
        )
        self._tenant_cursor = 0
        self._orphan_scanners = {}

    def _audit(
        self, connection, actor, tenant_id, action, target, details=None, reason=""
    ):
        self.platform.identity._audit(
            connection, actor, action, target, tenant_id, reason, details
        )

    def _export_user(self, connection, user, scope, *, captured=None):
        current = self.platform.identity.assert_current(
            connection, user, "exports.create"
        )
        if scope == "tenant":
            require_capability(current, "usage.read_all")
        if captured and any(
            current[field] != captured[field]
            for field in ("membership_id", "membership_version", "identity_version")
        ):
            raise PlatformError(
                403, "export_access_revoked", "导出权限已变化，请重新申请。"
            )
        closure = connection.execute(
            select(ot.closures.c.status).where(
                ot.closures.c.tenant_id == current["tenant_id"],
            )
        ).scalar_one_or_none()
        if closure in ("purging", "deleted"):
            raise PlatformError(403, "tenant_deleting", "组织已进入内容清理阶段。")
        return current

    @staticmethod
    def _public_export(row):
        result = {
            key: row[key]
            for key in (
                "id",
                "kind",
                "scope",
                "status",
                "row_count",
                "created_at",
                "updated_at",
                "expires_at",
                "error_code",
                "cutoff",
            )
        }
        result["measurement_note"] = (
            "范围固定为申请时间前创建的记录；用量、费用和状态取各分页生成时的账务事实。"
            "后续结算不会改写已生成文件，需要重新申请导出。"
        )
        return result

    def request_export(self, user, kind="calls", scope="self", *, idempotency_key):
        if kind not in ("calls", "runs") or scope not in ("self", "tenant"):
            raise PlatformError(422, "invalid_export", "仅支持调用或运行元数据导出。")
        if kind == "runs" and scope != "self":
            raise PlatformError(
                403, "private_export_only", "运行列表仅可导出本人记录。"
            )
        if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 160:
            raise PlatformError(400, "idempotency_required", "请提供 Idempotency-Key。")
        tenant_id = user["tenant_id"]
        fingerprint = _digest(json.dumps([kind, scope]))
        with self.db.transaction("tenant:" + tenant_id) as connection:
            current = self._export_user(connection, user, scope)
            existing = (
                connection.execute(
                    select(ot.exports).where(
                        ot.exports.c.tenant_id == tenant_id,
                        ot.exports.c.requested_by == current["id"],
                        ot.exports.c.idempotency_key == idempotency_key,
                    )
                )
                .mappings()
                .first()
            )
            if existing:
                if existing["request_hash"] != fingerprint:
                    raise PlatformError(
                        409, "idempotency_conflict", "幂等键已用于其他导出参数。"
                    )
                return self._public_export(existing)
            maximum = connection.scalar(
                select(nt.entitlements.c.max_export_jobs).where(
                    nt.entitlements.c.tenant_id == tenant_id
                )
            )
            active = connection.scalar(
                select(func.count())
                .select_from(ot.exports)
                .where(
                    ot.exports.c.tenant_id == tenant_id,
                    ot.exports.c.status.in_(ACTIVE_EXPORTS),
                )
            )
            if maximum is None or active >= maximum:
                raise PlatformError(429, "export_limit", "组织的后台导出任务已达上限。")
            now = _now()
            result = {
                "id": _id(),
                "tenant_id": tenant_id,
                "requested_by": current["id"],
                "membership_id": current["membership_id"],
                "membership_version": current["membership_version"],
                "identity_version": current["identity_version"],
                "kind": kind,
                "scope": scope,
                "idempotency_key": idempotency_key,
                "request_hash": fingerprint,
                "status": "queued",
                "cutoff": now,
                "cursor_created_at": None,
                "cursor_id": None,
                "row_count": 0,
                "chunk_count": 0,
                "owner": None,
                "fence": 0,
                "lease_until": None,
                "expires_at": time.time()
                + max(60, int(getattr(self.settings, "export_ttl_seconds", 86400))),
                "error_code": "",
                "created_at": now,
                "updated_at": now,
            }
            connection.execute(ot.exports.insert().values(**result))
            self._audit(
                connection,
                current["id"],
                tenant_id,
                "export.requested",
                result["id"],
                {"kind": kind, "scope": scope},
            )
            return self._public_export(result)

    def list_exports(self, user):
        with self.db.transaction("tenant:" + user["tenant_id"]) as connection:
            self._export_user(connection, user, "self")
            rows = connection.execute(
                select(ot.exports)
                .where(
                    ot.exports.c.tenant_id == user["tenant_id"],
                    ot.exports.c.requested_by == user["id"],
                )
                .order_by(ot.exports.c.created_at.desc(), ot.exports.c.id)
                .limit(200)
            ).mappings()
            return [self._public_export(row) for row in rows]

    def _directory(self, tenant_id, *, create=False):
        if not IDENTIFIER.fullmatch(tenant_id):
            raise PlatformError(503, "export_storage_invalid", "组织存储标识无效。")
        root = Path(self.settings.export_directory).resolve()
        if root.is_relative_to(Path(self.settings.web_directory).resolve()):
            raise PlatformError(
                503, "export_storage_invalid", "导出存储不能位于公开静态文件目录。"
            )
        folder = root / tenant_id
        if folder.is_symlink():
            raise PlatformError(503, "export_storage_invalid", "导出存储路径不可用。")
        if create:
            root.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(root, 0o700)
            folder.mkdir(mode=0o700, exist_ok=True)
            os.chmod(folder, 0o700)
        return folder

    def _file(self, tenant_id, filename, *, create=False):
        if not FILENAME.fullmatch(filename):
            raise PlatformError(503, "export_storage_invalid", "导出文件标识无效。")
        return self._directory(tenant_id, create=create) / filename

    def _authorized_job(self, connection, user, job_id):
        row = (
            connection.execute(
                select(ot.exports).where(
                    ot.exports.c.tenant_id == user["tenant_id"],
                    ot.exports.c.id == job_id,
                    ot.exports.c.requested_by == user["id"],
                )
            )
            .mappings()
            .first()
        )
        if not row:
            raise PlatformError(404, "export_not_found", "导出任务不存在。")
        self._export_user(connection, user, row["scope"], captured=row)
        if row["status"] != "ready" or row["expires_at"] <= time.time():
            raise PlatformError(409, "export_not_ready", "导出未完成或文件已过期。")
        return dict(row)

    def download(self, user, job_id):
        with self.db.transaction("tenant:" + user["tenant_id"]) as connection:
            row = self._authorized_job(connection, user, job_id)
            self._audit(
                connection, user["id"], user["tenant_id"], "export.download", job_id
            )

        def chunks():
            for sequence in range(row["chunk_count"]):
                # Recheck during the download, including membership revocation.
                with self.db.transaction("tenant:" + user["tenant_id"]) as connection:
                    self._authorized_job(connection, user, job_id)
                    artifact = (
                        connection.execute(
                            select(ot.artifacts).where(
                                ot.artifacts.c.tenant_id == user["tenant_id"],
                                ot.artifacts.c.job_id == job_id,
                                ot.artifacts.c.sequence == sequence,
                            )
                        )
                        .mappings()
                        .first()
                    )
                    if not artifact:
                        raise PlatformError(410, "export_expired", "导出文件已清理。")
                path = self._file(user["tenant_id"], artifact["filename"])
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                with os.fdopen(fd, "rb") as stream:
                    info = os.fstat(stream.fileno())
                    if (
                        not stat.S_ISREG(info.st_mode)
                        or info.st_size != artifact["byte_count"]
                    ):
                        raise PlatformError(
                            503, "export_storage_invalid", "导出文件校验失败。"
                        )
                    data = stream.read()
                    if hashlib.sha256(data).hexdigest() != artifact["sha256"]:
                        raise PlatformError(
                            503, "export_storage_invalid", "导出文件校验失败。"
                        )
                    yield data

        return {"filename": row["kind"] + "-" + row["id"] + ".csv", "chunks": chunks()}

    def _claim_export(self, tenant_id):
        with self.db.transaction("tenant:" + tenant_id) as connection:
            connection.execute(
                update(ot.exports)
                .where(
                    ot.exports.c.tenant_id == tenant_id,
                    ot.exports.c.status == "running",
                    ot.exports.c.lease_until < time.time(),
                )
                .values(
                    status="queued", owner=None, lease_until=None, updated_at=_now()
                )
            )
            row = (
                connection.execute(
                    select(ot.exports)
                    .where(
                        ot.exports.c.tenant_id == tenant_id,
                        ot.exports.c.status == "queued",
                        ot.exports.c.expires_at > time.time(),
                    )
                    .order_by(ot.exports.c.updated_at, ot.exports.c.id)
                    .limit(1)
                )
                .mappings()
                .first()
            )
            if not row:
                return None
            result = dict(row)
            result.update(
                status="running",
                owner=self.owner,
                fence=row["fence"] + 1,
                lease_until=time.time() + 120,
                updated_at=_now(),
            )
            connection.execute(
                update(ot.exports).where(ot.exports.c.id == row["id"]).values(**result)
            )
            return result

    def _export_guard(self, connection, job):
        row = (
            connection.execute(
                select(ot.exports).where(
                    ot.exports.c.tenant_id == job["tenant_id"],
                    ot.exports.c.id == job["id"],
                )
            )
            .mappings()
            .one()
        )
        if (
            row["status"] != "running"
            or row["owner"] != self.owner
            or row["fence"] != job["fence"]
            or row["lease_until"] < time.time()
            or row["expires_at"] <= time.time()
        ):
            raise PlatformError(409, "export_lease_lost", "导出执行权已变化。")
        return row

    def _captured_user(self, connection, job):
        user = self.platform.identity._context(
            connection, job["requested_by"], job["tenant_id"], allow_suspended=True
        )
        return self._export_user(connection, user, job["scope"], captured=job)

    def _generate_export(self, job):
        with self.db.transaction("tenant:" + job["tenant_id"]) as connection:
            self._export_guard(connection, job)
            self._captured_user(connection, job)
            table, names = (
                (t.calls, CALL_COLUMNS)
                if job["kind"] == "calls"
                else (t.runs, RUN_COLUMNS)
            )
            columns = {name: table.c[name] for name in names}
            if job["kind"] == "calls":
                receipt = reservations_table
                columns.update(
                    {
                        "status": case(
                            (receipt.c.status == "settled", "confirmed"),
                            (receipt.c.status == "reserved", "running"),
                            (receipt.c.status.is_not(None), receipt.c.status),
                            (
                                t.calls.c.status.in_(("rejected", "admitting")),
                                t.calls.c.status,
                            ),
                            else_="pending_evidence",
                        ).label("status"),
                        "input_tokens": func.coalesce(receipt.c.input_tokens, 0).label(
                            "input_tokens"
                        ),
                        "output_tokens": func.coalesce(
                            receipt.c.output_tokens, 0
                        ).label("output_tokens"),
                        "cost": func.coalesce(
                            cast(receipt.c.cost, String), literal("0")
                        ).label("cost"),
                        "finished_at": case(
                            (
                                receipt.c.status.in_(
                                    ("settled", "released", "written_off")
                                ),
                                receipt.c.updated_at,
                            ),
                            else_=None,
                        ).label("finished_at"),
                    }
                )
            query = select(*(columns[name] for name in names)).where(
                table.c.tenant_id == job["tenant_id"],
                table.c.created_at <= job["cutoff"],
            )
            if job["kind"] == "calls":
                query = query.select_from(
                    t.calls.outerjoin(
                        reservations_table,
                        and_(
                            reservations_table.c.tenant_id == t.calls.c.tenant_id,
                            reservations_table.c.call_id == t.calls.c.id,
                        ),
                    )
                )
            if job["scope"] == "self":
                query = query.where(table.c.user_id == job["requested_by"])
            if job["cursor_created_at"]:
                query = query.where(
                    or_(
                        table.c.created_at > job["cursor_created_at"],
                        and_(
                            table.c.created_at == job["cursor_created_at"],
                            table.c.id > job["cursor_id"],
                        ),
                    )
                )
            rows = list(
                connection.execute(
                    query.order_by(table.c.created_at, table.c.id).limit(
                        self.batch_size
                    )
                ).mappings()
            )
        artifact = None
        if rows or job["chunk_count"] == 0:
            buffer = io.StringIO(newline="")
            writer = csv.writer(buffer)
            if job["chunk_count"] == 0:
                writer.writerow(names)
            writer.writerows([_cell(row[name]) for name in names] for row in rows)
            data = buffer.getvalue().encode("utf-8")
            filename = f"{job['id']}-{job['fence']}-{_id()}.csv"
            path = self._file(job["tenant_id"], filename, create=True)
            fd = os.open(
                path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
            )
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            artifact = {
                "id": _id(),
                "tenant_id": job["tenant_id"],
                "job_id": job["id"],
                "sequence": job["chunk_count"],
                "filename": filename,
                "sha256": hashlib.sha256(data).hexdigest(),
                "byte_count": len(data),
                "row_count": len(rows),
                "created_at": _now(),
            }
        try:
            with self.db.transaction("tenant:" + job["tenant_id"]) as connection:
                self._export_guard(connection, job)
                self._captured_user(connection, job)
                if artifact:
                    connection.execute(ot.artifacts.insert().values(**artifact))
                complete = len(rows) < self.batch_size
                values = {
                    "status": "ready" if complete else "queued",
                    "owner": None,
                    "lease_until": None,
                    "row_count": job["row_count"] + len(rows),
                    "chunk_count": job["chunk_count"] + bool(artifact),
                    "updated_at": _now(),
                }
                if rows:
                    values.update(
                        cursor_created_at=rows[-1]["created_at"],
                        cursor_id=rows[-1]["id"],
                    )
                connection.execute(
                    update(ot.exports)
                    .where(ot.exports.c.id == job["id"])
                    .values(**values)
                )
        except BaseException:
            if artifact:
                self._file(job["tenant_id"], artifact["filename"]).unlink(
                    missing_ok=True
                )
            raise

    def _expire_exports(self, tenant_id, *, all_jobs=False):
        with self.db.transaction("tenant:" + tenant_id) as connection:
            condition = ot.exports.c.tenant_id == tenant_id
            if not all_jobs:
                condition = and_(
                    condition,
                    or_(
                        ot.exports.c.expires_at <= time.time(),
                        ot.exports.c.status.in_(("failed", "expired", "cancelled")),
                    ),
                )
            condition = and_(
                condition,
                or_(
                    ot.exports.c.status != "expired",
                    select(ot.artifacts.c.id)
                    .where(
                        ot.artifacts.c.tenant_id == tenant_id,
                        ot.artifacts.c.job_id == ot.exports.c.id,
                    )
                    .exists(),
                ),
            )
            ids = list(
                connection.scalars(
                    select(ot.exports.c.id)
                    .where(condition)
                    .order_by(ot.exports.c.created_at)
                    .limit(self.batch_size)
                )
            )
            if not ids:
                return False
            connection.execute(
                update(ot.exports)
                .where(ot.exports.c.id.in_(ids), ot.exports.c.tenant_id == tenant_id)
                .values(
                    status="expired",
                    owner=None,
                    lease_until=None,
                    updated_at=_now(),
                )
            )
            rows = list(
                connection.execute(
                    select(ot.artifacts)
                    .where(
                        ot.artifacts.c.tenant_id == tenant_id,
                        ot.artifacts.c.job_id.in_(ids),
                    )
                    .order_by(ot.artifacts.c.id)
                    .limit(self.batch_size)
                ).mappings()
            )
        for row in rows:
            self._file(tenant_id, row["filename"]).unlink(missing_ok=True)
            with self.db.transaction("tenant:" + tenant_id) as connection:
                connection.execute(
                    delete(ot.artifacts).where(
                        ot.artifacts.c.tenant_id == tenant_id,
                        ot.artifacts.c.id == row["id"],
                    )
                )
        return bool(rows)

    def _cleanup_orphans(self, tenant_id):
        """A bounded directory iterator; never delete a live or referenced chunk."""
        root = Path(self.settings.export_directory).resolve()
        if not IDENTIFIER.fullmatch(tenant_id) or not (root / tenant_id).is_dir():
            return 0
        if (root / tenant_id).is_symlink():
            raise PlatformError(503, "export_storage_invalid", "导出目录不可用。")
        if tenant_id not in self._orphan_scanners:
            if len(self._orphan_scanners) >= 16:
                old = next(iter(self._orphan_scanners))
                self._orphan_scanners.pop(old).close()
            self._orphan_scanners[tenant_id] = os.scandir(root / tenant_id)
        iterator = self._orphan_scanners[tenant_id]
        cutoff = time.time() - max(
            3600, int(getattr(self.settings, "export_ttl_seconds", 86400))
        )
        removed = 0
        for _ in range(self.batch_size):
            entry = next(iterator, None)
            if entry is None:
                self._orphan_scanners.pop(tenant_id).close()
                break
            if not FILENAME.fullmatch(entry.name) or not entry.is_file(
                follow_symlinks=False
            ):
                continue
            try:
                if entry.stat(follow_symlinks=False).st_mtime >= cutoff:
                    continue
            except FileNotFoundError:
                continue
            with self.db.transaction("tenant:" + tenant_id) as connection:
                referenced = connection.scalar(
                    select(ot.artifacts.c.id)
                    .where(
                        ot.artifacts.c.tenant_id == tenant_id,
                        ot.artifacts.c.filename == entry.name,
                    )
                    .limit(1)
                )
            if not referenced:
                Path(entry.path).unlink(missing_ok=True)
                removed += 1
        return removed

    def close_tenant(self, actor, tenant_id, reason):
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 1000:
            raise PlatformError(
                422, "reason_required", "关闭组织须提供原因（最多 1000 字）。"
            )
        with self.db.transaction("tenant:" + tenant_id) as connection:
            self.platform.require_platform(connection, actor, "platform.tenants.manage")
            existing = (
                connection.execute(
                    select(ot.closures).where(ot.closures.c.tenant_id == tenant_id)
                )
                .mappings()
                .first()
            )
            if existing:
                return dict(existing)
            tenant = (
                connection.execute(
                    select(nt.tenant_settings).where(
                        nt.tenant_settings.c.tenant_id == tenant_id
                    )
                )
                .mappings()
                .first()
            )
            if not tenant or tenant["status"] == "deleted":
                raise PlatformError(404, "tenant_not_found", "组织不存在。")
            now = _now()
            connection.execute(
                update(nt.tenant_settings)
                .where(nt.tenant_settings.c.tenant_id == tenant_id)
                .values(
                    status="closing",
                    version=tenant["version"] + 1,
                    updated_at=now,
                    suspension_reason=reason.strip(),
                )
            )
            self.platform.identity._cancel_queued(connection, tenant_id)
            result = {
                "tenant_id": tenant_id,
                "id": _id(),
                "requested_by": actor["id"],
                "reason": reason.strip(),
                "status": "waiting",
                "retain_until": time.time()
                + max(30, int(getattr(self.settings, "deletion_retention_days", 30)))
                * 86400,
                "purge_phase": 0,
                "cursor_id": None,
                "owner": None,
                "fence": 0,
                "lease_until": None,
                "legacy_revocation_confirmed": False,
                "legacy_key_fingerprint": _digest(self.settings.litellm_key)
                if self.settings.litellm_key
                else None,
                "tombstone_written_at": None,
                "error_code": "",
                "created_at": now,
                "updated_at": now,
                "finished_at": None,
            }
            connection.execute(ot.closures.insert().values(**result))
            self._audit(
                connection,
                actor["id"],
                tenant_id,
                "tenant.close_requested",
                result["id"],
                reason=reason.strip(),
            )
        try:
            self.platform.gateway.revoke(tenant_id, actor_id=actor["id"])
        except PlatformError:
            # Durable closure records authorize the dedicated system revoker.
            pass
        return result

    def lifecycle_status(self, actor, tenant_id):
        with self.db.transaction("tenant:" + tenant_id) as connection:
            self.platform.require_platform(connection, actor, "platform.tenants.manage")
            row = (
                connection.execute(
                    select(ot.closures).where(ot.closures.c.tenant_id == tenant_id)
                )
                .mappings()
                .first()
            )
            if not row:
                raise PlatformError(404, "closure_not_found", "组织没有关闭任务。")
            return dict(row)

    def _append_tombstone(self, job):
        raw_path = getattr(self.settings, "tombstone_path", None)
        if not raw_path:
            raise PlatformError(
                503, "tombstone_storage_required", "未配置外部删除记录存储。"
            )
        path = Path(raw_path).absolute()
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(
            path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600
        )
        with os.fdopen(fd, "ab") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise PlatformError(
                    503, "tombstone_storage_invalid", "删除记录必须为普通文件。"
                )
            os.fchmod(stream.fileno(), 0o600)
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            payload = {
                "version": 1,
                "tenant_id": job["tenant_id"],
                "operation_id": job["id"],
                "action": "delete_tenant_content",
                "recorded_at": _now(),
                "legacy_key_fingerprint": job["legacy_key_fingerprint"],
            }
            stream.write((json.dumps(payload, sort_keys=True) + "\n").encode())
            stream.flush()
            os.fsync(stream.fileno())
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return payload["recorded_at"]

    def _closure_guard(self, connection, job):
        row = (
            connection.execute(
                select(ot.closures).where(ot.closures.c.tenant_id == job["tenant_id"])
            )
            .mappings()
            .one()
        )
        if (
            row["owner"] != self.owner
            or row["fence"] != job["fence"]
            or row["lease_until"] < time.time()
        ):
            raise PlatformError(409, "closure_lease_lost", "关闭任务执行权已变化。")
        return row

    def _claim_closure(self, tenant_id):
        with self.db.transaction("tenant:" + tenant_id) as connection:
            row = (
                connection.execute(
                    select(ot.closures).where(
                        ot.closures.c.tenant_id == tenant_id,
                        ot.closures.c.status != "deleted",
                    )
                )
                .mappings()
                .first()
            )
            if not row or (
                row["owner"]
                and row["lease_until"]
                and row["lease_until"] >= time.time()
            ):
                return None
            result = dict(row)
            result.update(
                owner=self.owner, fence=row["fence"] + 1, lease_until=time.time() + 120
            )
            connection.execute(
                update(ot.closures)
                .where(ot.closures.c.tenant_id == tenant_id)
                .values(**result)
            )
            return result

    def _release_closure(self, job, **values):
        with self.db.transaction("tenant:" + job["tenant_id"]) as connection:
            self._closure_guard(connection, job)
            connection.execute(
                update(ot.closures)
                .where(ot.closures.c.tenant_id == job["tenant_id"])
                .values(
                    owner=None,
                    lease_until=None,
                    updated_at=_now(),
                    **values,
                )
            )

    def _closure_blocker(self, connection, job):
        tenant_id = job["tenant_id"]
        if connection.scalar(
            select(func.count())
            .select_from(reservations_table)
            .where(
                reservations_table.c.tenant_id == tenant_id,
                reservations_table.c.status.in_(("reserved", "unresolved")),
            )
        ):
            return "billing_unresolved"
        if connection.scalar(
            select(func.count())
            .select_from(t.runs)
            .where(
                t.runs.c.tenant_id == tenant_id,
                t.runs.c.status.in_(("queued", "running", "cancelling")),
            )
        ):
            return "runs_pending"
        binding = (
            connection.execute(
                select(gt.bindings).where(gt.bindings.c.tenant_id == tenant_id)
            )
            .mappings()
            .first()
        )
        if binding:
            if binding["mode"] == "legacy" and not job["legacy_revocation_confirmed"]:
                return "legacy_gateway_revocation_required"
            if binding["status"] != "revoked":
                return "gateway_revocation_pending"
        if connection.scalar(
            select(func.count())
            .select_from(gt.credentials)
            .where(
                gt.credentials.c.tenant_id == tenant_id,
                gt.credentials.c.status != "revoked",
            )
        ):
            return "gateway_revocation_pending"
        return None

    @staticmethod
    def _redacted_spec(spec):
        if not isinstance(spec, dict):
            return {"content_deleted": True}
        result = {
            key: spec[key]
            for key in (
                "version",
                "model_id",
                "deployment_id",
                "route",
                "price_version_id",
                "max_tokens",
                "max_steps",
                "temperature",
                "snapshot_schema_version",
            )
            if key in spec
        }
        if isinstance(spec.get("model"), dict):
            result["model"] = {
                key: spec["model"][key]
                for key in (
                    "id",
                    "tenant_id",
                    "alias",
                    "input_price",
                    "output_price",
                    "price_version",
                    "context_window",
                    "max_output_tokens",
                    "deployment_id",
                    "route",
                )
                if key in spec["model"]
            }
        result["content_deleted"] = True
        return result

    @staticmethod
    def _redacted_financial_evidence(row):
        """Keep accounted quantities and provenance, never arbitrary response data.

        The monetary columns, ledger, actors and reconciliation audit stay intact.
        A digest retains correlation to the original evidence without retaining a
        provider's extra fields, which may contain prompts or response text.
        """
        original = row["raw_usage"]
        if not original:
            return None
        try:
            parsed = json.loads(original)
        except (TypeError, ValueError):
            parsed = {}
        source = "platform_billing_record"
        original_hash = _digest(original)
        deleted_at = _now()
        if isinstance(parsed, dict):
            if parsed.get("usage_source") == "litellm_gateway":
                source = "litellm_gateway"
            elif (
                parsed.get("source") == "manual_reconciliation"
                or parsed.get("usage_source") == "manual_reconciliation"
            ):
                source = "manual_reconciliation"
            normalized_fields = {
                "evidence_schema",
                "content_deleted",
                "content_deleted_at",
                "original_sha256",
                "usage_source",
                "billing_status",
                "prompt_tokens",
                "completion_tokens",
            }
            if (
                parsed.get("evidence_schema") == "billing-normalized/v1"
                and parsed.get("content_deleted") is True
                and not set(parsed) - normalized_fields
                and re.fullmatch(
                    r"[0-9a-f]{64}", str(parsed.get("original_sha256", ""))
                )
            ):
                original_hash = parsed["original_sha256"]
                try:
                    deleted_at = datetime.fromisoformat(
                        parsed["content_deleted_at"]
                    ).isoformat()
                except (ValueError, TypeError, KeyError):
                    pass
        evidence = {
            "evidence_schema": "billing-normalized/v1",
            "content_deleted": True,
            "content_deleted_at": deleted_at,
            "original_sha256": original_hash,
            "usage_source": source,
            "billing_status": row["status"],
        }
        for column, key in (
            ("input_tokens", "prompt_tokens"),
            ("output_tokens", "completion_tokens"),
        ):
            if row[column] is not None:
                evidence[key] = row[column]
        return json.dumps(evidence, sort_keys=True)

    def _purge_page(self, job):
        tenant_id, phase = job["tenant_id"], job["purge_phase"]
        # Financial evidence is normalized in the final phase; financial amounts,
        # reservations, identifiers, transaction entries and audit are retained.
        phases = (
            t.messages,
            t.sessions,
            t.runs,
            t.run_events,
            t.agents,
            t.agent_versions,
            t.calls,
            nt.invitations,
            reservations_table,
        )
        if phase == 0:
            self._expire_exports(tenant_id, all_jobs=True)
            with self.db.transaction("tenant:" + tenant_id) as connection:
                self._closure_guard(connection, job)
                if connection.scalar(
                    select(func.count())
                    .select_from(ot.artifacts)
                    .where(ot.artifacts.c.tenant_id == tenant_id)
                ):
                    self._release_closure_after(connection, job, status="purging")
                    return
                # Old staged files are inaccessible; clean any unreferenced chunks.
            folder = self._directory(tenant_id, create=True)
            removed = 0
            with os.scandir(folder) as entries:
                for entry in entries:
                    if FILENAME.fullmatch(entry.name):
                        Path(entry.path).unlink(missing_ok=True)
                        removed += 1
                        if removed >= self.batch_size:
                            break
            self._release_closure(
                job, status="purging", purge_phase=0 if removed else 1, cursor_id=None
            )
            return
        if phase <= len(phases):
            table = phases[phase - 1]
            # Composite-key tables use an offset only after marking each row;
            # selecting untouched rows avoids unstable cross-engine pagination.
            with self.db.transaction("tenant:" + tenant_id) as connection:
                self._closure_guard(connection, job)
                query = select(table).where(table.c.tenant_id == tenant_id)
                if table is t.run_events:
                    # sequence is stable; cursor encodes run ID + sequence.
                    if job["cursor_id"]:
                        run_id, sequence = job["cursor_id"].rsplit(":", 1)
                        query = query.where(
                            or_(
                                table.c.run_id > run_id,
                                and_(
                                    table.c.run_id == run_id,
                                    table.c.sequence > int(sequence),
                                ),
                            )
                        )
                    query = query.order_by(table.c.run_id, table.c.sequence)
                elif table is t.agent_versions:
                    if job["cursor_id"]:
                        agent_id, version = job["cursor_id"].rsplit(":", 1)
                        query = query.where(
                            or_(
                                table.c.agent_id > agent_id,
                                and_(
                                    table.c.agent_id == agent_id,
                                    table.c.version > int(version),
                                ),
                            )
                        )
                    query = query.order_by(table.c.agent_id, table.c.version)
                else:
                    if job["cursor_id"]:
                        query = query.where(table.c.id > job["cursor_id"])
                    query = query.order_by(table.c.id)
                rows = list(connection.execute(query.limit(self.batch_size)).mappings())
                cursor = None
                for row in rows:
                    if table is t.run_events:
                        where = and_(
                            table.c.run_id == row["run_id"],
                            table.c.sequence == row["sequence"],
                        )
                        values, cursor = (
                            {"data": {"content_deleted": True}},
                            f"{row['run_id']}:{row['sequence']}",
                        )
                    elif table is t.agent_versions:
                        where = and_(
                            table.c.agent_id == row["agent_id"],
                            table.c.version == row["version"],
                        )
                        values, cursor = (
                            {"spec": self._redacted_spec(row["spec"])},
                            f"{row['agent_id']}:{row['version']}",
                        )
                    else:
                        where, cursor = table.c.id == row["id"], row["id"]
                        if table is t.messages:
                            values = {"content": "[内容已删除]"}
                        elif table is t.sessions:
                            values = {"title": "[已删除]"}
                        elif table is t.runs:
                            values = {
                                "message": "",
                                "spec": self._redacted_spec(row["spec"]),
                                "error": "",
                            }
                        elif table is t.agents:
                            values = {
                                "system_prompt": "",
                                "description": "",
                                "tools": [],
                            }
                        elif table is t.calls:
                            values = {
                                "error": "",
                                "raw_usage": None,
                                "user_email": "deleted+"
                                + _digest(row["user_id"])[:16]
                                + "@invalid.invalid",
                            }
                        elif table is reservations_table:
                            values = {
                                "raw_usage": self._redacted_financial_evidence(row)
                            }
                        else:
                            values = {
                                "email": "deleted+" + row["id"] + "@invalid.invalid",
                                "token_hash": _digest(_id()),
                                "revoked_at": _now(),
                            }
                    connection.execute(
                        update(table)
                        .where(where, table.c.tenant_id == tenant_id)
                        .values(**values)
                    )
                self._release_closure_after(
                    connection,
                    job,
                    status="purging",
                    purge_phase=phase if len(rows) == self.batch_size else phase + 1,
                    cursor_id=cursor if len(rows) == self.batch_size else None,
                    error_code="",
                )
            return
        with self.db.transaction("tenant:" + tenant_id) as connection:
            self._closure_guard(connection, job)
            blocker = self._closure_blocker(connection, job)
            if blocker:
                self._release_closure_after(connection, job, error_code=blocker)
                return
            connection.execute(
                update(nt.memberships)
                .where(nt.memberships.c.tenant_id == tenant_id)
                .values(
                    status="revoked",
                    authz_version=nt.memberships.c.authz_version + 1,
                    revoked_at=_now(),
                )
            )
            connection.execute(
                update(nt.tenant_settings)
                .where(nt.tenant_settings.c.tenant_id == tenant_id)
                .values(
                    status="deleted",
                    version=nt.tenant_settings.c.version + 1,
                    updated_at=_now(),
                )
            )
            self._release_closure_after(
                connection, job, status="deleted", error_code="", finished_at=_now()
            )
            self._audit(
                connection,
                None,
                tenant_id,
                "tenant.content_deleted",
                job["id"],
                {"financial_records_retained": True, "raw_evidence_normalized": True},
            )

    def _release_closure_after(self, connection, job, **values):
        connection.execute(
            update(ot.closures)
            .where(ot.closures.c.tenant_id == job["tenant_id"])
            .values(
                owner=None,
                lease_until=None,
                updated_at=_now(),
                **values,
            )
        )

    def _process_closure(self, job):
        with self.db.transaction("tenant:" + job["tenant_id"]) as connection:
            self._closure_guard(connection, job)
            gateway_status = connection.scalar(
                select(gt.bindings.c.status).where(
                    gt.bindings.c.tenant_id == job["tenant_id"]
                )
            )
        if gateway_status and gateway_status not in ("revoked", "revoking"):
            # Revocation proceeds even when unknown costs delay content erasure.
            self.platform.gateway.revoke_for_closure(job["tenant_id"])
        with self.db.transaction("tenant:" + job["tenant_id"]) as connection:
            self._closure_guard(connection, job)
            blocker = self._closure_blocker(connection, job)
        if blocker:
            if blocker == "gateway_revocation_pending":
                self.platform.gateway.revoke_for_closure(job["tenant_id"])
            self._release_closure(job, status="waiting", error_code=blocker)
            return
        if time.time() < job["retain_until"]:
            self._release_closure(job, status="retaining", error_code="")
            return
        if not job["tombstone_written_at"]:
            try:
                recorded = self._append_tombstone(job)
            except OSError:
                raise PlatformError(
                    503,
                    "tombstone_write_failed",
                    "外部删除记录未能可靠写入，内容清理已停止。",
                ) from None
            self._release_closure(
                job, status="purging", tombstone_written_at=recorded, error_code=""
            )
            return
        self._purge_page(job)

    def confirm_legacy_revocation(self, tenant_id):
        """Privileged maintenance command; actual remote read-back, no UI assertion."""
        with self.db.transaction("tenant:" + tenant_id) as connection:
            job = (
                connection.execute(
                    select(ot.closures).where(ot.closures.c.tenant_id == tenant_id)
                )
                .mappings()
                .first()
            )
            binding = (
                connection.execute(
                    select(gt.bindings).where(
                        gt.bindings.c.tenant_id == tenant_id,
                        gt.bindings.c.mode == "legacy",
                    )
                )
                .mappings()
                .first()
            )
            if not job or not binding or binding["status"] != "revoked":
                raise PlatformError(
                    409,
                    "legacy_confirmation_invalid",
                    "组织未处于 legacy 凭据关闭流程。",
                )
        fingerprint = job["legacy_key_fingerprint"]
        if (
            not fingerprint
            or self.platform.gateway._control().key_info(fingerprint) is not None
        ):
            raise PlatformError(
                409,
                "legacy_key_still_active",
                "旧运行 Key 尚未在网关撤销，或无法核实。",
            )
        with self.db.transaction("tenant:" + tenant_id) as connection:
            connection.execute(
                update(ot.closures)
                .where(ot.closures.c.tenant_id == tenant_id)
                .values(legacy_revocation_confirmed=True, updated_at=_now())
            )
            self._audit(
                connection,
                None,
                tenant_id,
                "tenant.legacy_revocation_verified",
                job["id"],
            )
        return {"tenant_id": tenant_id, "legacy_revocation_confirmed": True}

    def replay_tombstones(self):
        """Reapply deletion restrictions after an operator restores a backup.

        Does not restore any backup, overwrite a ledger, or restart model calls.
        A malformed tombstone aborts replay, so restoration must remain offline.
        """
        path = Path(self.settings.tombstone_path)
        restored = 0
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "r") as stream:
            for line in stream:
                try:
                    event = json.loads(line)
                    tenant_id = event["tenant_id"]
                    if (
                        event["version"] != 1
                        or event["action"] != "delete_tenant_content"
                        or not IDENTIFIER.fullmatch(tenant_id)
                    ):
                        raise ValueError
                except (ValueError, KeyError, TypeError):
                    raise PlatformError(
                        503, "tombstone_invalid", "删除记录无效，恢复环境必须保持离线。"
                    ) from None
                with self.db.transaction("tenant:" + tenant_id) as connection:
                    tenant = (
                        connection.execute(
                            select(nt.tenant_settings).where(
                                nt.tenant_settings.c.tenant_id == tenant_id
                            )
                        )
                        .mappings()
                        .first()
                    )
                    if not tenant:
                        continue
                    connection.execute(
                        update(nt.tenant_settings)
                        .where(nt.tenant_settings.c.tenant_id == tenant_id)
                        .values(
                            status="deleted",
                            version=tenant["version"] + 1,
                            updated_at=_now(),
                        )
                    )
                    self.platform.identity._cancel_queued(connection, tenant_id)
                    existing = (
                        connection.execute(
                            select(ot.closures).where(
                                ot.closures.c.tenant_id == tenant_id
                            )
                        )
                        .mappings()
                        .first()
                    )
                    values = {
                        "status": "purging",
                        "retain_until": 0,
                        "purge_phase": 0,
                        "cursor_id": None,
                        "owner": None,
                        "lease_until": None,
                        "tombstone_written_at": event["recorded_at"],
                        "error_code": "restore_requires_gateway_review",
                        "updated_at": _now(),
                        "legacy_key_fingerprint": event.get("legacy_key_fingerprint"),
                    }
                    if existing:
                        connection.execute(
                            update(ot.closures)
                            .where(ot.closures.c.tenant_id == tenant_id)
                            .values(**values)
                        )
                    else:
                        connection.execute(
                            ot.closures.insert().values(
                                tenant_id=tenant_id,
                                id=event["operation_id"],
                                requested_by="restore-operator",
                                reason="恢复后重放外部删除记录",
                                fence=0,
                                legacy_revocation_confirmed=False,
                                created_at=_now(),
                                finished_at=None,
                                **values,
                            )
                        )
                    self._audit(
                        connection,
                        None,
                        tenant_id,
                        "tenant.tombstone_replayed",
                        event["operation_id"],
                    )
                    restored += 1
        return {"replayed": restored, "external_calls_enabled": False}

    def process_batch(self):
        result = {"exports": 0, "closures": 0, "cleanup": 0, "errors": 0}
        with self.db.read() as connection:
            tenants = list(
                connection.scalars(select(t.tenants.c.id).order_by(t.tenants.c.id))
            )
        if not tenants:
            return result
        start = self._tenant_cursor % len(tenants)
        count = min(len(tenants), self.batch_size)
        for offset in range(count):
            tenant_id = tenants[(start + offset) % len(tenants)]
            try:
                result["cleanup"] += bool(self._expire_exports(tenant_id))
                result["cleanup"] += self._cleanup_orphans(tenant_id)
                job = self._claim_export(tenant_id)
                if job:
                    try:
                        self._generate_export(job)
                    except (PlatformError, OSError) as exc:
                        with self.db.transaction("tenant:" + tenant_id) as connection:
                            current = (
                                connection.execute(
                                    select(ot.exports).where(
                                        ot.exports.c.id == job["id"]
                                    )
                                )
                                .mappings()
                                .one()
                            )
                            if (
                                current["owner"] == self.owner
                                and current["fence"] == job["fence"]
                            ):
                                connection.execute(
                                    update(ot.exports)
                                    .where(ot.exports.c.id == job["id"])
                                    .values(
                                        status="failed",
                                        error_code=getattr(
                                            exc, "code", "export_storage_unavailable"
                                        ),
                                        owner=None,
                                        lease_until=None,
                                        updated_at=_now(),
                                    )
                                )
                    result["exports"] += 1
                closure = self._claim_closure(tenant_id)
                if closure:
                    try:
                        self._process_closure(closure)
                    except (PlatformError, OSError) as exc:
                        self._release_closure(
                            closure,
                            error_code=getattr(
                                exc, "code", "closure_storage_unavailable"
                            ),
                        )
                    result["closures"] += 1
            except Exception:  # noqa: BLE001 - isolate failed durable jobs without exposing secret-bearing exceptions
                # No exception repr: database and HTTP objects can contain secrets.
                result["errors"] += 1
        self._tenant_cursor = start + count
        return result

    async def serve(self, stop: asyncio.Event):
        interval = max(0.2, float(getattr(self.settings, "maintenance_interval", 30)))
        try:
            while not stop.is_set():
                result = await asyncio.to_thread(self.process_batch)
                delay = (
                    min(interval, 0.2)
                    if result["exports"] or result["cleanup"]
                    else interval
                )
                try:
                    await asyncio.wait_for(stop.wait(), timeout=delay)
                except TimeoutError:
                    pass
        finally:
            for iterator in self._orphan_scanners.values():
                iterator.close()
            self._orphan_scanners.clear()
