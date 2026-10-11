import asyncio
import logging

from temporalio.client import Client
from temporalio.contrib.langgraph import LangGraphPlugin
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.worker import Worker

from agentic_workflows.agent.graph import AGENT_GRAPH, build_state_graph
from agentic_workflows.config import get_settings
from agentic_workflows.logs import setup_logging
from agentic_workflows.events.producer import EventActivities, make_producer
from agentic_workflows.workflows.agent_run import AgentRunWorkflow


log = logging.getLogger(__name__)


async def run_worker() -> None:
    s = get_settings()
    # pydantic converter: LangChain messages (pydantic models) cross the activity boundary.
    client = await Client.connect(s.temporal_address, namespace=s.temporal_namespace, data_converter=pydantic_data_converter)
    producer = make_producer(s.kafka_bootstrap_servers)
    await producer.start()
    events = EventActivities(producer, s.run_finished_topic)
    try:
        worker = Worker(
            client,
            task_queue=s.temporal_task_queue,
            workflows=[AgentRunWorkflow],
            activities=[events.publish_run_finished],
            # Registers each graph node ("agent.model", "agent.tools") as an activity.
            plugins=[LangGraphPlugin(graphs={AGENT_GRAPH: build_state_graph()})],
        )
        log.info("worker polling task_queue=%s temporal=%s kafka=%s", s.temporal_task_queue, s.temporal_address, s.kafka_bootstrap_servers)
        await worker.run()
    finally:
        await producer.stop()
        log.info("worker stopped")


def main() -> None:
    setup_logging("worker")
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
