#!/bin/sh
# Runs once, on first boot of an empty volume. Temporal creates its own
# databases (temporal, temporal_visibility); this adds the app's database.
set -e
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" -c "CREATE DATABASE \"$APP_DB\";"
