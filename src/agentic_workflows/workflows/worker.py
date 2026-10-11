import asyncio
import logging

from temporalio.client import Client
from temporalio.worker import Worker

from agentic_workflows.config import get_settings
from agentic_workflows.events.producer import EventActivities, make_producer
from agentic_workflows.workflows.activities import run_agent_activity
from agentic_workflows.workflows.agent_run import AgentRunWorkflow


async def run_worker() -> None:
    s = get_settings()
    client = await Client.connect(s.temporal_address, namespace=s.temporal_namespace)
    producer = make_producer(s.kafka_bootstrap_servers)
    await producer.start()
    events = EventActivities(producer, s.run_finished_topic)
    try:
        worker = Worker(
            client,
            task_queue=s.temporal_task_queue,
            workflows=[AgentRunWorkflow],
            activities=[run_agent_activity, events.publish_run_finished],
        )
        logging.info("worker polling task queue %r on %s", s.temporal_task_queue, s.temporal_address)
        await worker.run()
    finally:
        await producer.stop()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
