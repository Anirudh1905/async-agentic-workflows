"""Shared consume loop: parse → handle with retries → DLQ on give-up → commit.

At-least-once: the offset is committed only after the handler succeeds or the
message is parked in the DLQ, so a crash replays it. Handlers must be idempotent.
"""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
from pydantic import TypeAdapter

from agentic_workflows.workflows.models import RunFinished

log = logging.getLogger(__name__)

Handler = Callable[[RunFinished], Awaitable[None]]

MAX_ATTEMPTS = 3
_parse = TypeAdapter(RunFinished).validate_python


async def process(raw: bytes, handler: Handler, *, backoff_seconds: float = 1.0) -> str | None:
    """Run the handler on one message. Returns None on success, or the error to
    send to the DLQ. Bad payloads go straight to the DLQ; handler errors are retried."""
    try:
        event = _parse(json.loads(raw))
    except Exception as e:
        return f"unparseable event: {e}"

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            await handler(event)
            return None
        except Exception as e:
            log.warning("run %s: attempt %d/%d failed: %s", event.run_id, attempt, MAX_ATTEMPTS, e)
            if attempt == MAX_ATTEMPTS:
                return f"{type(e).__name__}: {e}"
            await asyncio.sleep(backoff_seconds * 2 ** (attempt - 1))


async def run_consumer(
    *, name: str, group_id: str, topic: str, dlq_topic: str, bootstrap_servers: str, handler: Handler
) -> None:
    consumer = AIOKafkaConsumer(
        topic,
        bootstrap_servers=bootstrap_servers,
        group_id=group_id,
        enable_auto_commit=False,
        auto_offset_reset="earliest",
    )
    dlq = AIOKafkaProducer(bootstrap_servers=bootstrap_servers, acks="all")
    await consumer.start()
    await dlq.start()
    log.info("%s consuming %r as group %r", name, topic, group_id)
    try:
        async for msg in consumer:
            error = await process(msg.value, handler)
            if error:
                log.error("%s: parking %s[%d]@%d in %s: %s", name, msg.topic, msg.partition, msg.offset, dlq_topic, error)
                await dlq.send_and_wait(
                    dlq_topic,
                    key=msg.key,
                    value=msg.value,
                    headers=[
                        ("error", error.encode()[:1000]),
                        ("consumer", name.encode()),
                        ("source", f"{msg.topic}[{msg.partition}]@{msg.offset}".encode()),
                    ],
                )
            await consumer.commit()
    finally:
        await consumer.stop()
        await dlq.stop()
