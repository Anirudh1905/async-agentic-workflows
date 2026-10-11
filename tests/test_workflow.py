"""AgentRunWorkflow and the run API against Temporal's time-skipping test server.
The real activities are swapped for fakes with the same names, so no LLM, Kafka or network."""

import asyncio
import uuid
from contextlib import asynccontextmanager

import httpx
import pytest
from temporalio import activity
from temporalio.client import WorkflowFailureError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from agentic_workflows.api.auth import get_current_user
from agentic_workflows.api.deps import get_task_queue, get_temporal_client
from agentic_workflows.api.schemas import User
from agentic_workflows.api.main import app
from agentic_workflows.workflows.agent_run import AgentRunWorkflow
from agentic_workflows.workflows.models import AgentInput, RunFinished, RunInput

TASK_QUEUE = "test-agent-runs"


ALICE = User(id="user-alice", email="alice@example.com")
BOB = User(id="user-bob", email="bob@example.com")


@asynccontextmanager
async def api_client(env, user: User = ALICE):
    """HTTP client for the real app, wired to the test server instead of a live Temporal,
    and logged in as `user` (auth itself is covered in test_auth.py).
    httpx's ASGI transport doesn't run lifespan, so the app never connects on its own."""
    app.dependency_overrides[get_temporal_client] = lambda: env.client
    app.dependency_overrides[get_task_queue] = lambda: TASK_QUEUE
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            yield http
    finally:
        app.dependency_overrides.clear()


def fake_activity(behaviour):
    @activity.defn(name="run_agent_activity")
    async def run_agent_activity(input: AgentInput) -> str:
        return await behaviour(input)

    return run_agent_activity


def fake_publisher(published: list[RunFinished]):
    @activity.defn(name="publish_run_finished")
    async def publish_run_finished(event: RunFinished) -> None:
        published.append(event)

    return publish_run_finished


async def run_with(behaviour, coro_fn, published: list[RunFinished] | None = None):
    published = published if published is not None else []
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue=TASK_QUEUE,
            workflows=[AgentRunWorkflow],
            activities=[fake_activity(behaviour), fake_publisher(published)],
        ):
            return await coro_fn(env)


def test_workflow_returns_agent_answer():
    async def echo(input):
        return f"answer to: {input.prompt}"

    async def go(env):
        return await env.client.execute_workflow(
            AgentRunWorkflow.run, RunInput(run_id="r1", prompt="hi"), id="r1", task_queue=TASK_QUEUE
        )

    published = []
    result = asyncio.run(run_with(echo, go, published))
    assert result.run_id == "r1"
    assert result.answer == "answer to: hi"

    [event] = published
    assert (event.event_id, event.run_id, event.status) == ("r1:RUN_FINISHED", "r1", "completed")
    assert (event.prompt, event.answer, event.error) == ("hi", "answer to: hi", None)
    assert event.started_at <= event.finished_at


def test_workflow_retries_failed_activity():
    attempts = []

    async def flaky(input):
        attempts.append(activity.info().attempt)
        if len(attempts) < 3:
            raise RuntimeError("LLM timeout")
        return "ok after retries"

    async def go(env):
        return await env.client.execute_workflow(
            AgentRunWorkflow.run, RunInput(run_id="r2", prompt="x"), id="r2", task_queue=TASK_QUEUE
        )

    assert asyncio.run(run_with(flaky, go)).answer == "ok after retries"
    assert attempts == [1, 2, 3]


def test_workflow_fails_after_max_attempts():
    async def broken(input):
        raise RuntimeError("always down")

    async def go(env):
        with pytest.raises(WorkflowFailureError):
            await env.client.execute_workflow(
                AgentRunWorkflow.run, RunInput(run_id="r3", prompt="x"), id="r3", task_queue=TASK_QUEUE
            )

    published = []
    asyncio.run(run_with(broken, go, published))

    # A failed run still emits RUN_FINISHED so downstream records it.
    [event] = published
    assert (event.status, event.answer) == ("failed", None)
    assert "always down" in event.error


def test_api_starts_run_and_reports_result():
    async def echo(input):
        return f"answer to: {input.prompt}"

    async def go(env):
        async with api_client(env) as http:
            r = await http.post("/runs", json={"prompt": "hello"})
            assert r.status_code == 202
            run_id = r.json()["run_id"]

            await env.client.get_workflow_handle(run_id).result()
            r = await http.get(f"/runs/{run_id}")
            assert r.json() == {
                "run_id": run_id,
                "status": "completed",
                "step": None,
                "answer": "answer to: hello",
                "error": None,
            }

            assert (await http.get(f"/runs/{uuid.uuid4()}")).status_code == 404
            assert (await http.post("/runs", json={"prompt": ""})).status_code == 422

        # Another user can't see it: same 404 as a run that doesn't exist.
        async with api_client(env, user=BOB) as http:
            assert (await http.get(f"/runs/{run_id}")).status_code == 404

    published = []
    asyncio.run(run_with(echo, go, published))
    # The starting user is carried into the workflow and out on RUN_FINISHED.
    assert [e.user_id for e in published] == [ALICE.id]


def test_api_reports_running_when_no_worker_is_polling():
    async def go():
        async with await WorkflowEnvironment.start_time_skipping() as env:
            async with api_client(env) as http:
                run_id = (await http.post("/runs", json={"prompt": "hi"})).json()["run_id"]
                r = await http.get(f"/runs/{run_id}")
                assert r.status_code == 200
                assert r.json()["status"] == "running"
                assert r.json()["step"] is None

    asyncio.run(go())


def test_publish_is_retried_until_kafka_accepts():
    published, calls = [], []

    @activity.defn(name="publish_run_finished")
    async def flaky_publish(event: RunFinished) -> None:
        calls.append(1)
        if len(calls) < 4:
            raise RuntimeError("kafka unavailable")
        published.append(event)

    async def echo(input):
        return "ok"

    async def go():
        async with await WorkflowEnvironment.start_time_skipping() as env:
            async with Worker(
                env.client,
                task_queue=TASK_QUEUE,
                workflows=[AgentRunWorkflow],
                activities=[fake_activity(echo), flaky_publish],
            ):
                return await env.client.execute_workflow(
                    AgentRunWorkflow.run, RunInput(run_id="r9", prompt="x"), id="r9", task_queue=TASK_QUEUE
                )

    assert asyncio.run(go()).answer == "ok"
    assert len(calls) == 4 and len(published) == 1
