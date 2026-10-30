"""Explicit, repeatable local demo data. Never queues work or calls a model."""

import argparse
import csv
import hashlib
import json
import os
import random
import secrets
import sqlite3
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import insert, select, update

from agent_platform.infrastructure import metering_tables as mt
from agent_platform.infrastructure import tables as t
from agent_platform.infrastructure import tenancy_tables as nt
from agent_platform.infrastructure.config import Settings
from agent_platform.infrastructure.db import Database
from agent_platform.modules.billing import service as billing
from agent_platform.modules.platform import Platform, hasher

BATCH = "local-demo-v1"
TENANT_NAME = "演示数据组织（模拟）"
ROLE_GROUPS = (
    ("platform_admin", "平台管理员", 5, "member"),
    ("platform_finance", "平台财务", 5, "finance_viewer"),
    ("platform_support", "平台支持", 5, "member"),
    ("owner", "组织所有者", 5, "owner"),
    ("tenant_admin", "组织管理员", 10, "tenant_admin"),
    ("member", "普通成员", 15, "member"),
    ("finance_viewer", "财务查看者", 5, "finance_viewer"),
)
TOPICS = (
    ("产品需求梳理", "整理客户反馈，按影响范围和实施成本排列优先级"),
    ("季度销售复盘", "比较各区域的销售额、增长率和目标完成率"),
    ("项目进度分析", "列出当前里程碑、风险和下一步负责人"),
    ("用户增长分析", "比较访问、注册、激活和付费的转化情况"),
    ("知识库整理", "把文档按主题归类，并列出需要补充的内容"),
    ("客服问题归纳", "归纳高频咨询，整理统一回复和升级处理条件"),
    ("活动方案评估", "比较三种活动方案的预算、目标和执行步骤"),
    ("研发质量报告", "分析缺陷分布、修复时间和回归验证结果"),
    ("培训计划制定", "安排四周的学习目标、练习任务和验收标准"),
    ("运营指标看板", "整理最近十二个月的指标，给出图表展示建议"),
)


def demo_id(tenant_id, value):
    return uuid5(NAMESPACE_URL, f"{BATCH}:{tenant_id}:{value}").hex


def seed_demo(
    platform, admin_email, password, *, sessions_per_user=9, admin_sessions=50, rounds=5
):
    """All generated records commit together; a completed batch is never duplicated."""
    if not platform.db.sqlite or platform.settings.mode != "local":
        raise ValueError("Demo seeding is restricted to local SQLite databases")
    if len(password) < 16 or min(sessions_per_user, admin_sessions, rounds) < 1:
        raise ValueError("Use a password of at least 16 characters and positive counts")
    platform.db.assert_schema()
    with platform.db.read() as conn:
        admin_id = conn.scalar(
            select(t.users.c.id).where(t.users.c.email == admin_email)
        )
    actor = platform.identity.principal(admin_id)
    if "platform.roles.manage" not in actor["capabilities"]:
        raise ValueError("An existing platform administrator is required")
    tenant = platform.identity.create_tenant(
        actor,
        name=TENANT_NAME,
        owner_user_id=admin_id,
        idempotency_key=BATCH,
        limits={"max_members": 100},
    )
    tenant_id = tenant["id"]
    key = lambda value: demo_id(tenant_id, value)
    config = {
        "sessions_per_user": sessions_per_user,
        "admin_sessions": admin_sessions,
        "rounds": rounds,
    }
    with platform.db.transaction(f"tenant:{tenant_id}") as conn:
        previous = conn.execute(
            select(nt.operation_audits.c.details).where(
                nt.operation_audits.c.id == key("complete")
            )
        ).scalar_one_or_none()
        if previous:
            if previous["config"] != config:
                raise ValueError("This batch already exists with different counts")
            # Do not export a newly supplied password for accounts with an old password.
            stored = conn.scalar(
                select(t.users.c.password_hash).where(
                    t.users.c.id == key("user:member:1")
                )
            )
            if not hasher.verify(stored, password):
                raise ValueError("Existing demo accounts use a different password")
            return previous
        stamp = datetime.now(UTC)
        origin = stamp - timedelta(days=35)
        rng = random.Random(20261030)
        rows = defaultdict(list)
        accounts = []
        for role, label, count, membership_role in ROLE_GROUPS:
            for number in range(1, count + 1):
                user_id = key(f"user:{role}:{number}")
                email = f"demo.{role}.{number:02d}@example.test"
                name = f"模拟·{label}{number:02d}"
                rows[t.users].append(
                    {
                        "id": user_id,
                        "tenant_id": tenant_id,
                        "email": email,
                        "name": name,
                        "password_hash": hasher.hash(password),
                        "role": "member",
                        "active": True,
                        "global_status": "active",
                        "auth_version": 1,
                        "created_at": origin.isoformat(),
                    }
                )
                rows[nt.memberships].append(
                    {
                        "id": key(f"membership:{user_id}"),
                        "tenant_id": tenant_id,
                        "user_id": user_id,
                        "role": membership_role,
                        "status": "active",
                        "authz_version": 1,
                        "joined_at": origin.isoformat(),
                    }
                )
                if role.startswith("platform_"):
                    rows[nt.platform_roles].append(
                        {
                            "user_id": user_id,
                            "role": role,
                            "granted_by": admin_id,
                            "granted_at": stamp.isoformat(),
                        }
                    )
                accounts.append(
                    {
                        "id": user_id,
                        "email": email,
                        "name": name,
                        "role": role,
                        "role_label": label,
                        "membership_role": membership_role,
                    }
                )
                rows[nt.operation_audits].append(
                    {
                        "id": key(f"account-audit:{user_id}"),
                        "actor_user_id": admin_id,
                        "tenant_id": tenant_id,
                        "action": "demo.account.create",
                        "target": user_id,
                        "reason": "用户要求生成本地权限测试账号",
                        "details": {"synthetic": True, "role": role},
                        "created_at": stamp.isoformat(),
                    }
                )
        agents = {
            a["builtin_key"]: dict(a)
            for a in conn.execute(
                select(t.agents).where(t.agents.c.tenant_id == tenant_id)
            ).mappings()
        }
        if set(agents) != {"normal", "generative_ui"}:
            raise ValueError(
                "The demo tenant must contain exactly the two built-in assistants"
            )
        models = []
        for index, label in enumerate(("快速文本", "深度分析", "可视化")):
            model = {
                "id": key(f"model:{index}"),
                "tenant_id": tenant_id,
                "created_at": origin.isoformat(),
                "name": f"模拟模型·{label}（仅历史展示）",
                "alias": f"demo-offline-{index}",
                "active": False,
                "input_price": str(index + 1),
                "output_price": str((index + 1) * 3),
                "context_window": 65536,
                "max_output_tokens": 4096,
                "price_version": 1,
            }
            models.append(model)
        rows[t.models] = models
        owners = [(a, sessions_per_user) for a in accounts]
        owners.append(
            ({"id": admin_id, "email": admin_email, "name": "管理员"}, admin_sessions)
        )
        pending = []
        session_index = 0
        for account, count in owners:
            for local_index in range(count):
                session_index += 1
                session_id = key(f"session:{session_index}")
                topic, request = TOPICS[(session_index - 1) % len(TOPICS)]
                agent = agents["generative_ui" if session_index % 5 == 0 else "normal"]
                start = stamp - timedelta(minutes=rng.randint(120, 30 * 24 * 60))
                rows[t.sessions].append(
                    {
                        "id": session_id,
                        "tenant_id": tenant_id,
                        "created_at": start.isoformat(),
                        "user_id": account["id"],
                        "agent_id": agent["id"],
                        "title": f"[模拟] {topic} · {session_index:03d}",
                    }
                )
                for turn in range(rounds):
                    pending.append(
                        (
                            start + timedelta(minutes=turn * 2),
                            session_id,
                            account,
                            agent,
                            topic,
                            request,
                            turn,
                            session_index,
                        )
                    )
        balance = Decimal(1000)
        totals = defaultdict(Decimal)

        def transaction(kind, amount, when, reference, call_id=None):
            transaction_id = key(f"transaction:{reference}")
            rows[billing.ledger_transactions].append(
                {
                    "id": transaction_id,
                    "tenant_id": tenant_id,
                    "type": kind,
                    "amount": amount,
                    "balance": balance,
                    "description": f"[模拟数据] {reference}",
                    "actor_id": admin_id,
                    "call_id": call_id,
                    "idempotency_key": reference,
                    "fingerprint": key(reference),
                    "created_at": when,
                }
            )
            for account, value in (
                ("customer_wallet", amount),
                ("external_credit" if kind == "credit" else "model_revenue", -amount),
            ):
                rows[billing.ledger_entries].append(
                    {
                        "id": key(f"entry:{reference}:{account}"),
                        "tenant_id": tenant_id,
                        "transaction_id": transaction_id,
                        "account": account,
                        "amount": value,
                        "currency": "USD",
                    }
                )

        transaction("credit", balance, origin.isoformat(), "演示初始余额，非真实充值")
        status_counts = defaultdict(int)
        for index, (
            when,
            session_id,
            account,
            agent,
            topic,
            request,
            turn,
            serial,
        ) in enumerate(sorted(pending)):
            run_id, call_id = key(f"run:{index}"), key(f"call:{index}")
            model = models[index % len(models)]
            state = (
                "failed"
                if index % 20 == 0
                else "cancelled"
                if index % 20 == 1
                else "succeeded"
            )
            status_counts[state] += 1
            start = when.isoformat()
            end = (when + timedelta(seconds=12)).isoformat()
            prompt = f"[模拟场景 {serial:03d}] {request}。这是第 {turn + 1} 轮，请给出具体步骤和可验证的指标。"
            error = "[模拟] 上游服务响应超时" if state == "failed" else ""
            spec = {
                **agent,
                "model_id": model["id"],
                "model": model,
                "synthetic": True,
                "demo_batch": BATCH,
            }
            rows[t.runs].append(
                {
                    "id": run_id,
                    "tenant_id": tenant_id,
                    "created_at": start,
                    "session_id": session_id,
                    "user_id": account["id"],
                    "agent_name": agent["name"],
                    "spec": spec,
                    "message": prompt,
                    "status": state,
                    "error": error,
                    "idempotency_key": run_id,
                    "request_hash": hashlib.sha256(prompt.encode()).hexdigest(),
                    "cancel_requested": state == "cancelled",
                    "fence": 1,
                    "finished_at": end,
                }
            )
            rows[t.messages].append(
                {
                    "id": key(f"message:{index}:user"),
                    "tenant_id": tenant_id,
                    "created_at": start,
                    "session_id": session_id,
                    "run_id": run_id,
                    "role": "user",
                    "content": prompt,
                }
            )
            if state == "succeeded":
                value = rng.randint(15, 95)
                content = (
                    f"以下为 **{topic}** 的模拟结果，数据仅用于功能测试。\n\n"
                    f"1. 明确范围：第 {turn + 1} 阶段关注目标、现状与差距。\n"
                    f"2. 执行建议：将任务拆成三个里程碑，每周核对进度。\n"
                    f"3. 验收指标：示例完成率 {value}%，覆盖 {serial % 12 + 3} 个业务场景。\n\n"
                    f"| 指标 | 本期 | 上期 |\n| --- | ---: | ---: |\n"
                    f"| 完成任务 | {value} | {max(0, value - 8)} |\n"
                    f"| 待跟进项 | {serial % 7 + 1} | {serial % 7 + 3} |\n\n"
                    "下一步：确认负责人和截止日期，再根据实际反馈调整方案。"
                )
                rows[t.messages].append(
                    {
                        "id": key(f"message:{index}:assistant"),
                        "tenant_id": tenant_id,
                        "created_at": end,
                        "session_id": session_id,
                        "run_id": run_id,
                        "role": "assistant",
                        "content": content,
                    }
                )
            for sequence, event_type, event_time, data in (
                (1, "run.started", start, {"status": "running"}),
                (2, f"run.{state}", end, {"status": state, "error": error}),
            ):
                rows[t.run_events].append(
                    {
                        "run_id": run_id,
                        "tenant_id": tenant_id,
                        "sequence": sequence,
                        "type": event_type,
                        "data": {**data, "synthetic": True},
                        "created_at": event_time,
                    }
                )
            input_tokens = rng.randint(180, 16000) if state == "succeeded" else 0
            output_tokens = rng.randint(90, 3000) if state == "succeeded" else 0
            cost = (
                input_tokens * Decimal(model["input_price"])
                + output_tokens * Decimal(model["output_price"])
            ) / 1_000_000
            raw = {
                "synthetic": True,
                "demo_batch": BATCH,
                "prompt_tokens": input_tokens,
                "completion_tokens": output_tokens,
            }
            rows[t.calls].append(
                {
                    "id": call_id,
                    "tenant_id": tenant_id,
                    "created_at": start,
                    "user_id": account["id"],
                    "run_id": run_id,
                    "model_id": model["id"],
                    "model": model["alias"],
                    "agent_name": agent["name"],
                    "user_email": account["email"],
                    "status": state,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "cost": str(cost),
                    "provider_cost": str(cost * Decimal("0.6")),
                    "price_version": 1,
                    "raw_usage": raw,
                    "error": error,
                    "finished_at": end,
                }
            )
            rows[billing.reservations_table].append(
                {
                    "id": key(f"reservation:{index}"),
                    "tenant_id": tenant_id,
                    "user_id": account["id"],
                    "model_id": model["id"],
                    "run_id": run_id,
                    "call_id": call_id,
                    "fingerprint": run_id,
                    "amount": cost,
                    "input_price": Decimal(model["input_price"]),
                    "output_price": Decimal(model["output_price"]),
                    "estimated_input_tokens": input_tokens,
                    "max_output_tokens": output_tokens,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "rate_tokens": input_tokens + output_tokens,
                    "rate_scopes": "[]",
                    "status": "settled" if state == "succeeded" else "released",
                    "cost": cost,
                    "actual_cost": cost,
                    "overage": Decimal(0),
                    "provider_cost": cost * Decimal("0.6"),
                    "raw_usage": json.dumps(raw),
                    "reason": "本地模拟数据，未调用上游模型",
                    "created_at": start,
                    "updated_at": end,
                }
            )
            if cost:
                balance -= cost
                transaction("charge", -cost, end, f"模拟调用 {index + 1}", call_id)
                for scope, subject in (
                    ("tenant", tenant_id),
                    ("user", account["id"]),
                    ("model", model["id"]),
                ):
                    totals[(scope, subject)] += cost
        for (scope, subject), spent in totals.items():
            rows[mt.budget_totals].append(
                {
                    "tenant_id": tenant_id,
                    "scope": scope,
                    "subject_id": subject,
                    "spent": spent,
                }
            )
        if balance < 0:
            raise ValueError("The demo balance cannot cover the requested volume")
        # Parents precede children. One transaction prevents a partial batch on failure.
        for table in (
            t.users,
            nt.memberships,
            nt.platform_roles,
            t.models,
            t.sessions,
            t.runs,
            t.messages,
            t.run_events,
            t.calls,
            billing.reservations_table,
            billing.ledger_transactions,
            billing.ledger_entries,
            mt.budget_totals,
            nt.operation_audits,
        ):
            if rows[table]:
                conn.execute(insert(table), rows[table])
        conn.execute(
            update(billing.wallets)
            .where(billing.wallets.c.tenant_id == tenant_id)
            .values(balance=balance)
        )
        report = {
            "batch": BATCH,
            "tenant_id": tenant_id,
            "tenant_name": TENANT_NAME,
            "config": config,
            "accounts": accounts,
            "counts": {table.name: len(values) for table, values in rows.items()},
            "run_statuses": dict(status_counts),
            "simulated_cost": str(Decimal(1000) - balance),
            "simulated_balance": str(balance),
        }
        conn.execute(
            insert(nt.operation_audits).values(
                id=key("complete"),
                actor_user_id=admin_id,
                tenant_id=tenant_id,
                action="demo.seed.complete",
                target=BATCH,
                reason="用户要求创建 50 个权限测试账号及批量假数据",
                details=report,
                created_at=stamp.isoformat(),
            )
        )
        return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--admin-email", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    database = args.database.resolve(strict=True)
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    output.chmod(0o700)
    password_file = output / "password.txt"
    if password_file.exists():
        password = password_file.read_text().strip()
    else:
        password = "Demo-" + secrets.token_urlsafe(18)
        with os.fdopen(
            os.open(password_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w"
        ) as handle:
            handle.write(password + "\n")
    backup = output / (
        "before-seed-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ") + ".db"
    )
    with (
        sqlite3.connect(f"file:{database}?mode=ro", uri=True) as source,
        sqlite3.connect(backup) as target,
    ):
        source.backup(target)
    backup.chmod(0o600)
    settings = Settings(database_url=f"sqlite:///{database}", mode="local")
    db = Database(settings.database_url)
    try:
        report = seed_demo(Platform(db, settings), args.admin_email, password)
    finally:
        db.close()
    for filename, content in (
        ("report.json", json.dumps(report, ensure_ascii=False, indent=2)),
    ):
        path = output / filename
        with os.fdopen(
            os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w"
        ) as handle:
            handle.write(content + "\n")
    with os.fdopen(
        os.open(output / "accounts.csv", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600),
        "w",
        newline="",
        encoding="utf-8-sig",
    ) as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "email",
                "password",
                "name",
                "role",
                "role_label",
                "membership_role",
            ],
        )
        writer.writeheader()
        for account in report["accounts"]:
            writer.writerow(
                {
                    **{k: v for k, v in account.items() if k != "id"},
                    "password": password,
                }
            )
    print(
        json.dumps(
            {k: v for k, v in report.items() if k != "accounts"},
            ensure_ascii=False,
            indent=2,
        )
    )
    print(f"Private account list: {output / 'accounts.csv'}")


if __name__ == "__main__":
    main()
