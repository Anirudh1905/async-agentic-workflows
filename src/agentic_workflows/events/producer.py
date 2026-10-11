import json
from dataclasses import asdict

from aiokafka import AIOKafkaProducer
from temporalio import activity

from agentic_workflows.workflows.models import RunFinished


def make_producer(bootstrap_servers: str) -> AIOKafkaProducer:
    # acks=all + idempotence: the broker drops duplicates from this producer's own
    # internal retries. Duplicates from Temporal retrying the activity can still
    # happen, which is why consumers dedupe on event_id / run_id.
    return AIOKafkaProducer(bootstrap_servers=bootstrap_servers, acks="all", enable_idempotence=True)


def encode(event: RunFinished) -> bytes:
    return json.dumps(asdict(event)).encode()


class EventActivities:
    """Activities that need the worker's long-lived Kafka producer."""

    def __init__(self, producer: AIOKafkaProducer, topic: str) -> None:
        self._producer = producer
        self._topic = topic

    @activity.defn
    async def publish_run_finished(self, event: RunFinished) -> None:
        # Key by run_id so every event for a run lands on the same partition, in order.
        await self._producer.send_and_wait(
            self._topic,
            key=event.run_id.encode(),
            value=encode(event),
        )
