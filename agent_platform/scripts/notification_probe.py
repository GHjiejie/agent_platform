"""Measure isolated SSE SQL calls against the previous 250ms polling loop.

Uses a temporary migrated SQLite database, real tenant/session authorization and
the API's streaming response. It performs no model request and never uses the
deployment database. Optional Redis requires the explicit acceptance isolation
gate. This is a local comparison, not production capacity evidence.
"""

import argparse
import asyncio
import json
import os
import tempfile
import threading
import time
from pathlib import Path

from sqlalchemy import event, update

from agent_platform.apps.api.main import COOKIE, LeasedStreamingResponse, create_app
from agent_platform.infrastructure import tables as t
from agent_platform.infrastructure.config import Settings
from agent_platform.modules.authorization import require_capability
from agent_platform.modules.platform import STREAM_LEASE_SECONDS
from agent_platform.modules.run_records import append_event
from agent_platform.tests.fixtures import MultiTenantFixture, migrate


class ProbeRequest:
    def __init__(self, token):
        self.cookies = {COOKIE: token}
        self.headers = {}

    async def is_disconnected(self):
        return False


async def measure(mode, clients, seconds, delta_interval, redis_url):
    with tempfile.TemporaryDirectory(prefix="platform-sse-probe-") as directory:
        settings = Settings(
            database_url=f"sqlite:///{Path(directory) / 'isolated.db'}",
            redis_url=redis_url,
            admin_email="probe@notifications.test",
            admin_password="temporary-isolated-probe-password",
            operator_emails=("probe@notifications.test",),
            litellm_url="http://127.0.0.1:1",
            export_directory=Path(directory) / "exports",
            web_directory=Path(directory) / "web",
        )
        migrate(settings.database_url)
        app = create_app(settings)
        db, platform = app.state.database, app.state.platform
        try:
            fixture = MultiTenantFixture(db, settings)
            user = fixture.owner
            token = platform.login(
                settings.admin_email, settings.admin_password, "isolated-probe"
            )["token"]
            request = ProbeRequest(token)
            session = fixture.create_session(user)
            run = fixture.create_run(user, session)
            with db.transaction("tenant:" + user["tenant_id"]) as connection:
                connection.execute(
                    update(t.runs)
                    .where(t.runs.c.id == run["id"])
                    .values(status="running", finished_at=None)
                )
            endpoint = next(
                route.endpoint
                for route in app.routes
                if route.path == "/api/v1/runs/{run_id}/events"
            )
            responses = []
            for _ in range(clients):
                if mode == "notifications":
                    responses.append(
                        await endpoint(run["id"], request, after=0, user=user)
                    )
                    continue
                lease = platform.acquire_stream(user, run["id"])

                async def renew(lease=lease):
                    await asyncio.to_thread(platform.authenticate, token)
                    await asyncio.to_thread(platform.update_stream, user, lease)

                async def release(lease=lease):
                    await asyncio.to_thread(
                        platform.update_stream, user, lease, release=True
                    )

                async def original_poll():
                    cursor = 0
                    while True:
                        current = await asyncio.to_thread(platform.authenticate, token)
                        context = await asyncio.to_thread(
                            platform.identity.context,
                            current["user"]["id"],
                            user["tenant_id"],
                        )
                        require_capability(context, "runs.read_own")
                        rows = await asyncio.to_thread(
                            platform.events, context, run["id"], cursor
                        )
                        for row in rows:
                            cursor = row["sequence"]
                            yield f"id: {cursor}\nevent: {row['type']}\ndata: {json.dumps(row)}\n\n"
                        await asyncio.to_thread(platform.get_run, context, run["id"])
                        await asyncio.sleep(0.25)

                responses.append(
                    LeasedStreamingResponse(
                        original_poll(),
                        renew=renew,
                        release=release,
                        valid_until=time.monotonic() + STREAM_LEASE_SECONDS,
                    )
                )
            counts = {
                "select_statements": 0,
                "event_replay_queries": 0,
                "frames_sent": 0,
            }
            lock = threading.Lock()

            def count_sql(_conn, _cursor, statement, *_args):
                sql = " ".join(statement.lower().split())
                if sql.startswith("select"):
                    with lock:
                        counts["select_statements"] += 1
                        if "platform_run_events.sequence >" in sql:
                            counts["event_replay_queries"] += 1

            event.listen(db.engine, "before_cursor_execute", count_sql)

            async def send(message):
                if message.get("body"):
                    counts["frames_sent"] += message["body"].count(b"\nid:") + int(
                        message["body"].startswith(b"id:")
                    )

            stop = asyncio.Event()

            async def producer():
                while delta_interval and not stop.is_set():
                    try:
                        await asyncio.wait_for(stop.wait(), delta_interval)
                        return
                    except TimeoutError:

                        def write():
                            with db.transaction(
                                "tenant:" + user["tenant_id"]
                            ) as connection:
                                append_event(
                                    connection,
                                    run["id"],
                                    "message.delta",
                                    {"text": "isolated delta"},
                                )

                        await asyncio.to_thread(write)

            tasks = [
                asyncio.create_task(response.stream_response(send))
                for response in responses
            ]
            writer = asyncio.create_task(producer())
            started = time.monotonic()
            await asyncio.sleep(seconds)
            elapsed = time.monotonic() - started
            # Capture before cleanup so lease release does not distort comparisons.
            with lock:
                result = {
                    **counts,
                    "seconds": round(elapsed, 3),
                    "clients": clients,
                    "mode": mode,
                }
            stop.set()
            await writer
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            event.remove(db.engine, "before_cursor_execute", count_sql)
            if failures := [
                task.exception()
                for task in tasks
                if not task.cancelled() and task.exception()
            ]:
                raise RuntimeError(f"Probe stream failed: {type(failures[0]).__name__}")
            result["scope"] = (
                "isolated SQLite, API stream_response and real identity reads"
            )
            return result
        finally:
            db.close()


async def run(args):
    redis_url = None
    if args.redis:
        redis_url = os.getenv("PLATFORM_ACCEPTANCE_REDIS_URL")
        if not redis_url or os.getenv("PLATFORM_ACCEPTANCE_REDIS_ISOLATED") != "1":
            raise RuntimeError(
                "Redis probe requires explicitly isolated acceptance Redis"
            )
    baseline = await measure(
        "previous_polling", args.clients, args.seconds, args.delta_interval, redis_url
    )
    current = await measure(
        "notifications", args.clients, args.seconds, args.delta_interval, redis_url
    )
    return {
        "baseline": baseline,
        "current": current,
        "replay_query_reduction_percent": round(
            100
            * (1 - current["event_replay_queries"] / baseline["event_replay_queries"]),
            2,
        ),
        "select_reduction_percent": round(
            100 * (1 - current["select_statements"] / baseline["select_statements"]), 2
        ),
        "redis": "isolated acceptance Redis"
        if redis_url
        else "disabled, local commits and bounded DB polling",
        "delta_interval_seconds": args.delta_interval,
        "limitations": "Local synthetic comparison; no production throughput, PostgreSQL lock or network capacity claim.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clients", type=int, default=8)
    parser.add_argument("--seconds", type=float, default=4)
    parser.add_argument("--delta-interval", type=float, default=0)
    parser.add_argument("--redis", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if (
        not 1 <= args.clients <= 20
        or not 1 <= args.seconds <= 30
        or args.delta_interval < 0
    ):
        parser.error("Use 1-20 clients, 1-30 seconds and a nonnegative delta interval")
    result = asyncio.run(run(args))
    payload = json.dumps(result, indent=2, ensure_ascii=False) + "\n"
    if args.output:
        args.output.write_text(payload)
    print(payload, end="")


if __name__ == "__main__":
    main()
