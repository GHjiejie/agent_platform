#!/bin/sh
set -eu

# psql variables quote passwords as SQL literals, not interpolated SQL source.
psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  --set ON_ERROR_STOP=1 \
  --set platform_password="$PLATFORM_DB_PASSWORD" \
  --set litellm_password="$LITELLM_DB_PASSWORD" <<'SQL'
SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', 'platform', :'platform_password') \gexec
SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', 'litellm', :'litellm_password') \gexec
CREATE DATABASE platform OWNER platform;
CREATE DATABASE litellm OWNER litellm;
REVOKE CONNECT ON DATABASE platform FROM PUBLIC;
REVOKE CONNECT ON DATABASE litellm FROM PUBLIC;
GRANT CONNECT ON DATABASE platform TO platform;
GRANT CONNECT ON DATABASE litellm TO litellm;
SQL
