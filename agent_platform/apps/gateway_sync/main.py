"""Run with python -m agent_platform.apps.gateway_sync.main after migrations."""

import asyncio
import signal

from agent_platform.infrastructure.config import Settings
from agent_platform.infrastructure.db import Database
from agent_platform.modules.gateway_control import GatewayService
from agent_platform.modules.health import run_service


async def run():
    settings = Settings.from_env(role="gateway-sync")
    db = Database(settings.database_url)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stop.set)
    service = GatewayService(db, settings)
    try:
        db.assert_schema(saas=settings.mode == "saas")
        # serve validates strict-mode control configuration without sending calls.
        await run_service(db, "gateway-sync", service.serve, stop)
    finally:
        db.close()


def main():
    asyncio.run(run())


if __name__ == "__main__":
    main()
