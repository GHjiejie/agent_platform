"""Explicit background service composition, sharing storage and domain libraries.

The gateway provisioner only constructs GatewayService. Workers need metering,
identity and routing. Maintenance needs identity and gateway revocation, and does
not construct the HTTP facade or a billing service/Redis client.
"""

from dataclasses import dataclass

from agent_platform.infrastructure.config import Settings
from agent_platform.infrastructure.db import Database
from agent_platform.modules.gateway_control import GatewayService
from agent_platform.modules.identity import IdentityService


@dataclass(frozen=True)
class MaintenanceServices:
    db: Database
    settings: Settings
    identity: IdentityService
    gateway: GatewayService

    def require_platform(self, connection, actor, capability):
        self.identity._platform_actor(connection, actor, capability)


@dataclass(frozen=True)
class WorkerServices(MaintenanceServices):
    billing: object


def maintenance_services(db: Database, settings: Settings) -> MaintenanceServices:
    return MaintenanceServices(
        db, settings, IdentityService(db, settings), GatewayService(db, settings)
    )


def worker_services(db: Database, settings: Settings) -> WorkerServices:
    from agent_platform.modules.billing.service import BillingService

    return WorkerServices(
        db,
        settings,
        IdentityService(db, settings),
        GatewayService(db, settings),
        BillingService(db, settings.redis_url),
    )
