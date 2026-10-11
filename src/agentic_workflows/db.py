import asyncpg

# Applied on consumer startup. Idempotent; a migration tool can replace this once
# the schema starts changing (phase 5 adds users).
SCHEMA = """
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
    return pool
