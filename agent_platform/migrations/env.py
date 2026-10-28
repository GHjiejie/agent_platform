"""Run platform migrations independently of the LiteLLM database."""

import os
from logging.config import fileConfig

from alembic import context

from agent_platform.infrastructure import (  # noqa: F401
    gateway_tables,
    operations_tables,
    runtime_tables,
    support_tables,
    tables,
    tenancy_tables,
)
from agent_platform.infrastructure.db import Database, metadata
from agent_platform.modules.billing import service

config = context.config
if config.config_file_name:
    fileConfig(config.config_file_name)


def render_item(kind, obj, autogen_context):
    if kind == "type" and isinstance(obj, service.ExactMoney):
        return "sa.Numeric(30, 12).with_variant(sa.String(64), 'sqlite')"
    return False


url = os.getenv("PLATFORM_DATABASE_URL", config.get_main_option("sqlalchemy.url"))
if context.is_offline_mode():
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    context.configure(
        url=url,
        target_metadata=metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_item=render_item,
    )
    with context.begin_transaction():
        context.run_migrations()
else:
    db = Database(url)
    with db.engine.connect() as connection:
        if db.sqlite:
            # SQLite batch ALTER rebuilds a referenced table. Disable FK checking
            # outside a transaction, then verify every reference before committing.
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
            connection.commit()
        context.configure(
            connection=connection,
            target_metadata=metadata,
            render_as_batch=db.sqlite,
            render_item=render_item,
        )
        try:
            with context.begin_transaction():
                context.run_migrations()
                if (
                    db.sqlite
                    and connection.exec_driver_sql("PRAGMA foreign_key_check").first()
                ):
                    raise RuntimeError("Migration left invalid foreign key references.")
        finally:
            if db.sqlite:
                connection.rollback()
                connection.exec_driver_sql("PRAGMA foreign_keys=ON")
    db.close()
