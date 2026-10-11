"""Per-user active-run cap: the Lua script against the compose Redis, with a fake
Temporal standing in for which runs are still going. Skips when Redis isn't up."""

import asyncio
import socket
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest
import redis.asyncio as redis
from fastapi import HTTPException
from temporalio.client import WorkflowExecutionStatus
from temporalio.service import RPCError, RPCStatusCode

from agentic_workflows.api.deps import get_active_runs, get_temporal_client
from agentic_workflows.api.limits.active_runs import ActiveRunLimiter
from agentic_workflows.api.main import app
from agentic_workflows.api.routes.auth import get_current_user
from agentic_workflows.api.schemas import User
from agentic_workflows.config import get_settings

REDIS_URL = get_settings().redis_url
_port = int(REDIS_URL.rsplit(":", 1)[1].split("/")[0])


def _reachable() -> bool:
    try:
        socket.create_connection(("127.0.0.1", _port), timeout=1).close()
        return True
    except OSError:
        return False


needs_redis = pytest.mark.skipif(not _reachable(), reason="compose Redis not running (make up)")


class FakeTemporal:
    """describe() answers from `status`; runs it has never heard of are NOT_FOUND."""

    def __init__(self):
        self.status: dict[str, WorkflowExecutionStatus] = {}
        self.fail_start = False

    def get_workflow_handle(self, run_id):
        async def describe():
            if run_id not in self.status:
                raise RPCError("not found", RPCStatusCode.NOT_FOUND, b"")
            return SimpleNamespace(status=self.status[run_id])

        return SimpleNamespace(describe=describe)

    async def start_workflow(self, *args, id, **kwargs):
        if self.fail_start:
            raise RuntimeError("temporal unavailable")
        self.status[id] = WorkflowExecutionStatus.RUNNING


@asynccontextmanager
async def limiter(max_active: int, **kwargs):
    client = redis.from_url(REDIS_URL)
    temporal = FakeTemporal()
    # A unique prefix per test, so slots never leak between tests or runs.
    lim = ActiveRunLimiter(client, temporal, max_active, prefix=f"test-{uuid.uuid4().hex}", **kwargs)
    try:
        yield lim, temporal
    finally:
        await client.aclose()


async def _start(lim: ActiveRunLimiter, temporal: FakeTemporal, user_id: str, run_id: str) -> int:
    """enforce() then mark the run running, as POST /runs does. Returns the status code."""
    try:
        await lim.enforce(user_id, run_id)
    except HTTPException as e:
        return e.status_code
    temporal.status[run_id] = WorkflowExecutionStatus.RUNNING
    return 202


@needs_redis
def test_cap_per_user():
    async def go():
        async with limiter(2) as (lim, t):
            alice = [await _start(lim, t, "alice", f"a{i}") for i in range(3)]
            bob = await _start(lim, t, "bob", "b0")
            return alice, bob

    alice, bob = asyncio.run(go())
    assert alice == [202, 202, 429]
    assert bob == 202  # alice being at her cap doesn't touch bob's


@needs_redis
def test_finished_run_frees_its_slot():
    async def go():
        async with limiter(1) as (lim, t):
            first = await _start(lim, t, "u", "r1")
            blocked = await _start(lim, t, "u", "r2")
            t.status["r1"] = WorkflowExecutionStatus.COMPLETED
            after = await _start(lim, t, "u", "r3")
            return first, blocked, after

    assert asyncio.run(go()) == (202, 429, 202)


@needs_redis
def test_terminated_run_frees_its_slot():
    # A terminated run never publishes RUN_FINISHED; Temporal still knows it's over.
    async def go():
        async with limiter(1) as (lim, t):
            await _start(lim, t, "u", "r1")
            t.status["r1"] = WorkflowExecutionStatus.TERMINATED
            return await _start(lim, t, "u", "r2")

    assert asyncio.run(go()) == 202


@needs_redis
def test_run_still_being_started_keeps_its_slot():
    # Admitted but not yet known to Temporal: inside the grace window it still counts.
    async def go():
        async with limiter(1) as (lim, t):
            await lim.enforce("u", "starting")
            return await _start(lim, t, "u", "r2")

    assert asyncio.run(go()) == 429


@needs_redis
def test_slot_with_no_workflow_is_dropped_after_grace():
    # Admitted, then the start failed and the release was lost too.
    async def go():
        async with limiter(1, start_grace_s=0) as (lim, t):
            await lim.enforce("u", "never-started")
            await asyncio.sleep(0.01)
            return await _start(lim, t, "u", "r2")

    assert asyncio.run(go()) == 202


def test_fails_open_when_redis_is_down():
    async def go():
        client = redis.from_url("redis://127.0.0.1:1/0", socket_connect_timeout=0.2)  # nothing listens here
        try:
            await ActiveRunLimiter(client, FakeTemporal(), 1).enforce("u", "r1")  # no raise = let through
        finally:
            await client.aclose()

    asyncio.run(go())


# --- through the API -----------------------------------------------------------------


@needs_redis
def test_post_runs_is_capped_and_a_failed_start_gives_the_slot_back():
    async def go():
        async with limiter(1) as (lim, t):
            app.dependency_overrides[get_active_runs] = lambda: lim
            app.dependency_overrides[get_temporal_client] = lambda: t
            app.dependency_overrides[get_current_user] = lambda: User(id="alice", email="a@example.com")
            try:
                transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
                async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
                    post = lambda: http.post("/runs", json={"prompt": "hi"})  # noqa: E731
                    t.fail_start = True
                    failed = await post()
                    t.fail_start = False
                    first = await post()  # the failed start didn't keep the slot
                    capped = await post()
                    return failed, first, capped
            finally:
                app.dependency_overrides.clear()

    failed, first, capped = asyncio.run(go())
    assert failed.status_code == 500
    assert first.status_code == 202
    assert capped.status_code == 429
    assert capped.json()["detail"] == "too many active runs (1/1); wait for one to finish"
