"""Consumers: retry/DLQ logic as unit tests, then handlers and a full Kafka round trip
against the compose stack. Integration tests skip when Postgres/Kafka aren't up (`make up`)."""

import asyncio
import json
import socket
import uuid
from dataclasses import asdict

import httpx
import pytest
from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
from aiokafka.admin import AIOKafkaAdminClient, NewTopic

from agentic_workflows import db
from agentic_workflows.config import get_settings
from agentic_workflows.consumers.handlers import notification_handler, persistence_handler
from agentic_workflows.consumers.runner import process, run_consumer
from agentic_workflows.events.producer import encode
from agentic_workflows.workflows.models import RunFinished


def make_event(**overrides) -> RunFinished:
    run_id = overrides.pop("run_id", f"test-{uuid.uuid4()}")
    fields = {
        "event_id": f"{run_id}:RUN_FINISHED",
        "run_id": run_id,
        "status": "completed",
        "prompt": "what is 2+2?",
        "answer": "4",
        "error": None,
        "started_at": "2026-10-10T10:00:00+00:00",
        "finished_at": "2026-10-10T10:00:05+00:00",
    }
    return RunFinished(**(fields | overrides))


# --- runner.process: no infrastructure needed -------------------------------------


def test_process_succeeds_after_transient_failures():
    calls = []

    async def handler(event):
        calls.append(event.run_id)
        if len(calls) < 3:
            raise RuntimeError("db blip")

    assert asyncio.run(process(encode(make_event()), handler, backoff_seconds=0)) is None
    assert len(calls) == 3


def test_process_gives_up_after_max_attempts():
    async def handler(event):
        raise RuntimeError("db down")

    assert asyncio.run(process(encode(make_event()), handler, backoff_seconds=0)) == "RuntimeError: db down"


def test_process_sends_bad_payload_straight_to_dlq():
    calls = []

    async def handler(event):
        calls.append(event)

    error = asyncio.run(process(b'{"not": "an event"}', handler, backoff_seconds=0))
    assert error.startswith("unparseable event")
    assert calls == []


# --- integration: compose Postgres + Kafka -----------------------------------------


def _reachable(host: str, port: int) -> bool:
    try:
        socket.create_connection((host, port), timeout=1).close()
        return True
    except OSError:
        return False


s = get_settings()
pg_host, pg_port = "127.0.0.1", int(s.database_url.rsplit(":", 1)[1].split("/")[0])
kafka_host, kafka_port = s.kafka_bootstrap_servers.split(":")
needs_stack = pytest.mark.skipif(
    not (_reachable(pg_host, pg_port) and _reachable(kafka_host, int(kafka_port))),
    reason="compose stack not running (make up)",
)


@needs_stack
def test_persistence_handler_is_idempotent():
    async def go():
        pool = await db.connect(s.database_url)
        try:
            handle = persistence_handler(pool)
            event = make_event()
            await handle(event)
            await handle(event)  # replay
            rows = await pool.fetch("SELECT status, answer FROM runs WHERE run_id = $1", event.run_id)
            assert [tuple(r) for r in rows] == [("completed", "4")]
            await pool.execute("DELETE FROM runs WHERE run_id = $1", event.run_id)
        finally:
            await pool.close()

    asyncio.run(go())


@needs_stack
def test_notification_handler_sends_once_and_releases_claim_on_failure():
    sent = []
    fail = {"next": True}

    def webhook(request: httpx.Request) -> httpx.Response:
        if fail["next"]:
            fail["next"] = False
            return httpx.Response(503)
        sent.append(json.loads(request.content)["run_id"])
        return httpx.Response(200)

    async def go():
        pool = await db.connect(s.database_url)
        try:
            async with httpx.AsyncClient(transport=httpx.MockTransport(webhook)) as http:
                handle = notification_handler(pool, "http://hooks.test/notify", http)
                event = make_event()
                with pytest.raises(httpx.HTTPStatusError):
                    await handle(event)  # webhook 503 -> claim released
                await handle(event)  # retry sends
                await handle(event)  # replay is skipped
                assert sent == [event.run_id]
                await pool.execute("DELETE FROM notifications_sent WHERE event_id = $1", event.event_id)
        finally:
            await pool.close()

    asyncio.run(go())


@needs_stack
def test_kafka_round_trip_with_dlq():
    """Real topics: a good event reaches the handler, a poison one lands in the DLQ."""
    suffix = uuid.uuid4().hex[:8]
    topic, dlq_topic = f"test.RUN_FINISHED.{suffix}", f"test.RUN_FINISHED.DLQ.{suffix}"
    handled = []

    async def handler(event):
        if event.prompt == "poison":
            raise RuntimeError("cannot handle")
        handled.append(event.run_id)

    async def go():
        admin = AIOKafkaAdminClient(bootstrap_servers=s.kafka_bootstrap_servers)
        await admin.start()
        await admin.create_topics([NewTopic(t, 1, 1) for t in (topic, dlq_topic)])
        producer = AIOKafkaProducer(bootstrap_servers=s.kafka_bootstrap_servers)
        await producer.start()
        good, bad = make_event(), make_event(prompt="poison")
        for e in (good, bad):
            await producer.send_and_wait(topic, key=e.run_id.encode(), value=encode(e))
        await producer.stop()

        consumer_task = asyncio.create_task(
            run_consumer(
                name="test",
                group_id=f"test-{suffix}",
                topic=topic,
                dlq_topic=dlq_topic,
                bootstrap_servers=s.kafka_bootstrap_servers,
                handler=handler,
            )
        )
        dlq = AIOKafkaConsumer(dlq_topic, bootstrap_servers=s.kafka_bootstrap_servers, auto_offset_reset="earliest")
        await dlq.start()
        try:
            parked = await asyncio.wait_for(dlq.getone(), timeout=30)
        finally:
            await dlq.stop()
            consumer_task.cancel()
            await asyncio.gather(consumer_task, return_exceptions=True)
            await admin.delete_topics([topic, dlq_topic])
            await admin.close()

        assert handled == [good.run_id]
        assert json.loads(parked.value) == asdict(bad)
        headers = dict(parked.headers)
        assert headers["error"] == b"RuntimeError: cannot handle"
        assert headers["source"].startswith(topic.encode())

    asyncio.run(go())
