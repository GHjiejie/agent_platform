"""Export/closure maintenance, without database migration or automatic restore.

Before reopening a restored environment, run --replay-tombstones with the external
tombstone volume mounted, reconcile newer financial facts, and review credentials.
This command never restores a backup or replays inference requests.
"""

import argparse
import asyncio
import json
import signal

from agent_platform.infrastructure.config import Settings
from agent_platform.infrastructure.db import Database
from agent_platform.modules.health import run_service
from agent_platform.modules.operations import OperationsService
from agent_platform.modules.service_contexts import maintenance_services


async def run(args):
    settings = Settings.from_env(role="maintenance")
    db = Database(settings.database_url)
    try:
        db.assert_schema(saas=settings.mode == "saas")
        service = OperationsService(maintenance_services(db, settings))
        if args.replay_tombstones:
            print(json.dumps(service.replay_tombstones()))
            return
        if args.confirm_legacy_revocation:
            print(
                json.dumps(
                    service.confirm_legacy_revocation(args.confirm_legacy_revocation)
                )
            )
            return
        if args.once:
            print(json.dumps(service.process_batch()))
            return
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(signum, stop.set)
        await run_service(db, "maintenance", service.serve, stop)
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--once", action="store_true")
    group.add_argument("--replay-tombstones", action="store_true")
    group.add_argument("--confirm-legacy-revocation", metavar="TENANT_ID")
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
