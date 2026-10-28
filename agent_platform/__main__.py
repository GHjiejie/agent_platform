"""Run from the repository root: uv run python -m agent_platform serve."""

import argparse
import asyncio
import logging
import os
import signal

from agent_platform.infrastructure.config import Settings
from agent_platform.infrastructure.db import Database


async def run_background(db, role, service):
    from agent_platform.modules.health import run_service

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(signum, stop.set)
        except NotImplementedError:
            pass
    await run_service(db, role, service.serve, stop)


def main() -> None:
    parser = argparse.ArgumentParser(description="Agent Platform")
    parser.add_argument(
        "command",
        choices=["serve", "worker", "gateway-sync", "maintenance", "init"],
        nargs="?",
        default="serve",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8000, type=int)
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    settings = Settings.from_env(
        role="api" if args.command == "serve" else args.command
    )
    if args.command in {"serve", "worker", "maintenance"} and any(
        os.getenv(key)
        for key in (
            "PLATFORM_GATEWAY_CONTROL_KEY",
            "LITELLM_MASTER_KEY",
            "UPSTREAM_API_KEY",
            "OPENAI_API_KEY",
            "PLATFORM_MIGRATION_DB_PASSWORD",
            "POSTGRES_PASSWORD",
            "PLATFORM_DB_PASSWORD",
            "PLATFORM_ADMIN_PASSWORD",
            "LITELLM_DB_PASSWORD",
            "LITELLM_SALT_KEY",
            "UI_PASSWORD",
        )
    ):
        raise RuntimeError(
            "运行进程不应接收网关控制、上游或数据库迁移凭据；请使用隔离启动环境。"
        )
    if args.command == "serve":
        import uvicorn

        from agent_platform.apps.api.main import create_app

        uvicorn.run(create_app(settings), host=args.host, port=args.port)
    else:
        db = Database(settings.database_url)
        try:
            # Schema changes belong exclusively to Alembic. Explicit init is an
            # operator action and may use the migration owner; runtime may not.
            db.assert_schema(saas=settings.mode == "saas" and args.command != "init")
            if args.command == "init":
                from agent_platform.modules.platform import Platform

                platform = Platform(db, settings)
                platform.bootstrap()
                print("平台已初始化；新组织余额为零。")
            else:
                if args.command == "worker":
                    from agent_platform.apps.worker.main import Worker
                    from agent_platform.modules.service_contexts import worker_services

                    service = Worker(worker_services(db, settings))
                elif args.command == "gateway-sync":
                    from agent_platform.modules.gateway_control import GatewayService

                    service = GatewayService(db, settings)
                else:
                    from agent_platform.modules.operations import OperationsService
                    from agent_platform.modules.service_contexts import (
                        maintenance_services,
                    )

                    service = OperationsService(maintenance_services(db, settings))
                try:
                    asyncio.run(run_background(db, args.command, service))
                except KeyboardInterrupt:
                    pass
        finally:
            db.close()


if __name__ == "__main__":
    main()
