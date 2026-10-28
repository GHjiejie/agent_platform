"""Dependency readiness without provider calls, credentials or tenant labels."""

import asyncio
import hashlib
import os
import socket
import time

from sqlalchemy import delete, insert, select, update

from agent_platform.infrastructure.runtime_tables import service_heartbeats

BACKGROUND_ROLES = {"worker", "gateway-sync", "maintenance"}


def service_instance_id(role):
    # Charts inject Pod UID so a healthy replica cannot hide another replica's
    # dead process. Native deployments may set their own unique instance name.
    instance = os.getenv("PLATFORM_SERVICE_INSTANCE") or socket.gethostname()
    return hashlib.sha256(f"{role}:{instance}".encode()).hexdigest()


async def run_service(db, role, service, stop):
    if role not in BACKGROUND_ROLES:
        raise ValueError("Unsupported background service role")
    identifier = service_instance_id(role)

    def beat():
        with db.transaction("health:" + identifier) as connection:
            connection.execute(
                delete(service_heartbeats).where(
                    service_heartbeats.c.role == role,
                    service_heartbeats.c.expires_at < time.time() - 300,
                )
            )
            exists = connection.scalar(
                select(service_heartbeats.c.id).where(
                    service_heartbeats.c.id == identifier
                )
            )
            if exists:
                connection.execute(
                    update(service_heartbeats)
                    .where(service_heartbeats.c.id == identifier)
                    .values(expires_at=time.time() + 30)
                )
            else:
                connection.execute(
                    insert(service_heartbeats).values(
                        id=identifier, role=role, expires_at=time.time() + 30
                    )
                )

    async def pulse():
        # Keep the heartbeat alive while a stopped worker drains in-flight runs.
        # The operation owns graceful shutdown; a SIGTERM must not let a finished
        # heartbeat task immediately cancel an external model call.
        while True:
            await asyncio.to_thread(beat)
            await asyncio.sleep(5)

    heartbeat = asyncio.create_task(pulse())
    operation = asyncio.create_task(service(stop))
    try:
        done, _pending = await asyncio.wait(
            [heartbeat, operation], return_when=asyncio.FIRST_COMPLETED
        )
        for task in done:
            await task
    finally:
        heartbeat.cancel()
        operation.cancel()
        await asyncio.gather(heartbeat, operation, return_exceptions=True)
        with db.transaction("health:" + identifier) as connection:
            connection.execute(
                delete(service_heartbeats).where(service_heartbeats.c.id == identifier)
            )


def readiness(db, settings, *, role="api"):
    """Process readiness checks only the service's own required dependencies."""
    db.assert_schema(saas=settings.mode == "saas")
    if role in BACKGROUND_ROLES:
        with db.read() as connection:
            live = connection.scalar(
                select(service_heartbeats.c.id).where(
                    service_heartbeats.c.id == service_instance_id(role),
                    service_heartbeats.c.role == role,
                    service_heartbeats.c.expires_at > time.time(),
                )
            )
        return {"status": "ready" if live else "unavailable", "service": role}
    if role != "api":
        raise ValueError("Unsupported readiness service role")
    if settings.redis_url:
        from redis import Redis

        client = Redis.from_url(
            settings.redis_url, socket_connect_timeout=2, socket_timeout=2
        )
        try:
            client.ping()
        finally:
            client.close()
    return {"status": "ready", "service": "api"}


def platform_status(db, settings):
    """Aggregate diagnostics are separate from individual service routing."""
    db.assert_schema(saas=settings.mode == "saas")
    with db.read() as connection:
        live = set(
            connection.scalars(
                select(service_heartbeats.c.role).where(
                    service_heartbeats.c.expires_at > time.time()
                )
            )
        )
    required = {"worker", "maintenance"}
    if settings.mode == "saas":
        required.add("gateway-sync")
    missing = sorted(required - live)
    return {
        "status": "ready" if not missing else "degraded",
        "missing_services": missing,
        "live_services": sorted(live & BACKGROUND_ROLES),
    }
