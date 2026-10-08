#!/bin/sh
set -eu

export HW_APP_PASSWORD="$(cat /run/secrets/app_db_password)"
export HW_CHECKPOINT_PASSWORD="$(cat /run/secrets/checkpoint_db_password)"
export HW_MIGRATOR_PASSWORD="$(cat /run/secrets/migrator_db_password)"

psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" --no-psqlrc --set ON_ERROR_STOP=1 <<'SQL'
\getenv app_password HW_APP_PASSWORD
\getenv checkpoint_password HW_CHECKPOINT_PASSWORD
\getenv migrator_password HW_MIGRATOR_PASSWORD
CREATE ROLE huntweave_app LOGIN PASSWORD :'app_password' NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
CREATE ROLE huntweave_checkpoint LOGIN PASSWORD :'checkpoint_password' NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
CREATE ROLE huntweave_migrator LOGIN PASSWORD :'migrator_password' NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
REVOKE ALL ON DATABASE huntweave FROM PUBLIC;
GRANT CONNECT ON DATABASE huntweave TO huntweave_app, huntweave_checkpoint, huntweave_migrator;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
CREATE SCHEMA huntweave AUTHORIZATION huntweave_migrator;
CREATE SCHEMA huntweave_checkpoint AUTHORIZATION huntweave_migrator;
GRANT USAGE ON SCHEMA huntweave TO huntweave_app;
GRANT USAGE ON SCHEMA huntweave_checkpoint TO huntweave_checkpoint;
ALTER DEFAULT PRIVILEGES FOR ROLE huntweave_migrator IN SCHEMA huntweave
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO huntweave_app;
ALTER DEFAULT PRIVILEGES FOR ROLE huntweave_migrator IN SCHEMA huntweave
  GRANT USAGE, SELECT ON SEQUENCES TO huntweave_app;
ALTER DEFAULT PRIVILEGES FOR ROLE huntweave_migrator IN SCHEMA huntweave_checkpoint
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO huntweave_checkpoint;
ALTER ROLE huntweave_app SET search_path = huntweave,pg_catalog;
ALTER ROLE huntweave_checkpoint SET search_path = huntweave_checkpoint,pg_catalog;
ALTER ROLE huntweave_migrator SET search_path = huntweave,pg_catalog;
SQL
unset HW_APP_PASSWORD HW_CHECKPOINT_PASSWORD HW_MIGRATOR_PASSWORD
