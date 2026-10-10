#!/bin/sh
# One-shot (temporal-schema service): create or migrate Temporal's Postgres schemas.
# Safe to re-run: create-database tolerates an existing db, setup-schema -v 0.0 only
# creates the version table if missing, update-schema applies pending migrations.
set -eu
SCHEMA_DIR=/etc/temporal/schema/postgresql/v12
sql() { temporal-sql-tool --plugin postgres12 --ep postgres -p 5432 -u "$POSTGRES_USER" --pw "$POSTGRES_PWD" "$@"; }

for db in temporal temporal_visibility; do
  case $db in temporal) dir=$SCHEMA_DIR/temporal/versioned ;; *) dir=$SCHEMA_DIR/visibility/versioned ;; esac
  sql --db "$db" create-database 2>&1 | grep -v "already exists" || true
  sql --db "$db" setup-schema -v 0.0
  sql --db "$db" update-schema -d "$dir"
done
echo "temporal schemas ready"
