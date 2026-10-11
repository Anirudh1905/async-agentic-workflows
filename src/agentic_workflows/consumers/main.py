"""`uv run consumer persistence` or `uv run consumer notifications`."""

import argparse
import asyncio

import httpx

from agentic_workflows import db
from agentic_workflows.config import get_settings
from agentic_workflows.consumers.handlers import notification_handler, persistence_handler
from agentic_workflows.consumers.runner import run_consumer
from agentic_workflows.logs import setup_logging


async def run(kind: str) -> None:
    s = get_settings()
    pool = await db.connect(s.database_url)
    async with httpx.AsyncClient() as http:
        handler = (
            persistence_handler(pool)
            if kind == "persistence"
            else notification_handler(pool, s.notify_webhook_url, http)
        )
        try:
            await run_consumer(
                name=f"{kind}-consumer",
                group_id=kind,
                topic=s.run_finished_topic,
                dlq_topic=s.run_finished_dlq_topic,
                bootstrap_servers=s.kafka_bootstrap_servers,
                handler=handler,
            )
        finally:
            await pool.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a RUN_FINISHED consumer.")
    parser.add_argument("kind", choices=["persistence", "notifications"])
    args = parser.parse_args()
    setup_logging(f"consumer-{args.kind}")
    asyncio.run(run(args.kind))


if __name__ == "__main__":
    main()
