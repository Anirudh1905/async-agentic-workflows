"""RUN_FINISHED handlers. Each runs in its own consumer group, so both see every event.
Both are idempotent because Kafka delivery is at-least-once."""

import logging
from dataclasses import asdict
from datetime import datetime

import asyncpg
import httpx

from agentic_workflows.workflows.models import RunFinished

log = logging.getLogger(__name__)


def persistence_handler(pool: asyncpg.Pool):
    async def handle(event: RunFinished) -> None:
        # Upsert on run_id: a replayed event rewrites the same row.
        await pool.execute(
            """
            INSERT INTO runs (run_id, user_id, status, prompt, answer, error, started_at, finished_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            ON CONFLICT (run_id) DO UPDATE SET
                user_id = EXCLUDED.user_id, status = EXCLUDED.status, prompt = EXCLUDED.prompt,
                answer = EXCLUDED.answer, error = EXCLUDED.error,
                started_at = EXCLUDED.started_at, finished_at = EXCLUDED.finished_at,
                recorded_at = now()
            """,
            event.run_id,
            event.user_id,
            event.status,
            event.prompt,
            event.answer,
            event.error,
            datetime.fromisoformat(event.started_at),
            datetime.fromisoformat(event.finished_at),
        )
        log.info("recorded run %s (%s)", event.run_id, event.status)

    return handle


def notification_handler(pool: asyncpg.Pool, webhook_url: str | None, http: httpx.AsyncClient):
    async def handle(event: RunFinished) -> None:
        # Claim the event first so a replay doesn't notify twice. If sending fails,
        # release the claim so the retry (or a replay) can send it.
        claimed = await pool.fetchval(
            "INSERT INTO notifications_sent (event_id) VALUES ($1) ON CONFLICT DO NOTHING RETURNING event_id",
            event.event_id,
        )
        if not claimed:
            log.info("run %s: notification already sent, skipping", event.run_id)
            return
        try:
            if webhook_url:
                r = await http.post(webhook_url, json=asdict(event), timeout=10)
                r.raise_for_status()
            log.info("NOTIFY run %s %s: %s", event.run_id, event.status, (event.answer or event.error or "")[:120])
        except Exception:
            await pool.execute("DELETE FROM notifications_sent WHERE event_id = $1", event.event_id)
            raise

    return handle
