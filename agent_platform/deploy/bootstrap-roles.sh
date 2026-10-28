#!/bin/sh
set -eu

# New data directories only. Existing clusters run this explicitly in their
# approved role-provisioning window before migration 0010 (see operations guide).
# Passwords are read by psql and never passed as command arguments.
psql --username "${POSTGRES_USER:-postgres}" --dbname "${POSTGRES_DB:-postgres}" --set ON_ERROR_STOP=1 <<'SQL'
\getenv migration_password PLATFORM_MIGRATION_DB_PASSWORD
\getenv api_password PLATFORM_API_DB_PASSWORD
\getenv worker_password PLATFORM_WORKER_DB_PASSWORD
\getenv sync_password PLATFORM_GATEWAY_SYNC_DB_PASSWORD
\getenv maintenance_password PLATFORM_MAINTENANCE_DB_PASSWORD
\getenv litellm_password LITELLM_DB_PASSWORD
SELECT format('CREATE ROLE %I LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS', role)
FROM (VALUES ('platform_migrator'), ('platform_api'), ('platform_worker'), ('platform_gateway_sync'), ('platform_maintenance'), ('litellm')) AS roles(role)
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname=role) \gexec
SELECT format('ALTER ROLE %I LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS PASSWORD %L', role, password)
FROM (VALUES ('platform_migrator', :'migration_password'), ('platform_api', :'api_password'), ('platform_worker', :'worker_password'), ('platform_gateway_sync', :'sync_password'), ('platform_maintenance', :'maintenance_password'), ('litellm', :'litellm_password')) AS credentials(role, password) \gexec
SELECT 'CREATE DATABASE platform OWNER platform_migrator'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname='platform') \gexec
SELECT 'CREATE DATABASE litellm OWNER litellm'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname='litellm') \gexec
REVOKE CONNECT ON DATABASE platform FROM PUBLIC;
REVOKE CONNECT ON DATABASE litellm FROM PUBLIC;
GRANT CONNECT ON DATABASE platform TO platform_migrator, platform_api, platform_worker, platform_gateway_sync, platform_maintenance;
GRANT CONNECT ON DATABASE litellm TO litellm;
\connect platform
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE, CREATE ON SCHEMA public TO platform_migrator;
GRANT USAGE ON SCHEMA public TO platform_api, platform_worker, platform_gateway_sync, platform_maintenance;
-- No blanket/default table grants. Migration 0010 grants the reviewed matrix.
-- Remove former broad defaults for the old role without revoking running old
-- processes during provisioning. Retire its credential after service cutover.
SELECT 'ALTER DEFAULT PRIVILEGES FOR ROLE platform_migrator IN SCHEMA public REVOKE ALL ON TABLES FROM platform_runtime'
WHERE EXISTS (SELECT FROM pg_roles WHERE rolname='platform_runtime') \gexec
SELECT 'ALTER DEFAULT PRIVILEGES FOR ROLE platform_migrator IN SCHEMA public REVOKE ALL ON SEQUENCES FROM platform_runtime'
WHERE EXISTS (SELECT FROM pg_roles WHERE rolname='platform_runtime') \gexec
SQL
