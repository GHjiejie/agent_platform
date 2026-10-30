"""One-time import of the local installation into OrbStack Kubernetes.

Secrets are sent on stdin to kubectl, never stored in manifests or command args.
The local profile retains SQLite, so all application roles stay at one replica.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

import yaml
from dotenv import dotenv_values

from agent_platform.scripts.gateway import LocalGateway
from agent_platform.scripts.local import load_runtime_environment, managed_state

ROOT = Path(__file__).resolve().parents[2]
PLATFORM = ROOT / "agent_platform"
STATE = PLATFORM / ".data/kubernetes"
STACK = PLATFORM / "deploy/kubernetes/stack.yaml"
ROLES = ("api", "worker", "gateway-sync", "maintenance")
IMAGE = "agent-platform:0.2.0-k8s"


def run(command, *, data=None, stdin=None, stdout=None, timeout=600):
    result = subprocess.run(
        command,
        input=data,
        stdin=stdin,
        stdout=stdout or subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )
    if result.returncode:
        # A management command can include raw database errors or Secret JSON.
        # Do not echo either into terminal/chat logs.
        STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
        log = STATE / "last-error.log"
        descriptor = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "wb") as target:
            target.write(result.stderr or b"")
        raise RuntimeError(f"{command[0]} 执行失败，私有诊断保存在 {log}")
    return (result.stdout or b"").decode() if stdout is None else ""


def private_json(path, value):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as target:
        json.dump(value, target, ensure_ascii=False, indent=2)
        target.write("\n")


class Cluster:
    def __init__(self, args):
        self.args = args
        self.context = (
            args.context or run(["kubectl", "config", "current-context"]).strip()
        )
        if self.context != "orbstack":
            raise RuntimeError("此本地部署配置只支持 orbstack；不会修改其他集群。")
        self.namespace = args.namespace
        self.image = args.image
        self.base = ["kubectl", "--context", self.context, "-n", self.namespace]
        STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
        STATE.chmod(0o700)

    def kube(self, *args, **kwargs):
        return run([*self.base, *args], **kwargs)

    def get(self, kind, name):
        value = self.kube("get", kind, name, "--ignore-not-found", "-o", "json")
        return json.loads(value) if value.strip() else None

    def apply(self, items):
        self.kube(
            "apply",
            "--server-side",
            "--field-manager=agent-platform",
            "-f",
            "-",
            data=json.dumps(
                {"apiVersion": "v1", "kind": "List", "items": items}
            ).encode(),
        )

    def marker(self, name, data):
        self.apply(
            [
                {
                    "apiVersion": "v1",
                    "kind": "ConfigMap",
                    "metadata": {"name": name, "namespace": self.namespace},
                    "data": data,
                }
            ]
        )

    def resources(self):
        items = []
        for original in yaml.safe_load(STACK.read_text())["items"]:
            resource = copy.deepcopy(original)
            metadata = resource["metadata"]
            metadata.setdefault("labels", {})["app.kubernetes.io/part-of"] = (
                "agent-platform"
            )
            if resource["kind"] == "Namespace":
                metadata["name"] = self.namespace
            else:
                metadata["namespace"] = self.namespace
            if resource["kind"] in {"Deployment", "StatefulSet"}:
                pod = resource["spec"]["template"]
                pod["metadata"]["labels"]["app.kubernetes.io/part-of"] = (
                    "agent-platform"
                )
                # Local-path/SQLite must stay on the single OrbStack node.
                pod["spec"]["nodeSelector"] = {"kubernetes.io/hostname": "orbstack"}
                for container in pod["spec"]["containers"]:
                    if container["name"] == "platform":
                        container["image"] = self.image
                resource["spec"]["replicas"] = 0
            items.append(resource)
        return items

    def build(self):
        print("构建平台镜像…", flush=True)
        subprocess.run(
            [
                "docker",
                "build",
                "-f",
                str(PLATFORM / "deploy/Dockerfile"),
                "-t",
                self.image,
                str(ROOT),
            ],
            check=True,
        )

    def settings(self):
        env = load_runtime_environment(self.args.env_file, environ={})
        gateway = LocalGateway(PLATFORM / ".data/local", env, 4000)
        if not gateway.control_file.is_file() or not (
            gateway.key_file.is_file() or gateway.legacy_key_file.is_file()
        ):
            raise RuntimeError(
                "未找到现有 managed 网关配置；请使用原实例配置进行迁移。"
            )
        gateway.prepare()
        controls = gateway.compose_env
        runtime_key = dotenv_values(gateway.key_file).get("PLATFORM_LITELLM_KEY")
        if not runtime_key or not env.get("PLATFORM_SECRET_ENCRYPTION_KEY"):
            raise RuntimeError("现有运行 Key 或持久加密 Key 缺失，不能迁移。")
        return env, gateway, controls, runtime_key

    def install_config(self, initial=False):
        if initial:
            env, _gateway, controls, runtime_key = self.settings()
        else:
            env = {
                k: v
                for k, v in dotenv_values(self.args.env_file).items()
                if v is not None
            }
            controls, runtime_key = {}, None
        resources = self.resources()
        self.apply([r for r in resources if r["kind"] == "Namespace"])
        if not initial:
            # Deployments have been drained; keep databases running while their
            # StatefulSet configuration is reconciled. Respect the scale owner.
            for resource in resources:
                if resource["kind"] == "StatefulSet":
                    existing = self.get("statefulset", resource["metadata"]["name"])
                    if existing:
                        resource["spec"]["replicas"] = existing["spec"]["replicas"]
        config = next(
            r for r in resources if r["metadata"]["name"] == "platform-config"
        )
        # Preserve explicit operational limits, without copying arbitrary env.
        for name in (
            "PLATFORM_WORKER_CONCURRENCY",
            "PLATFORM_RUN_TIMEOUT",
            "PLATFORM_MODEL_TIMEOUT",
            "PLATFORM_MAX_RUN_COST",
            "PLATFORM_COLLABORATION_ENABLED",
            "PLATFORM_COLLABORATION_DYNAMIC_ENABLED",
            "PLATFORM_COLLABORATION_PARALLEL_ENABLED",
            "PLATFORM_COLLABORATION_DURABLE_ENABLED",
            "PLATFORM_COLLABORATION_RECOVERY_ENABLED",
            "PLATFORM_COLLABORATION_DISTRIBUTED_ENABLED",
            "PLATFORM_REMOTE_AGENTS_ENABLED",
            "PLATFORM_REMOTE_AGENT_ALLOWED_ORIGINS",
            "PLATFORM_COLLABORATION_MAX_ACTIVE_MEMBERS",
            "PLATFORM_COLLABORATION_CAPACITY_WAIT_SECONDS",
            "PLATFORM_MAINTENANCE_INTERVAL",
            "PLATFORM_EXPORT_TTL_SECONDS",
            "PLATFORM_DELETION_RETENTION_DAYS",
        ):
            if env.get(name):
                config["data"][name] = env[name]
        if initial:
            config["data"]["PLATFORM_DEFAULT_MODEL"] = controls["LITELLM_MODEL_ALIAS"]
        else:
            existing_config = self.get("configmap", "platform-config")
            if existing_config:
                config["data"]["PLATFORM_DEFAULT_MODEL"] = existing_config["data"].get(
                    "PLATFORM_DEFAULT_MODEL", ""
                )
        secrets = (
            {
                "platform-runtime-secret": {
                    "PLATFORM_SECRET_ENCRYPTION_KEY": env[
                        "PLATFORM_SECRET_ENCRYPTION_KEY"
                    ],
                    "PLATFORM_LITELLM_KEY": runtime_key,
                },
                "gateway-control-secret": {
                    "PLATFORM_GATEWAY_CONTROL_URL": "http://litellm:4000",
                    "PLATFORM_GATEWAY_CONTROL_KEY": controls["LITELLM_MASTER_KEY"],
                },
                "postgres-secret": {
                    "POSTGRES_PASSWORD": controls["POSTGRES_PASSWORD"],
                    "LITELLM_DB_PASSWORD": controls["LITELLM_DB_PASSWORD"],
                },
                "litellm-secret": {
                    **{
                        name: controls[name]
                        for name in (
                            "LITELLM_MASTER_KEY",
                            "LITELLM_SALT_KEY",
                            "UI_USERNAME",
                            "UI_PASSWORD",
                            "UPSTREAM_MODEL",
                            "UPSTREAM_API_BASE",
                            "UPSTREAM_API_KEY",
                            "LITELLM_MODEL_ALIAS",
                        )
                    },
                    "DATABASE_URL": "postgresql://litellm:"
                    + quote(controls["LITELLM_DB_PASSWORD"], safe="")
                    + "@postgres:5432/litellm",
                },
            }
            if initial
            else dict.fromkeys(
                (
                    "platform-runtime-secret",
                    "gateway-control-secret",
                    "postgres-secret",
                    "litellm-secret",
                )
            )
        )
        if initial:
            self.apply(
                [
                    {
                        "apiVersion": "v1",
                        "kind": "Secret",
                        "type": "Opaque",
                        "metadata": {"name": name, "namespace": self.namespace},
                        "stringData": values,
                    }
                    for name, values in secrets.items()
                ]
            )
        else:
            for name in secrets:
                if not self.get("secret", name):
                    raise RuntimeError(
                        f"集群 Secret {name} 缺失；不会自动替换已有凭据。"
                    )
        self.apply(
            [
                {
                    "apiVersion": "v1",
                    "kind": "ConfigMap",
                    "metadata": {"name": "litellm-config", "namespace": self.namespace},
                    "data": {
                        "litellm.yaml": (PLATFORM / "deploy/litellm.yaml").read_text()
                    },
                }
            ]
        )
        self.apply([r for r in resources if r["kind"] != "Namespace"])

    def scale(self, kind, names, replicas):
        for name in names:
            self.kube("scale", f"{kind}/{name}", f"--replicas={replicas}")

    def wait_empty(self, apps):
        selector = "app in (" + ",".join("platform-" + app for app in apps) + ")"
        self.kube(
            "wait", "--for=delete", "pod", "-l", selector, "--timeout=420s", timeout=440
        )

    def wait_ready(self, kind, name):
        self.kube("rollout", "status", f"{kind}/{name}", "--timeout=300s", timeout=320)
        print(f"{name} 已就绪", flush=True)

    def retain_volumes(self):
        claims = json.loads(self.kube("get", "pvc", "-o", "json"))["items"]
        for claim in claims:
            volume = claim["spec"].get("volumeName")
            if volume:
                self.kube(
                    "patch",
                    "pv",
                    volume,
                    "--type=merge",
                    "-p",
                    '{"spec":{"persistentVolumeReclaimPolicy":"Retain"}}',
                )

    def migrate_schema(self):
        template = next(
            r
            for r in self.resources()
            if r["metadata"]["name"] == "api" and r["kind"] == "Deployment"
        )["spec"]["template"]
        template["metadata"]["labels"] = {"app": "platform-migration"}
        template["spec"]["restartPolicy"] = "Never"
        container = template["spec"]["containers"][0]
        container["command"] = [
            "alembic",
            "-c",
            "agent_platform/alembic.ini",
            "upgrade",
            "head",
        ]
        container.pop("startupProbe", None)
        container.pop("readinessProbe", None)
        name = "schema-" + str(time.time_ns())
        self.apply(
            [
                {
                    "apiVersion": "batch/v1",
                    "kind": "Job",
                    "metadata": {"name": name, "namespace": self.namespace},
                    "spec": {
                        "backoffLimit": 0,
                        "activeDeadlineSeconds": 300,
                        "ttlSecondsAfterFinished": 86400,
                        "template": template,
                    },
                }
            ]
        )
        self.kube(
            "wait",
            "--for=condition=complete",
            f"job/{name}",
            "--timeout=310s",
            timeout=330,
        )

    def start(self):
        self.scale("statefulset", ("postgres", "redis"), 1)
        self.wait_ready("statefulset", "postgres")
        self.wait_ready("statefulset", "redis")
        self.migrate_schema()
        self.scale("deployment", ("litellm",), 1)
        self.wait_ready("deployment", "litellm")
        self.scale("deployment", ROLES, 1)
        for role in ROLES:
            self.wait_ready("deployment", role)
        self.retain_volumes()
        self.marker(
            "platform-release",
            {
                "status": "ready",
                "image": self.image,
                "deployed_at": datetime.now(UTC).isoformat(),
            },
        )
        private_json(
            STATE / "deployment.json",
            {
                "context": self.context,
                "namespace": self.namespace,
                "image": self.image,
            },
        )
        print(
            "Kubernetes 部署完成：http://127.0.0.1:8010；LiteLLM：http://127.0.0.1:4000/ui",
            flush=True,
        )

    def deploy(self):
        if (self.get("configmap", "platform-release") or {}).get("data", {}).get(
            "manager"
        ) == "helm":
            raise RuntimeError(
                "资源已经由独立 Helm releases 管理；请使用 scripts.helm deploy。"
            )
        if not self.get("configmap", "platform-release"):
            raise RuntimeError(
                "首次迁移请运行 make k8s-import；尚未导入数据，不能直接启动。"
            )
        if managed_state(PLATFORM / ".data/local"):
            raise RuntimeError(
                "旧本地 supervisor 正在运行，请先停止，避免两个环境同时提供服务。"
            )
        if not self.args.skip_build:
            self.build()
        self.drain()
        self.scale("deployment", (*ROLES, "litellm"), 0)
        self.wait_empty((*ROLES, "litellm"))
        self.install_config()
        self.start()

    def drain(self):
        """Close admission, then let the existing worker finish queued work."""
        api = self.get("deployment", "api")
        previous = api["spec"]["replicas"] if api else 0
        self.scale("deployment", ("api",), 0)
        self.wait_empty(("api",))
        workers = json.loads(
            self.kube("get", "pods", "-l", "app=platform-worker", "-o", "json")
        )["items"]
        if not any(p["status"].get("phase") == "Running" for p in workers):
            return
        print("已关闭新请求入口，等待当前运行收尾…", flush=True)
        deadline = time.monotonic() + 360
        try:
            while True:
                count = self.kube(
                    "exec",
                    "deployment/worker",
                    "--",
                    "python",
                    "-c",
                    "import sqlite3; c=sqlite3.connect('file:/app/agent_platform/.data/platform.db?mode=ro',uri=True); "
                    "print(c.execute(\"SELECT count(*) FROM platform_runs WHERE status IN ('queued','running','cancelling')\").fetchone()[0])",
                ).strip()
                if int(count) == 0:
                    return
                if time.monotonic() >= deadline:
                    raise RuntimeError(
                        "任务尚未全部结束，已恢复原入口；请稍后重试部署。"
                    )
                time.sleep(5)
        except (RuntimeError, subprocess.SubprocessError, ValueError):
            self.scale("deployment", ("api",), previous)
            raise

    def helper(self):
        self.apply(
            [
                {
                    "apiVersion": "v1",
                    "kind": "PersistentVolumeClaim",
                    "metadata": {"name": "data-redis-0", "namespace": self.namespace},
                    "spec": {
                        "accessModes": ["ReadWriteOnce"],
                        "storageClassName": "local-path",
                        "resources": {"requests": {"storage": "1Gi"}},
                    },
                }
            ]
        )
        self.apply(
            [
                {
                    "apiVersion": "v1",
                    "kind": "Pod",
                    "metadata": {"name": "data-import", "namespace": self.namespace},
                    "spec": {
                        "restartPolicy": "Never",
                        "automountServiceAccountToken": False,
                        "nodeSelector": {"kubernetes.io/hostname": "orbstack"},
                        "containers": [
                            {
                                "name": "import",
                                "image": self.image,
                                "imagePullPolicy": "IfNotPresent",
                                "command": [
                                    "python",
                                    "-c",
                                    "import time; time.sleep(7200)",
                                ],
                                "securityContext": {
                                    "runAsUser": 0,
                                    "allowPrivilegeEscalation": False,
                                },
                                "volumeMounts": [
                                    {"name": n, "mountPath": "/" + n}
                                    for n in ("data", "tombstones", "redis")
                                ],
                            }
                        ],
                        "volumes": [
                            {"name": n, "persistentVolumeClaim": {"claimName": claim}}
                            for n, claim in (
                                ("data", "platform-data"),
                                ("tombstones", "tenant-tombstones"),
                                ("redis", "data-redis-0"),
                            )
                        ],
                    },
                }
            ]
        )
        self.kube(
            "wait",
            "--for=condition=Ready",
            "pod/data-import",
            "--timeout=180s",
            timeout=200,
        )

    def import_local(self):
        if (STATE / "deployment.json").exists():
            raise RuntimeError("本机数据已经迁入 Kubernetes，不能再次导入旧本地副本。")
        if self.get("configmap", "platform-release") or self.get(
            "pvc", "platform-data"
        ):
            raise RuntimeError("目标已存在数据卷。不会覆盖；部分失败请按部署手册恢复。")
        env, gateway, _controls, _runtime_key = self.settings()
        source_url = env.get(
            "PLATFORM_DATABASE_URL", "sqlite:///agent_platform/.data/platform.db"
        )
        if source_url != "sqlite:///agent_platform/.data/platform.db":
            raise RuntimeError(
                "自动导入仅支持默认 SQLite 文件；自定义数据库需明确迁移。"
            )
        source = PLATFORM / ".data/platform.db"
        with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as db:
            active = db.execute(
                "SELECT count(*) FROM platform_runs WHERE status IN ('queued','running','cancelling')"
            ).fetchone()[0]
            if active:
                raise RuntimeError("存在排队或活动运行，请等待结束后再迁移。")
        if not self.args.skip_build:
            self.build()
        backup = STATE / ("backup-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"))
        backup.mkdir(mode=0o700)
        print(f"停止旧服务并保存独立备份：{backup}", flush=True)
        run(
            [
                sys.executable,
                "-m",
                "agent_platform.scripts.local",
                "stop",
                "--state-dir",
                str(PLATFORM / ".data/local"),
            ]
        )
        # A stopped writer is required before copying any SQLite/AOF state.
        with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as origin:
            active = origin.execute(
                "SELECT count(*) FROM platform_runs WHERE status IN ('queued','running','cancelling')"
            ).fetchone()[0]
            if active:
                raise RuntimeError(
                    "停机时存在未结束运行；保留源端，先恢复并收尾后再迁移。"
                )
            with sqlite3.connect(backup / "platform.db") as target:
                origin.backup(target)
            counts = {
                row[0]: origin.execute(
                    'SELECT count(*) FROM "' + row[0] + '"'
                ).fetchone()[0]
                for row in origin.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            }
        (backup / "platform.db").chmod(0o600)
        for name in ("exports",):
            folder = PLATFORM / ".data" / name
            if folder.exists():
                shutil.copytree(folder, backup / name)
        tombstone = PLATFORM / ".data/tenant-tombstones.jsonl"
        if tombstone.exists():
            shutil.copy2(tombstone, backup / tombstone.name)
        postgres = gateway.project + "-postgres-1"
        redis = gateway.project + "-redis-1"
        run(["docker", "cp", redis + ":/data/.", str(backup / "redis")])
        run(["docker", "start", postgres])
        try:
            deadline = time.monotonic() + 60
            while subprocess.run(
                ["docker", "exec", postgres, "pg_isready", "-U", "postgres"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            ).returncode:
                if time.monotonic() > deadline:
                    raise RuntimeError("源 PostgreSQL 未就绪。")
                time.sleep(1)
            with (backup / "litellm.dump").open("wb") as target:
                os.fchmod(target.fileno(), 0o600)
                run(
                    [
                        "docker",
                        "exec",
                        postgres,
                        "pg_dump",
                        "-U",
                        "postgres",
                        "-d",
                        "litellm",
                        "-Fc",
                        "--no-owner",
                        "--no-acl",
                    ],
                    stdout=target,
                )
            count_sql = (
                b"SELECT format('SELECT %L, count(*) FROM %I.%I', tablename, schemaname, tablename) "
                b"FROM pg_tables WHERE schemaname='public' ORDER BY tablename\n\\gexec\n"
            )
            gateway_counts = run(
                [
                    "docker",
                    "exec",
                    "-i",
                    postgres,
                    "psql",
                    "-U",
                    "postgres",
                    "-d",
                    "litellm",
                    "-At",
                ],
                data=count_sql,
            )
        finally:
            run(["docker", "stop", "--time", "30", postgres])
        digest = hashlib.sha256((backup / "platform.db").read_bytes()).hexdigest()
        private_json(
            backup / "manifest.json",
            {
                "database_sha256": digest,
                "table_counts": counts,
                "gateway_table_counts": gateway_counts,
                "context": self.context,
                "namespace": self.namespace,
                "image": self.image,
            },
        )
        self.install_config(initial=True)
        self.helper()
        self.retain_volumes()
        archive = backup / "volumes.tar"
        with tarfile.open(archive, "w") as bundle:
            bundle.add(backup / "platform.db", arcname="data/platform.db")
            if (backup / "exports").exists():
                bundle.add(backup / "exports", arcname="data/exports")
            if (backup / tombstone.name).exists():
                bundle.add(
                    backup / tombstone.name, arcname="tombstones/" + tombstone.name
                )
            bundle.add(backup / "redis", arcname="redis")
        archive.chmod(0o600)
        with archive.open("rb") as incoming:
            self.kube(
                "exec",
                "-i",
                "data-import",
                "--",
                "python",
                "-c",
                "import tarfile,sys; tarfile.open(fileobj=sys.stdin.buffer,mode='r|').extractall('/',filter='data')",
                stdin=incoming,
            )
        script = """
import hashlib,json,os,sqlite3
from pathlib import Path
p=Path('/data/platform.db')
assert hashlib.sha256(p.read_bytes()).hexdigest()==EXPECTED_HASH, 'Database checksum mismatch'
db=sqlite3.connect(p)
assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
assert not db.execute('PRAGMA foreign_key_check').fetchall()
for table,count in EXPECTED_COUNTS.items():
    assert db.execute('SELECT count(*) FROM "'+table+'"').fetchone()[0]==count, table
with db:
    db.execute("UPDATE model_deployments SET base_url='http://litellm:4000/v1', config_version=config_version+1 WHERE gateway_id='primary' AND base_url IN ('http://127.0.0.1:4000/v1','http://localhost:4000/v1')")
db.close()
for root in ('/data','/tombstones'):
    for directory,folders,files in os.walk(root):
        os.chown(directory,10001,10001); os.chmod(directory,0o700)
        for name in files:
            path=os.path.join(directory,name)
            os.chown(path,10001,10001); os.chmod(path,0o600)
print('SQLite digest, integrity, foreign keys and table counts verified')
"""
        script = (
            "EXPECTED_HASH="
            + repr(digest)
            + "\nEXPECTED_COUNTS="
            + repr(counts)
            + "\n"
            + script
        )
        print(
            self.kube(
                "exec", "-i", "data-import", "--", "python", "-", data=script.encode()
            ).strip(),
            flush=True,
        )
        self.scale("statefulset", ("postgres",), 1)
        self.wait_ready("statefulset", "postgres")
        self.retain_volumes()
        with (backup / "litellm.dump").open("rb") as incoming:
            self.kube(
                "exec",
                "-i",
                "postgres-0",
                "--",
                "pg_restore",
                "-U",
                "postgres",
                "-d",
                "litellm",
                "--single-transaction",
                "--no-owner",
                "--no-acl",
                "--role=litellm",
                stdin=incoming,
            )
        target_counts = self.kube(
            "exec",
            "-i",
            "postgres-0",
            "--",
            "psql",
            "-U",
            "postgres",
            "-d",
            "litellm",
            "-At",
            data=count_sql,
        )
        if target_counts != gateway_counts:
            raise RuntimeError(
                "LiteLLM 数据库表行数不一致；服务保持停止，请检查私有备份。"
            )
        print("LiteLLM 所有表行数与源备份一致", flush=True)
        self.kube("delete", "pod", "data-import", "--wait=true")
        self.retain_volumes()
        self.marker(
            "platform-release",
            {
                "status": "imported",
                "image": self.image,
                "source_database_sha256": digest,
                "backup_directory": str(backup),
            },
        )
        self.start()


def main():
    # Keep old command names usable, but never reconcile the monolithic stack
    # over Helm-owned resources. Only the historical one-time importer uses it.
    if len(sys.argv) > 1 and sys.argv[1] != "import-local":
        from agent_platform.scripts.helm import main as helm_main

        helm_main(sys.argv[1:])
        return
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=("import-local", "deploy", "status", "stop", "logs")
    )
    parser.add_argument("--context", default="")
    parser.add_argument("--namespace", default="agent-platform")
    parser.add_argument("--image", default=IMAGE)
    parser.add_argument("--env-file", type=Path, default=PLATFORM / ".env")
    parser.add_argument("--skip-build", action="store_true")
    args = parser.parse_args()
    try:
        cluster = Cluster(args)
        if args.command == "import-local":
            cluster.import_local()
        elif args.command == "deploy":
            cluster.deploy()
        elif args.command == "stop":
            cluster.drain()
            cluster.scale("deployment", (*ROLES, "litellm"), 0)
            cluster.wait_empty((*ROLES, "litellm"))
            cluster.scale("statefulset", ("postgres", "redis"), 0)
            print("工作负载已停止，PVC 与 Secret 保留。")
        elif args.command == "logs":
            print(cluster.kube("logs", "deployment/api", "--tail=100"))
        else:
            print(cluster.kube("get", "pods,services,pvc", "-o", "wide"))
    except (RuntimeError, subprocess.SubprocessError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
