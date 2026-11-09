"""Distributed member admission; task leases never create ordinary session runs."""

import asyncio
import time
from contextlib import suppress

from sqlalchemy import and_, func, insert, or_, select, update

from agent_platform.infrastructure import collaboration_tables as ct
from agent_platform.infrastructure import tables as t
from agent_platform.infrastructure.errors import PlatformError
from agent_platform.infrastructure.journal_tables import journal
from agent_platform.infrastructure.task_lease_tables import leases
from agent_platform.modules.agent_registry.contracts import AgentDeclaration
from agent_platform.modules.run_records import append_event, now, uid
from agent_platform.modules.runtime.context import ExecutionContext
from agent_platform.modules.runtime.contracts import canonical_digest
from agent_platform.modules.runtime.engine import RunCancelled
from agent_platform.modules.runtime.invocation import InvocationService
from agent_platform.modules.runtime.journal import EvidenceCodec

from . import artifacts
from .context import messages_for_task, task_input


def active_members(tenant_id):
    """Shared capacity for local and distributed members under the tenant lock."""
    stamp = time.time()
    local = and_(
        leases.c.task_id.is_(None),
        ct.executions.c.owner == t.runs.c.owner,
        ct.executions.c.fence == t.runs.c.fence,
    )
    distributed = and_(
        leases.c.execution_id == ct.executions.c.id,
        leases.c.root_fence == t.runs.c.fence,
        leases.c.owner == ct.executions.c.owner,
        leases.c.fence == ct.executions.c.fence,
        leases.c.lease_until > stamp,
    )
    return (
        select(func.count())
        .select_from(
            ct.executions.join(
                t.runs,
                and_(
                    t.runs.c.tenant_id == ct.executions.c.tenant_id,
                    t.runs.c.id == ct.executions.c.root_run_id,
                ),
            ).outerjoin(
                leases,
                and_(
                    leases.c.tenant_id == ct.executions.c.tenant_id,
                    leases.c.root_run_id == ct.executions.c.root_run_id,
                    leases.c.task_id == ct.executions.c.task_id,
                ),
            )
        )
        .where(
            ct.executions.c.tenant_id == tenant_id,
            ct.executions.c.role == "member",
            ct.executions.c.status == "running",
            t.runs.c.status.in_(("running", "cancelling")),
            t.runs.c.lease_until > stamp,
            or_(local, distributed),
        )
    )


def dispatch(scheduler, task):
    """Only the root owner releases dependencies; member workers only claim ready work."""
    run = scheduler.run
    with scheduler.db.transaction(f"tenant:{run['tenant_id']}") as connection:
        scheduler.guard(connection)
        changed = connection.execute(
            update(ct.tasks)
            .where(
                ct.tasks.c.id == task["id"],
                ct.tasks.c.status == "pending",
            )
            .values(status="ready", revision=ct.tasks.c.revision + 1)
        )
        if changed.rowcount != 1:
            return
        prior = (
            connection.execute(select(leases).where(leases.c.task_id == task["id"]))
            .mappings()
            .first()
        )
        if prior:
            # A root takeover invalidates every former child capability. Keep
            # the original task deadline and evidence; claim validates reuse.
            connection.execute(
                update(leases)
                .where(leases.c.task_id == task["id"])
                .values(
                    owner=None,
                    lease_until=None,
                    root_fence=run["fence"],
                    fence=prior["fence"] + 1,
                    ready_at=time.time(),
                )
            )
        else:
            connection.execute(
                insert(leases).values(
                    task_id=task["id"],
                    tenant_id=run["tenant_id"],
                    root_run_id=run["id"],
                    root_fence=run["fence"],
                    owner=None,
                    fence=0,
                    ready_at=time.time(),
                )
            )
        append_event(
            connection,
            run["id"],
            "task.updated",
            {"task_id": task["id"], "status": "ready", "error": ""},
        )


def fail_task(connection, task, error):
    """Called under tenant admission lock after expiry or an explicit claim failure."""
    for execution in connection.execute(
        select(ct.executions).where(
            ct.executions.c.tenant_id == task["tenant_id"],
            ct.executions.c.task_id == task["id"],
            ct.executions.c.status == "running",
        )
    ).mappings():
        connection.execute(
            update(ct.executions)
            .where(ct.executions.c.id == execution["id"])
            .values(
                status="failed",
                error=error,
                finished_at=now(),
            )
        )
        append_event(
            connection,
            task["root_run_id"],
            "execution.failed",
            {
                "task_id": task["id"],
                "execution_id": execution["id"],
                "status": "failed",
                "error": error,
            },
        )
    connection.execute(
        update(ct.tasks)
        .where(ct.tasks.c.id == task["id"])
        .values(
            status="failed",
            error=error,
            revision=ct.tasks.c.revision + 1,
        )
    )
    connection.execute(
        update(leases)
        .where(leases.c.task_id == task["id"])
        .values(
            owner=None,
            lease_until=None,
            fence=leases.c.fence + 1,
        )
    )
    append_event(
        connection,
        task["root_run_id"],
        "task.updated",
        {"task_id": task["id"], "status": "failed", "error": error},
    )


def verify_resume(worker, connection, root, lease):
    if not (
        root["spec"]["team_snapshot"]["features"].get("recovery")
        and worker.settings.collaboration_recovery_enabled
        and lease["recovery_count"] < 3
    ):
        raise PlatformError(
            409, "task_recovery_disabled", "成员执行失联，当前不允许接管。"
        )
    codec = EvidenceCodec(worker.settings.secret_encryption_key)
    for evidence in connection.execute(
        select(journal).where(
            journal.c.tenant_id == root["tenant_id"],
            journal.c.execution_id == lease["execution_id"],
        )
    ).mappings():
        codec.decode(evidence["request_ciphertext"], evidence["request_digest"])
        if evidence["state"] == "confirmed":
            codec.decode(evidence["result_ciphertext"], evidence["result_digest"])
        elif not (
            evidence["state"] == "prepared"
            or evidence["state"] in {"sent", "unknown"}
            and (evidence["effect"] == "pure" or evidence["kind"] == "remote")
        ):
            raise PlatformError(
                409, "task_output_unknown", "成员调用缺少完整结果证据，已停止且未重发。"
            )


class TaskLeaseQueue:
    def __init__(self, worker):
        self.worker, self.db = worker, worker.db
        self.cursor = ""

    def claim(self, *, root_ids=None):
        if not self.worker.settings.collaboration_distributed_enabled:
            return None
        for tenant_id in self.worker._tenant_page(self.cursor, active=True, limit=128):
            self.cursor = tenant_id
            with (
                self.db.tenant_scope(tenant_id),
                self.db.transaction(f"tenant:{tenant_id}") as connection,
            ):
                stamp = time.time()
                candidates = (
                    connection.execute(
                        select(ct.tasks)
                        .join(
                            leases,
                            leases.c.task_id == ct.tasks.c.id,
                        )
                        .join(
                            t.runs,
                            and_(
                                t.runs.c.id == ct.tasks.c.root_run_id,
                                t.runs.c.tenant_id == ct.tasks.c.tenant_id,
                            ),
                        )
                        .where(
                            ct.tasks.c.tenant_id == tenant_id,
                            ct.tasks.c.root_run_id.in_(root_ids)
                            if root_ids is not None
                            else True,
                            ct.tasks.c.status.in_(
                                ("ready", "waiting_capacity", "running")
                            ),
                            t.runs.c.status == "running",
                            t.runs.c.cancel_requested.is_(False),
                            t.runs.c.lease_until > stamp,
                            or_(t.runs.c.deadline.is_(None), t.runs.c.deadline > stamp),
                            leases.c.root_fence == t.runs.c.fence,
                            or_(
                                leases.c.lease_until.is_(None),
                                leases.c.lease_until <= stamp,
                            ),
                        )
                        .order_by(leases.c.ready_at, ct.tasks.c.id)
                        .limit(32)
                    )
                    .mappings()
                    .all()
                )
                for row in candidates:
                    task = dict(row)
                    root = dict(
                        connection.execute(
                            select(t.runs).where(t.runs.c.id == task["root_run_id"])
                        )
                        .mappings()
                        .one()
                    )
                    lease = dict(
                        connection.execute(
                            select(leases).where(leases.c.task_id == task["id"])
                        )
                        .mappings()
                        .one()
                    )
                    if (
                        not root["spec"]
                        .get("team_snapshot", {})
                        .get("features", {})
                        .get("distributed")
                    ):
                        continue
                    try:
                        claim = self._claim(connection, root, task, lease)
                        if claim:
                            return claim
                    except PlatformError as exc:
                        fail_task(connection, task, exc.message)
                    # Capacity-only waits are returned as None without any model hold.
        return None

    def _claim(self, connection, root, task, lease):
        from .planner import validate_plan

        snapshot = root["spec"]["team_snapshot"]
        team = validate_plan(snapshot)
        stamp = time.time()
        spec = snapshot["members"][task["member_key"]]["spec"]
        self.worker.invocation._authorize_identity(
            connection, {**root, "spec": spec, "root_spec": root["spec"]}
        )
        previous = None
        if lease["execution_id"]:
            previous = dict(
                connection.execute(
                    select(ct.executions).where(
                        ct.executions.c.id == lease["execution_id"]
                    )
                )
                .mappings()
                .one()
            )
            if previous["status"] != "running" or previous["spec"] != spec:
                raise PlatformError(
                    409, "task_execution_conflict", "成员执行状态不允许接管。"
                )
            verify_resume(self.worker, connection, root, lease)
        if lease["deadline"] is not None and lease["deadline"] <= stamp:
            raise PlatformError(409, "task_expired", "成员任务执行期限已到。")
        if (
            not previous or task["status"] in ("ready", "waiting_capacity")
        ) and stamp - lease[
            "ready_at"
        ] >= self.worker.settings.collaboration_capacity_wait_seconds:
            raise PlatformError(
                429, "member_capacity_timeout", "等待协作成员容量超时。"
            )
        active = active_members(root["tenant_id"])
        if (
            connection.scalar(active)
            >= self.worker.settings.collaboration_max_active_members
            or connection.scalar(
                active.where(ct.executions.c.root_run_id == root["id"])
            )
            >= team.max_parallel
        ):
            if task["status"] != "waiting_capacity":
                if previous and task["status"] == "running":
                    connection.execute(
                        update(leases)
                        .where(leases.c.task_id == task["id"])
                        .values(ready_at=stamp)
                    )
                connection.execute(
                    update(ct.tasks)
                    .where(ct.tasks.c.id == task["id"])
                    .values(status="waiting_capacity", revision=ct.tasks.c.revision + 1)
                )
                append_event(
                    connection,
                    root["id"],
                    "task.updated",
                    {"task_id": task["id"], "status": "waiting_capacity", "error": ""},
                )
            return None
        contract = next(
            c
            for c in AgentDeclaration.from_spec(spec).task_contracts
            if c.task_type == task["task_type"]
        )
        if previous:
            messages = previous["input"]
        else:
            dependencies = [
                dict(row)
                for row in connection.execute(
                    select(ct.tasks).where(
                        ct.tasks.c.tenant_id == root["tenant_id"],
                        ct.tasks.c.root_run_id == root["id"],
                        ct.tasks.c.logical_key.in_(task["dependencies"]),
                    )
                ).mappings()
            ]
            outputs = artifacts.dependency_outputs(connection, root, dependencies)
            messages = messages_for_task(
                root, task, contract, task_input(root["message"], contract, outputs)
            )
        deadline = lease["deadline"]
        if not previous:
            limit = spec.get("execution_limits", {}).get("timeout_seconds")
            deadline = stamp + limit if limit else root["deadline"]
            if root["deadline"] is not None:
                deadline = (
                    min(deadline, root["deadline"]) if deadline else root["deadline"]
                )
        identifier = previous["id"] if previous else uid()
        changes = {
            "owner": self.worker.owner,
            "fence": lease["fence"] + 1,
            "lease_until": min(stamp + 20, deadline) if deadline else stamp + 20,
            "deadline": deadline,
            "execution_id": identifier,
            "recovery_count": lease["recovery_count"] + bool(previous),
        }
        if previous:
            connection.execute(
                update(ct.executions)
                .where(ct.executions.c.id == identifier)
                .values(owner=changes["owner"], fence=changes["fence"])
            )
            connection.execute(
                update(journal)
                .where(
                    journal.c.tenant_id == root["tenant_id"],
                    journal.c.execution_id == identifier,
                )
                .values(
                    owner=changes["owner"],
                    fence=changes["fence"],
                    revision=journal.c.revision + 1,
                    updated_at=now(),
                )
            )
        else:
            connection.execute(
                insert(ct.executions).values(
                    id=identifier,
                    tenant_id=root["tenant_id"],
                    root_run_id=root["id"],
                    task_id=task["id"],
                    attempt=1,
                    role="member",
                    agent_id=spec["id"],
                    agent_version=spec["version"],
                    spec=spec,
                    input=messages,
                    input_digest=canonical_digest(messages),
                    status="running",
                    owner=changes["owner"],
                    fence=changes["fence"],
                    created_at=now(),
                )
            )
        connection.execute(
            update(leases).where(leases.c.task_id == task["id"]).values(**changes)
        )
        connection.execute(
            update(ct.tasks)
            .where(ct.tasks.c.id == task["id"])
            .values(status="running", revision=ct.tasks.c.revision + 1)
        )
        append_event(
            connection,
            root["id"],
            "execution.started",
            {
                "execution_id": identifier,
                "task_id": task["id"],
                "name": spec["name"],
                "role": "member",
                "description": task["objective"],
            },
        )
        return {
            "root": root,
            "task": task,
            "lease": {**lease, **changes},
            "spec": spec,
            "input": messages,
            "contract": contract.output,
        }

    async def execute(self, claim):
        from .scheduler import CollaborationScheduler

        controller = TaskController(self.worker, claim)
        context = ExecutionContext.from_run(
            controller.run,
            spec=claim["spec"],
            task_id=claim["task"]["id"],
            execution_id=claim["lease"]["execution_id"],
            role="member",
        )
        with self.db.tenant_scope(context.tenant_id):
            pulse = asyncio.create_task(controller.heartbeat())
            try:
                await CollaborationScheduler(controller, controller.run).execute(
                    claim["spec"],
                    claim["input"],
                    task=claim["task"],
                    contract=claim["contract"],
                    admitted_context=context,
                )
            except (RunCancelled, asyncio.CancelledError):
                # Never return an interrupted external request to the ready queue.
                # The journal and lease expiry decide whether a new owner may resume.
                if not asyncio.current_task().cancelling():
                    await asyncio.to_thread(
                        controller.fail, "成员授权已失效或执行已取消。"
                    )
                else:
                    raise
            except Exception:  # noqa: BLE001 - isolate a failed member without exposing provider details
                await asyncio.to_thread(controller.fail, "成员任务执行失败。")
            finally:
                pulse.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await pulse


class TaskController:
    """Execution capability for one child; never impersonates the root owner."""

    def __init__(self, worker, claim):
        self.worker, self.claim = worker, claim
        self.platform, self.db, self.settings = (
            worker.platform,
            worker.db,
            worker.settings,
        )
        self.gateway, self.owner = worker.gateway, worker.owner
        self.remote_transport = getattr(worker, "remote_transport", None)
        self.run = {
            **claim["root"],
            "owner": claim["lease"]["owner"],
            "fence": claim["lease"]["fence"],
            "deadline": claim["lease"]["deadline"],
        }
        self.invocation = InvocationService(self)

    def _assert_lease(self, connection, run, *, check_deadline=True):
        root, expected = self.claim["root"], self.claim["lease"]
        current = (
            connection.execute(
                select(t.runs).where(
                    t.runs.c.tenant_id == root["tenant_id"], t.runs.c.id == root["id"]
                )
            )
            .mappings()
            .one()
        )
        task_lease = (
            connection.execute(
                select(leases).where(
                    leases.c.tenant_id == root["tenant_id"],
                    leases.c.task_id == expected["task_id"],
                )
            )
            .mappings()
            .one()
        )
        execution = (
            connection.execute(
                select(ct.executions).where(
                    ct.executions.c.tenant_id == root["tenant_id"],
                    ct.executions.c.root_run_id == root["id"],
                    ct.executions.c.id == expected["execution_id"],
                )
            )
            .mappings()
            .first()
        )
        stamp = time.time()
        if (
            not execution
            or execution["status"] != "running"
            or execution["owner"] != self.owner
            or execution["fence"] != expected["fence"]
            or (
                run.get("execution_id") is not None
                and (
                    run["execution_id"] != expected["execution_id"]
                    or run.get("task_id") != expected["task_id"]
                    or run["spec"] != execution["spec"]
                )
            )
            or run["id"] != root["id"]
            or run["tenant_id"] != root["tenant_id"]
            or current["owner"] != root["owner"]
            or current["fence"] != root["fence"]
            or current["status"] not in ("running", "cancelling")
            or not current["lease_until"]
            or current["lease_until"] <= stamp
            or task_lease["root_fence"] != current["fence"]
            or task_lease["owner"] != self.owner
            or task_lease["fence"] != expected["fence"]
            or not task_lease["lease_until"]
            or task_lease["lease_until"] <= stamp
            or task_lease["execution_id"] != expected["execution_id"]
        ):
            raise PlatformError(409, "task_lease_lost", "成员任务执行权已失效。")
        if check_deadline and any(
            value is not None and value <= stamp
            for value in (current["deadline"], task_lease["deadline"])
        ):
            raise TimeoutError("成员执行期限已到。")
        if not self.settings.collaboration_distributed_enabled:
            raise PlatformError(409, "distributed_disabled", "跨 Worker 执行已关闭。")
        return current

    def _authorize_identity(self, connection, run):
        return self.worker._authorize_identity(connection, run)

    def is_cancelled(self, run):
        with self.db.tenant_scope(run["tenant_id"]), self.db.read() as connection:
            try:
                current = self._assert_lease(connection, run)
                self.invocation._authorize_identity(connection, run)
                return bool(current["cancel_requested"])
            except PlatformError:
                return True

    def event(self, run, kind, data):
        with self.db.transaction(f"tenant:{run['tenant_id']}") as connection:
            current = self._assert_lease(connection, run)
            if current["cancel_requested"]:
                raise RunCancelled()
            append_event(connection, run["id"], kind, data)

    def renew(self):
        with self.db.transaction(f"tenant:{self.run['tenant_id']}") as connection:
            current = self._assert_lease(connection, self.run)
            if current["cancel_requested"]:
                raise RunCancelled()
            until = time.time() + 20
            if self.run["deadline"] is not None:
                until = min(until, self.run["deadline"])
            connection.execute(
                update(leases)
                .where(leases.c.task_id == self.claim["task"]["id"])
                .values(lease_until=until)
            )

    async def heartbeat(self):
        while True:
            await asyncio.sleep(3)
            await asyncio.to_thread(self.renew)

    def fail(self, error):
        with self.db.transaction(f"tenant:{self.run['tenant_id']}") as connection:
            try:
                current = self._assert_lease(connection, self.run, check_deadline=False)
            except PlatformError:
                return
            if current["cancel_requested"]:
                return  # Root cancellation owns the consistent child terminal state.
            status = connection.scalar(
                select(ct.tasks.c.status).where(
                    ct.tasks.c.id == self.claim["task"]["id"]
                )
            )
            if status == "running":
                fail_task(connection, self.claim["task"], error)
