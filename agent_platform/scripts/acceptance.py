"""Run the current suite with an explicit, disposable PostgreSQL release gate.

Usage: python -m agent_platform.scripts.acceptance --report /tmp/acceptance.json
The default `all` mode requires real PostgreSQL 17, isolated Redis, Docker and Helm.
Set PLATFORM_ACCEPTANCE_DOCKER_ISOLATED=1 to permit a fresh temporary PostgreSQL
container for the actual role bootstrap/upgrade gate. It is removed after testing.
`postgres` is a focused database check; `local` is offline development validation.
Neither focused mode constitutes a passed release gate. Skips fail every mode.
This command never falls back to PLATFORM_DATABASE_URL or PLATFORM_REDIS_URL.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import unittest
from datetime import UTC, datetime
from pathlib import Path

from redis import Redis
from sqlalchemy import create_engine, text

from agent_platform.tests.test_postgres_isolation import (
    SERVICES,
    isolated_postgres_url,
)

ROOT = Path(__file__).resolve().parents[2]
TESTS = ROOT / "agent_platform/tests"


def preflight(mode):
    if mode != "local":
        if os.environ.get(
            "PLATFORM_ACCEPTANCE_DOCKER_ISOLATED"
        ) != "1" or not shutil.which("docker"):
            raise RuntimeError(
                "The role bootstrap gate requires explicit disposable Docker opt-in"
            )
        subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            check=True,
            timeout=10,
        )
        url = isolated_postgres_url()
        engine = create_engine(url, pool_pre_ping=True)
        try:
            with engine.connect() as connection:
                major = int(connection.scalar(text("SHOW server_version_num"))) // 10000
                if major != 17:
                    raise RuntimeError("The release gate requires PostgreSQL 17")
        finally:
            engine.dispose()
    if mode == "all":
        if not shutil.which("helm"):
            raise RuntimeError("Helm is required; chart contract skips cannot pass")
        redis_url = os.environ.get("PLATFORM_ACCEPTANCE_REDIS_URL", "")
        if not redis_url or os.environ.get("PLATFORM_ACCEPTANCE_REDIS_ISOLATED") != "1":
            raise RuntimeError("The release gate requires an explicitly isolated Redis")
        client = Redis.from_url(redis_url, socket_connect_timeout=5, socket_timeout=5)
        try:
            if not client.ping():
                raise RuntimeError("Isolated Redis did not respond")
        finally:
            client.close()


def suite_for(mode):
    loader = unittest.TestLoader()
    if mode == "postgres":
        return loader.loadTestsFromName("agent_platform.tests.test_postgres_isolation")
    names = [
        "agent_platform.tests." + path.stem
        for path in sorted(TESTS.glob("test_*.py"))
        if mode != "local" or path.stem != "test_postgres_isolation"
    ]
    return loader.loadTestsFromNames(names)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("all", "postgres", "local"), default="all")
    parser.add_argument(
        "--postgres-role",
        choices=SERVICES,
        default="api",
        help="First runtime role inspected; every service role remains mandatory",
    )
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    os.environ["PLATFORM_ACCEPTANCE_RUNTIME_ROLE"] = args.postgres_role
    report = {
        "checked_at": datetime.now(UTC).isoformat(),
        "mode": args.mode,
        "runtime_role": args.postgres_role,
        "passed": False,
        "release_gate_passed": False,
        "postgres_verified": False,
        "role_bootstrap_verified": False,
        "redis_verified": False,
        "tests_run": 0,
        "failures": 0,
        "errors": 0,
        "skipped": 0,
    }
    try:
        preflight(args.mode)
        result = unittest.TextTestRunner(verbosity=2).run(suite_for(args.mode))
        passed = bool(result.testsRun) and result.wasSuccessful() and not result.skipped
        report.update(
            passed=passed,
            release_gate_passed=passed and args.mode == "all",
            postgres_verified=passed and args.mode != "local",
            role_bootstrap_verified=passed and args.mode != "local",
            redis_verified=passed and args.mode == "all",
            tests_run=result.testsRun,
            failures=len(result.failures),
            errors=len(result.errors),
            skipped=len(result.skipped),
        )
        if result.skipped:
            print("Acceptance failed: skipped checks are unverified.", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001 - boundary sanitizes credential-bearing errors
        # Reports contain no URLs, database credentials, SQL or exception payloads.
        report["preflight_error"] = type(exc).__name__
        print(
            "Acceptance prerequisites failed. Configure disposable PostgreSQL 17 "
            "and fresh Docker bootstrap; all checks also need isolated Redis and Helm. "
            f"Failure type: {type(exc).__name__}.",
            file=sys.stderr,
        )
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, sort_keys=True))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
