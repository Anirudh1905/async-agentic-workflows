"""Run endpoints: start AgentRunWorkflow and report its status. Never waits for the agent."""

import uuid
from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from temporalio.client import Client, WorkflowExecutionStatus, WorkflowFailureError
from temporalio.service import RPCError, RPCStatusCode

from agentic_workflows.api.deps import get_task_queue, get_temporal_client
from agentic_workflows.api.schemas import CreateRun, RunCreated, RunStatus
from agentic_workflows.workflows.agent_run import AgentRunWorkflow
from agentic_workflows.workflows.models import RunInput

router = APIRouter(prefix="/runs", tags=["runs"])

TemporalClient = Annotated[Client, Depends(get_temporal_client)]
TaskQueue = Annotated[str, Depends(get_task_queue)]


@router.post("", status_code=202, response_model=RunCreated)
async def create_run(body: CreateRun, client: TemporalClient, task_queue: TaskQueue):
    run_id = str(uuid.uuid4())
    await client.start_workflow(
        AgentRunWorkflow.run,
        RunInput(run_id=run_id, prompt=body.prompt),
        id=run_id,
        task_queue=task_queue,
    )
    return RunCreated(run_id=run_id)


@router.get("/{run_id}", response_model=RunStatus)
async def get_run(run_id: str, client: TemporalClient):
    handle = client.get_workflow_handle_for(AgentRunWorkflow.run, run_id)
    try:
        desc = await handle.describe()
    except RPCError as e:
        if e.status == RPCStatusCode.NOT_FOUND:
            raise HTTPException(404, "run not found")
        raise

    out = RunStatus(run_id=run_id, status=desc.status.name.lower())
    if desc.status == WorkflowExecutionStatus.RUNNING:
        # Queries are answered by a live worker; if none is polling, the run is
        # still safe in Temporal, we just can't report the finer-grained step.
        try:
            out.step = await handle.query(AgentRunWorkflow.status, rpc_timeout=timedelta(seconds=2))
        except RPCError:
            out.step = None
    elif desc.status == WorkflowExecutionStatus.COMPLETED:
        out.answer = (await handle.result()).answer
    else:
        try:
            await handle.result()
        except WorkflowFailureError as e:
            out.error = str(e.cause or e)
    return out
