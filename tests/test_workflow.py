"""AgentRunWorkflow and the run API against Temporal's time-skipping test server.

The real LangGraph graph runs through Temporal's LangGraph plugin, so each model and
tool step is its own activity, exactly as in production. The LLM is a scripted fake,
web search is stubbed and the Kafka publish is a fake activity: no network."""

import asyncio
import uuid
from contextlib import asynccontextmanager

import httpx
import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from temporalio import activity
from temporalio.client import WorkflowFailureError
from temporalio.contrib.langgraph import LangGraphPlugin
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from agentic_workflows.agent import graph as agent_graph
from agentic_workflows.agent import tools
from agentic_workflows.api.auth import get_current_user
from agentic_workflows.api.deps import get_task_queue, get_temporal_client
from agentic_workflows.api.main import app
from agentic_workflows.api.schemas import User
from agentic_workflows.workflows.agent_run import AgentRunWorkflow
from agentic_workflows.workflows.models import RunFinished, RunInput

TASK_QUEUE = "test-agent-runs"

ALICE = User(id="user-alice", email="alice@example.com")
BOB = User(id="user-bob", email="bob@example.com")


class FakeToolModel(FakeMessagesListChatModel):
    """Replays scripted responses; bind_tools is a no-op so the graph can wire it up."""

    def bind_tools(self, tools, **kwargs):
        return self


class FlakyModel(FakeToolModel):
    """Raises on the calls numbered in `fail_on` (1-based), otherwise replays its responses."""

    fail_on: set[int] = set()
    calls: int = 0

    async def ainvoke(self, *args, **kwargs):
        self.calls += 1
        if self.calls in self.fail_on:
            raise RuntimeError("LLM timeout")
        return await super().ainvoke(*args, **kwargs)


def tool_call(query: str, id: str = "call_1") -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": "web_search", "args": {"query": query}, "id": id}])


class FakeDDGS:
    def __call__(self):
        return self

    def text(self, query, max_results):
        return [{"title": f"about {query}", "href": "https://example.com", "body": "snippet"}]


@pytest.fixture(autouse=True)
def stub_search(monkeypatch):
    monkeypatch.setattr(tools, "DDGS", FakeDDGS())
    yield
    agent_graph.set_llm(None)


def fake_publisher(published: list[RunFinished]):
    @activity.defn(name="publish_run_finished")
    async def publish_run_finished(event: RunFinished) -> None:
        published.append(event)

    return publish_run_finished


@asynccontextmanager
async def temporal(publisher=None):
    """Test server + a worker with the real workflow and graph (via the plugin)."""
    async with await WorkflowEnvironment.start_time_skipping(data_converter=pydantic_data_converter) as env:
        async with Worker(
            env.client,
            task_queue=TASK_QUEUE,
            workflows=[AgentRunWorkflow],
            activities=[publisher or fake_publisher([])],
            plugins=[LangGraphPlugin(graphs={agent_graph.AGENT_GRAPH: agent_graph.build_state_graph()})],
        ):
            yield env


async def run(env, prompt="hi", run_id="r1", max_steps=10):
    return await env.client.execute_workflow(
        AgentRunWorkflow.run,
        RunInput(run_id=run_id, prompt=prompt, max_steps=max_steps),
        id=run_id,
        task_queue=TASK_QUEUE,
    )


async def scheduled_activities(env, run_id):
    """(activity type, summary) for every activity the run scheduled, in order."""
    out = []
    async for e in env.client.get_workflow_handle(run_id).fetch_history_events():
        if e.HasField("activity_task_scheduled_event_attributes"):
            summary = await env.client.data_converter.decode([e.user_metadata.summary]) if e.user_metadata.HasField("summary") else [None]
            out.append((e.activity_task_scheduled_event_attributes.activity_type.name, summary[0]))
    return out


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


def test_each_llm_and_tool_call_is_its_own_activity():
    agent_graph.set_llm(FakeToolModel(responses=[tool_call("capital of France"), AIMessage(content="Paris.")]))
    published = []

    async def go():
        async with temporal(fake_publisher(published)) as env:
            result = await run(env, prompt="capital of France?")
            return result, await scheduled_activities(env, "r1")

    result, history = asyncio.run(go())
    assert result.answer == "Paris."
    # One row per step, with a readable summary in the Temporal UI.
    assert history == [
        ("agent.model", "LLM call · 1 messages in context"),
        ("agent.tools", "web_search('capital of France')"),
        ("agent.model", "LLM call · 3 messages in context"),
        ("publish_run_finished", "RUN_FINISHED · completed"),
    ]

    [event] = published
    assert (event.event_id, event.run_id, event.status) == ("r1:RUN_FINISHED", "r1", "completed")
    assert (event.prompt, event.answer, event.error) == ("capital of France?", "Paris.", None)
    assert event.started_at <= event.finished_at


def test_failed_step_is_retried_on_its_own():
    # The first LLM call and the tool call succeed; the second LLM call fails twice.
    # Only that step is retried: the tool and the first LLM call already have their
    # results recorded in the history and don't run again.
    llm = FlakyModel(responses=[tool_call("x"), AIMessage(content="ok after retries")], fail_on={2, 3})
    agent_graph.set_llm(llm)

    async def go():
        async with temporal() as env:
            return await run(env), await scheduled_activities(env, "r1")

    result, history = asyncio.run(go())
    assert result.answer == "ok after retries"
    assert llm.calls == 4  # 1 good + 2 failed attempts + 1 good retry
    # Retries happen inside one scheduled activity; the history still shows 3 agent steps.
    assert [a for a, _ in history] == ["agent.model", "agent.tools", "agent.model", "publish_run_finished"]


def test_workflow_fails_after_max_attempts():
    agent_graph.set_llm(FlakyModel(responses=[AIMessage(content="never")], fail_on=set(range(1, 100))))
    published = []

    async def go():
        async with temporal(fake_publisher(published)) as env:
            with pytest.raises(WorkflowFailureError):
                await run(env, run_id="r3")

    asyncio.run(go())
    # A failed run still emits RUN_FINISHED so downstream records it.
    [event] = published
    assert (event.status, event.answer) == ("failed", None)
    assert "LLM timeout" in event.error


def test_agent_stops_at_max_steps():
    # A model that only ever asks for another search.
    agent_graph.set_llm(FakeToolModel(responses=[tool_call(f"q{i}", id=f"c{i}") for i in range(20)]))
    published = []

    async def go():
        async with temporal(fake_publisher(published)) as env:
            with pytest.raises(WorkflowFailureError):
                await run(env, run_id="r4", max_steps=2)

    asyncio.run(go())
    [event] = published
    assert (event.status, event.error) == ("failed", "agent exceeded 2 steps")


def test_api_starts_run_and_reports_result():
    agent_graph.set_llm(FakeToolModel(responses=[AIMessage(content="answer to: hello")]))
    published = []

    async def go():
        async with temporal(fake_publisher(published)) as env:
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

    asyncio.run(go())
    # The starting user is carried into the workflow and out on RUN_FINISHED.
    assert [e.user_id for e in published] == [ALICE.id]


def test_api_reports_running_when_no_worker_is_polling():
    async def go():
        async with await WorkflowEnvironment.start_time_skipping(data_converter=pydantic_data_converter) as env:
            async with api_client(env) as http:
                run_id = (await http.post("/runs", json={"prompt": "hi"})).json()["run_id"]
                r = await http.get(f"/runs/{run_id}")
                assert r.status_code == 200
                assert r.json()["status"] == "running"
                assert r.json()["step"] is None

    asyncio.run(go())


def test_publish_is_retried_until_kafka_accepts():
    agent_graph.set_llm(FakeToolModel(responses=[AIMessage(content="ok")]))
    published, calls = [], []

    @activity.defn(name="publish_run_finished")
    async def flaky_publish(event: RunFinished) -> None:
        calls.append(1)
        if len(calls) < 4:
            raise RuntimeError("kafka unavailable")
        published.append(event)

    async def go():
        async with temporal(flaky_publish) as env:
            return await run(env, run_id="r9")

    assert asyncio.run(go()).answer == "ok"
    assert len(calls) == 4 and len(published) == 1
