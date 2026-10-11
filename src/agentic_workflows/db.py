import logging

import asyncpg

log = logging.getLogger(__name__)

# Applied on API and consumer startup. Idempotent and additive only; switch to a
# migration tool the first time an existing table needs altering.
SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    email         text NOT NULL UNIQUE,  -- stored lowercased
    password_hash text NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS runs (
    run_id      text PRIMARY KEY,
    user_id     text,
    status      text NOT NULL,
    prompt      text NOT NULL,
    answer      text,
    error       text,
    started_at  timestamptz NOT NULL,
    finished_at timestamptz NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS runs_user_finished ON runs (user_id, finished_at DESC);

CREATE TABLE IF NOT EXISTS notifications_sent (
    event_id text PRIMARY KEY,
    sent_at  timestamptz NOT NULL DEFAULT now()
);
"""


async def connect(database_url: str) -> asyncpg.Pool:
    pool = await asyncpg.create_pool(database_url, min_size=1, max_size=4)
    async with pool.acquire() as conn, conn.transaction():
        # Both consumers start together; concurrent CREATE TABLE IF NOT EXISTS can still collide.
        await conn.execute("SELECT pg_advisory_xact_lock(hashtext('agentic_workflows.schema'))")
        await conn.execute(SCHEMA)
    log.info("postgres ready, schema ensured")
    return pool
