"""Run endpoints: start AgentRunWorkflow and report its status. Never waits for the agent.
All routes require a logged-in user, who only ever sees their own runs."""

import logging
import uuid
from datetime import timedelta
from typing import Annotated

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query
from temporalio.client import Client, WorkflowExecutionStatus, WorkflowFailureError
from temporalio.service import RPCError, RPCStatusCode

from agentic_workflows.api.auth import CurrentUser
from agentic_workflows.api.deps import get_db, get_task_queue, get_temporal_client
from agentic_workflows.api.schemas import CreateRun, RunCreated, RunStatus, RunSummary
from agentic_workflows.config import get_settings
from agentic_workflows.workflows.agent_run import AgentRunWorkflow
from agentic_workflows.workflows.models import RunInput

router = APIRouter(prefix="/runs", tags=["runs"])
log = logging.getLogger(__name__)

TemporalClient = Annotated[Client, Depends(get_temporal_client)]
TaskQueue = Annotated[str, Depends(get_task_queue)]
Db = Annotated[asyncpg.Pool, Depends(get_db)]

_not_found = HTTPException(404, "run not found")


@router.post("", status_code=202, response_model=RunCreated)
async def create_run(body: CreateRun, user: CurrentUser, client: TemporalClient, task_queue: TaskQueue):
    run_id = str(uuid.uuid4())
    await client.start_workflow(
        AgentRunWorkflow.run,
        RunInput(run_id=run_id, prompt=body.prompt, user_id=user.id, max_steps=get_settings().agent_max_steps),
        id=run_id,
        task_queue=task_queue,
        # The owner travels with the execution, so GET /runs/{id} can check it from describe().
        memo={"user_id": user.id},
    )
    log.info("run started run_id=%s user_id=%s prompt_chars=%d", run_id, user.id, len(body.prompt))
    return RunCreated(run_id=run_id)


@router.get("", response_model=list[RunSummary])
async def list_runs(user: CurrentUser, db: Db, limit: Annotated[int, Query(ge=1, le=100)] = 20):
    """The user's finished runs, newest first. In-flight runs appear once RUN_FINISHED is recorded."""
    rows = await db.fetch(
        """
        SELECT run_id, status, prompt, answer, error, started_at, finished_at
        FROM runs WHERE user_id = $1 ORDER BY finished_at DESC LIMIT $2
        """,
        user.id,
        limit,
    )
    return [RunSummary(**dict(r)) for r in rows]


@router.get("/{run_id}", response_model=RunStatus)
async def get_run(run_id: str, user: CurrentUser, client: TemporalClient):
    handle = client.get_workflow_handle_for(AgentRunWorkflow.run, run_id)
    try:
        desc = await handle.describe()
    except RPCError as e:
        if e.status == RPCStatusCode.NOT_FOUND:
            raise _not_found
        raise
    # Someone else's run looks exactly like a missing one, so run ids can't be probed.
    if (await desc.memo_value("user_id", default=None)) != user.id:
        log.info("run hidden from non-owner run_id=%s user_id=%s", run_id, user.id)
        raise _not_found

    out = RunStatus(run_id=run_id, status=desc.status.name.lower())
    if desc.status == WorkflowExecutionStatus.RUNNING:
        # Queries are answered by a live worker; if none is polling, the run is
        # still safe in Temporal, we just can't report the finer-grained step.
        try:
            out.step = await handle.query(AgentRunWorkflow.status, rpc_timeout=timedelta(seconds=2))
        except RPCError as e:
            log.debug("status query unavailable run_id=%s: %s", run_id, e)
            out.step = None
    elif desc.status == WorkflowExecutionStatus.COMPLETED:
        out.answer = (await handle.result()).answer
    else:
        try:
            await handle.result()
        except WorkflowFailureError as e:
            out.error = str(e.cause or e)
    return out
