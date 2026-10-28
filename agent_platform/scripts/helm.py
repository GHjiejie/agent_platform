"""Independent Helm releases for the existing OrbStack installation.

Charts also work directly on other clusters. This convenience CLI deliberately
retains the local-only guard and never reads or rewrites legacy credentials.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import yaml

from agent_platform.scripts.kubernetes import (
    PLATFORM,
    ROOT,
    STATE,
    Cluster,
    private_json,
    run,
)

CHARTS = PLATFORM / "charts"
APPLICATIONS = ("api", "worker", "gateway-sync", "maintenance")
SERVICES = ("storage", "postgres", "redis", "litellm", *APPLICATIONS, "web")
BUILD_TARGETS = (*APPLICATIONS, "migration", "web")
IMAGE = "agent-platform:0.3.0-helm"
KINDS = {
    "Deployment",
    "StatefulSet",
    "Service",
    "ConfigMap",
    "PersistentVolumeClaim",
    "Job",
}


def service_image(image: str, service: str) -> tuple[str, str]:
    repository, separator, tag = image.rpartition(":")
    if not separator or "/" in tag or "@" in image or not repository or not tag:
        raise ValueError("--image 需要带 tag 的镜像名，例如 agent-platform:0.3.0-helm")
    return f"{repository}-{service}", tag


def selected_services(service: str) -> tuple[str, ...]:
    return SERVICES if service == "all" else (service,)


class HelmCluster(Cluster):
    def __init__(self, args):
        super().__init__(args)
        self.helm_base = ["helm", "--kube-context", self.context, "-n", self.namespace]
        self.build_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        # Helm 4 defaults to SSA. The imported stack has another SSA field owner;
        # use client-side Helm patches rather than forcing conflicts or replacing
        # StatefulSets/PVCs. Helm 3 already uses this update strategy.
        version = run(["helm", "version", "--template", "{{.Version}}"])
        self.apply_options = (
            ["--server-side=false"] if version.startswith("v4.") else []
        )

    def helm(self, *args, **kwargs):
        return run([*self.helm_base, *args], **kwargs)

    def release(self, service):
        return f"platform-{service}"

    def values(self, service):
        arguments = ["-f", str(CHARTS / service / "values.local.yaml")]
        if self.args.values:
            if self.args.service == "all":
                raise RuntimeError(
                    "额外 --values 只用于一个 --service；全栈请逐服务配置。"
                )
            arguments.extend(("-f", str(self.args.values)))
        if service in BUILD_TARGETS:
            repository, tag = service_image(self.image, service)
            arguments.extend(
                (
                    "--set-string",
                    f"image.repository={repository}",
                    "--set-string",
                    f"image.tag={tag}",
                )
            )
            if not self.args.skip_build:
                arguments.extend(
                    ("--set-string", f"podAnnotations.platform-build={self.build_id}")
                )
        return arguments

    def render(self, service, *, release=None, extra=()):
        output = self.helm(
            "template",
            release or self.release(service),
            str(CHARTS / service),
            *self.values(service),
            *extra,
        )
        resources = [item for item in yaml.safe_load_all(output) if item]
        for item in resources:
            if item["kind"] not in KINDS:
                raise RuntimeError(
                    f"Chart {service} 包含不支持接管的资源类型 {item['kind']}"
                )
        return resources

    def build_services(self, services):
        for service in services:
            if service not in BUILD_TARGETS:
                continue
            repository, tag = service_image(self.image, service)
            target = "frontend" if service == "web" else service
            dockerfile = "Dockerfile.web" if service == "web" else "Dockerfile"
            print(f"构建 {service} 镜像 {repository}:{tag}…", flush=True)
            subprocess.run(
                [
                    "docker",
                    "build",
                    "--target",
                    target,
                    "-f",
                    str(PLATFORM / "deploy" / dockerfile),
                    "-t",
                    f"{repository}:{tag}",
                    str(ROOT),
                ],
                check=True,
            )

    def check_ownership(self, service, desired, existing, *, adopt):
        """Never steal another release or change immutable data boundaries."""
        metadata = existing["metadata"]
        owner = metadata.get("annotations", {}).get("meta.helm.sh/release-name")
        owner_namespace = metadata.get("annotations", {}).get(
            "meta.helm.sh/release-namespace"
        )
        if owner and (
            owner != self.release(service) or owner_namespace != self.namespace
        ):
            raise RuntimeError(
                f"{desired['kind']}/{metadata['name']} 已属于其他 Helm release"
            )
        if not owner and not adopt:
            raise RuntimeError(
                f"{desired['kind']}/{metadata['name']} 尚未由 Helm 管理；首次请运行 make helm-adopt"
            )
        kind = desired["kind"]
        actual, wanted = existing.get("spec", {}), desired.get("spec", {})
        if kind in {"Deployment", "StatefulSet", "Service"} and actual.get(
            "selector"
        ) != wanted.get("selector"):
            raise RuntimeError(f"{metadata['name']} 的不可变 selector 不一致，拒绝替换")
        if kind == "StatefulSet":
            if actual.get("serviceName") != wanted.get("serviceName"):
                raise RuntimeError(f"{metadata['name']} 的持久服务名不一致")

            def claims(spec):
                return [
                    (
                        c["metadata"]["name"],
                        c["spec"].get("storageClassName"),
                        c["spec"]["accessModes"],
                        c["spec"]["resources"]["requests"]["storage"],
                        c["spec"].get("volumeMode", "Filesystem"),
                    )
                    for c in spec.get("volumeClaimTemplates", [])
                ]

            if claims(actual) != claims(wanted):
                raise RuntimeError(f"{metadata['name']} 的持久卷模板不一致，拒绝替换")
        if kind == "PersistentVolumeClaim":
            for key in ("accessModes", "storageClassName"):
                if actual.get(key) != wanted.get(key):
                    raise RuntimeError(f"PVC/{metadata['name']} 的 {key} 不一致")
            if (
                actual["resources"]["requests"]["storage"]
                != wanted["resources"]["requests"]["storage"]
            ):
                raise RuntimeError(f"PVC/{metadata['name']} 容量变化需要单独处理")
            if actual.get("volumeMode", "Filesystem") != wanted.get(
                "volumeMode", "Filesystem"
            ):
                raise RuntimeError(f"PVC/{metadata['name']} 的 volumeMode 不一致")

    def preflight(
        self, services, *, adopt=False, snapshot=True, release=None, extra=()
    ):
        snapshots = []
        for service in services:
            self.helm("lint", str(CHARTS / service), *self.values(service))
            for desired in self.render(service, release=release, extra=extra):
                name = desired["metadata"]["name"]
                existing = self.get(desired["kind"], name)
                if existing:
                    self.check_ownership(service, desired, existing, adopt=adopt)
                    snapshots.append(existing)
                pod = desired.get("spec", {}).get("template", {}).get("spec", {})
                secrets = set()
                claims = set()
                for container in (
                    *pod.get("containers", []),
                    *pod.get("initContainers", []),
                ):
                    for ref in container.get("envFrom", []):
                        if ref.get("secretRef"):
                            secrets.add(ref["secretRef"]["name"])
                    for variable in container.get("env", []):
                        ref = variable.get("valueFrom", {}).get("secretKeyRef")
                        if ref:
                            secrets.add(ref["name"])
                for volume in pod.get("volumes", []):
                    if volume.get("persistentVolumeClaim"):
                        claims.add(volume["persistentVolumeClaim"]["claimName"])
                for secret in secrets:
                    # Metadata only; credentials never enter Helm release values.
                    present = self.kube(
                        "get", "secret", secret, "--ignore-not-found", "-o", "name"
                    ).strip()
                    if not present:
                        raise RuntimeError(f"{service} 缺少已有 Secret/{secret}")
                for claim in claims:
                    if not self.get("pvc", claim):
                        raise RuntimeError(
                            f"{service} 缺少已有 PVC/{claim}，请先部署 storage"
                        )
        if adopt and snapshot:
            path = STATE / f"helm-adoption-{self.build_id}.json"
            private_json(
                path,
                {
                    "context": self.context,
                    "namespace": self.namespace,
                    "resources": snapshots,
                },
            )
            print(f"已保存接管前非敏感资源清单：{path}", flush=True)

    def upgrade(self, service, *, adopt=False, extra=()):
        if adopt:
            # A build can take minutes; recheck ownership before takeover.
            self.preflight((service,), adopt=True, snapshot=False)
        # Helm 4.0's `upgrade --install` does not forward its server-side mode
        # to the first install. Use install explicitly so adoption also uses the
        # client-side strategy and never forces the old SSA field owner.
        exists = bool(
            self.helm(
                "list",
                "--filter",
                f"^{self.release(service)}$",
                "--deployed",
                "--failed",
                "--pending",
                "-q",
            ).strip()
        )
        arguments = [
            "upgrade" if exists else "install",
            self.release(service),
            str(CHARTS / service),
            *getattr(self, "apply_options", ()),
            *(("--reuse-values", "--history-max", "10") if exists else ()),
            *self.values(service),
            "--wait",
            "--timeout",
            "420s",
            *extra,
        ]
        if adopt:
            # Service port is a strategic merge list key. During the first
            # takeover Helm has no old manifest, so a changed 8010 -> 8000
            # port would be appended and duplicate the name "http". Replace
            # only this list after ownership/selector checks; retain ClusterIP.
            for item in self.render(service):
                if item["kind"] != "Service":
                    continue
                name = item["metadata"]["name"]
                existing = self.get("Service", name)
                if existing and {p["port"] for p in existing["spec"]["ports"]} != {
                    p["port"] for p in item["spec"]["ports"]
                }:
                    self.kube(
                        "patch",
                        "service",
                        name,
                        "--type=merge",
                        "-p",
                        json.dumps(
                            {
                                "spec": {
                                    "type": item["spec"].get("type", "ClusterIP"),
                                    "ports": item["spec"]["ports"],
                                }
                            }
                        ),
                    )
            arguments.append("--take-ownership")
        self.helm(*arguments, timeout=460)
        print(f"{self.release(service)} 已就绪", flush=True)

    def deploy_releases(self, *, adopt=False):
        services = selected_services(self.args.service)
        self.preflight(services, adopt=adopt)
        if not self.args.skip_build:
            self.build_services(services)
        for service in services:
            self.upgrade(service, adopt=adopt)
        self.retain_service_volumes(services)
        marker = self.get("configmap", "platform-release")
        if marker:
            self.kube(
                "patch",
                "configmap",
                "platform-release",
                "--type=merge",
                "-p",
                json.dumps(
                    {
                        "data": {
                            "manager": "helm",
                            "deployed_at": datetime.now(UTC).isoformat(),
                        },
                    }
                ),
            )
        private_json(
            STATE / "deployment.json",
            {
                "context": self.context,
                "namespace": self.namespace,
                "manager": "helm",
                "image": self.image,
            },
        )
        print("独立 Helm 发布完成。迁移 Job 仅由 make migrate 显式执行。", flush=True)

    def retain_service_volumes(self, services):
        claims = set()
        for service in services:
            for item in self.render(service):
                spec = item.get("spec", {})
                if item["kind"] == "PersistentVolumeClaim":
                    claims.add(item["metadata"]["name"])
                for volume in (
                    spec.get("template", {}).get("spec", {}).get("volumes", [])
                ):
                    if volume.get("persistentVolumeClaim"):
                        claims.add(volume["persistentVolumeClaim"]["claimName"])
                if item["kind"] == "StatefulSet":
                    for template in spec.get("volumeClaimTemplates", []):
                        for ordinal in range(spec.get("replicas", 1)):
                            claims.add(
                                f"{template['metadata']['name']}-{item['metadata']['name']}-{ordinal}"
                            )
        for name in sorted(claims):
            claim = self.get("pvc", name)
            volume = (claim or {}).get("spec", {}).get("volumeName")
            if volume:
                pv = self.get("pv", volume)
                if pv and pv["spec"].get("persistentVolumeReclaimPolicy") != "Retain":
                    self.kube(
                        "patch",
                        "pv",
                        volume,
                        "--type=merge",
                        "-p",
                        '{"spec":{"persistentVolumeReclaimPolicy":"Retain"}}',
                    )

    def stop_releases(self):
        services = selected_services(self.args.service)
        if self.args.service == "all":
            # OrbStack profile still has SQLite; retain the proven local drain.
            self.drain()
            services = ("web", *APPLICATIONS, "litellm", "redis", "postgres")
        for service in services:
            if service == "storage":
                raise RuntimeError("storage 只保留 PVC，没有可停止的工作负载")
            self.helm(
                "upgrade",
                self.release(service),
                str(CHARTS / service),
                "--reuse-values",
                *getattr(self, "apply_options", ()),
                "--set",
                "replicaCount=0",
                "--wait",
                "--timeout",
                "420s",
                timeout=460,
            )
            print(f"{service} 已停止，release 与数据保留", flush=True)

    def start_releases(self):
        for service in selected_services(self.args.service):
            if service == "storage":
                continue
            self.helm(
                "upgrade",
                self.release(service),
                str(CHARTS / service),
                "--reuse-values",
                *getattr(self, "apply_options", ()),
                "--set",
                "replicaCount=1",
                "--wait",
                "--timeout",
                "420s",
                timeout=460,
            )
            print(f"{service} 已恢复，保留原 release 配置与镜像", flush=True)

    def migrate(self):
        # SQLite schema updates require exclusive use; migration is never hidden
        # in an API/worker release. Finish runs before stopping all app writers.
        version = self.build_id.lower()
        release = f"platform-migration-{version}"
        extra = ("--set-string", f"job.version={version}")
        self.preflight(("migration",), release=release, extra=extra)
        if not self.args.skip_build:
            self.build_services(("migration",))
        self.drain()
        for service in APPLICATIONS:
            self.helm(
                "upgrade",
                self.release(service),
                str(CHARTS / service),
                *getattr(self, "apply_options", ()),
                "--reuse-values",
                "--set",
                "replicaCount=0",
                "--wait",
                "--timeout",
                "420s",
                timeout=460,
            )
        self.helm(
            "install",
            release,
            str(CHARTS / "migration"),
            *getattr(self, "apply_options", ()),
            *self.values("migration"),
            "--set-string",
            f"job.version={version}",
            "--wait",
            "--wait-for-jobs",
            "--timeout",
            "360s",
            timeout=400,
        )
        print(f"{release} 迁移完成；运行 make resume 恢复应用。")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=(
            "deploy",
            "adopt",
            "validate",
            "status",
            "stop",
            "resume",
            "logs",
            "migrate",
        ),
    )
    parser.add_argument("--service", choices=("all", *SERVICES), default="all")
    parser.add_argument("--context", default="orbstack")
    parser.add_argument("--namespace", default="agent-platform")
    parser.add_argument("--image", default=IMAGE)
    parser.add_argument(
        "--env-file", type=Path, default=PLATFORM / ".env", help=argparse.SUPPRESS
    )
    parser.add_argument("--values", type=Path)
    parser.add_argument("--skip-build", action="store_true")
    args = parser.parse_args(argv)
    try:
        cluster = HelmCluster(args)
        if args.command in {"deploy", "adopt"}:
            cluster.deploy_releases(adopt=args.command == "adopt")
        elif args.command == "validate":
            for service in (
                *selected_services(args.service),
                *(("migration",) if args.service == "all" else ()),
            ):
                cluster.helm("lint", str(CHARTS / service), *cluster.values(service))
                cluster.render(service)
                print(f"{service} lint/template 通过")
        elif args.command == "migrate":
            cluster.migrate()
        elif args.command == "stop":
            cluster.stop_releases()
        elif args.command == "resume":
            cluster.start_releases()
        elif args.command == "logs":
            service = "api" if args.service == "all" else args.service
            if service == "storage":
                raise RuntimeError("storage 没有日志")
            kind = "statefulset" if service in {"postgres", "redis"} else "deployment"
            print(cluster.kube("logs", f"{kind}/{service}", "--tail=100"))
        else:
            print(cluster.helm("list"))
            print(cluster.kube("get", "pods,services,pvc", "-o", "wide"))
    except (RuntimeError, ValueError, subprocess.SubprocessError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
