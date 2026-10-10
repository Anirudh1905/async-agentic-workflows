from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from agentic_workflows.workflows.activities import run_agent_activity
    from agentic_workflows.workflows.models import AgentInput, RunInput, RunResult


@workflow.defn
class AgentRunWorkflow:
    """One agent run. Workflow code must be deterministic: all real work
    happens in activities, the workflow only orchestrates and tracks status."""

    def __init__(self) -> None:
        self._status = "queued"

    @workflow.run
    async def run(self, input: RunInput) -> RunResult:
        self._status = "running_agent"
        answer = await workflow.execute_activity(
            run_agent_activity,
            AgentInput(prompt=input.prompt),
            start_to_close_timeout=timedelta(minutes=5),
            retry_policy=RetryPolicy(
                initial_interval=timedelta(seconds=2),
                backoff_coefficient=2.0,
                maximum_attempts=3,
            ),
        )
        self._status = "completed"
        return RunResult(run_id=input.run_id, answer=answer)

    @workflow.query
    def status(self) -> str:
        return self._status
