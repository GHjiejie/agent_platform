"""Check the current background service instance; no provider traffic.

Usage: python -m agent_platform.scripts.service_probe worker
Kubernetes injects PLATFORM_SERVICE_INSTANCE from metadata.uid. Each native
replica should configure its own instance name when using these probes.
"""

import argparse
import sys

from agent_platform.infrastructure.config import Settings
from agent_platform.infrastructure.db import Database
from agent_platform.modules.health import BACKGROUND_ROLES, readiness


def probe(role):
    db = None
    try:
        settings = Settings.from_env(role=role)
        db = Database(settings.database_url)
        return readiness(db, settings, role=role)["status"] == "ready"
    except Exception:  # noqa: BLE001 - probe never emits credentials or connection details
        return False
    finally:
        if db is not None:
            db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("role", choices=sorted(BACKGROUND_ROLES))
    role = parser.parse_args().role
    if not probe(role):
        print(f"{role} unavailable", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
