from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError

with workflow.unsafe.imports_passed_through():
    from agentic_workflows.events.producer import EventActivities
    from agentic_workflows.workflows.activities import run_agent_activity
    from agentic_workflows.workflows.models import AgentInput, RunFinished, RunInput, RunResult


@workflow.defn
class AgentRunWorkflow:
    """One agent run. Workflow code must be deterministic: all real work
    happens in activities, the workflow only orchestrates and tracks status."""

    def __init__(self) -> None:
        self._status = "queued"

    @workflow.run
    async def run(self, input: RunInput) -> RunResult:
        started_at = workflow.now().isoformat()
        self._status = "running_agent"
        try:
            answer = await workflow.execute_activity(
                run_agent_activity,
                AgentInput(prompt=input.prompt),
                # Long enough for deep_analysis (5-7 min) plus the model calls around it.
                start_to_close_timeout=timedelta(minutes=15),
                retry_policy=RetryPolicy(
                    initial_interval=timedelta(seconds=2),
                    backoff_coefficient=2.0,
                    maximum_attempts=3,
                ),
            )
        except ActivityError as e:
            # Out of retries: still tell downstream the run finished (as failed), then fail the workflow.
            error = str(e.cause or e)
            await self._publish(input, started_at, status="failed", answer=None, error=error)
            self._status = "failed"
            raise ApplicationError(f"agent failed: {error}", non_retryable=True) from e

        await self._publish(input, started_at, status="completed", answer=answer, error=None)
        self._status = "completed"
        return RunResult(run_id=input.run_id, answer=answer)

    async def _publish(self, input: RunInput, started_at: str, *, status: str, answer, error) -> None:
        self._status = "publishing"
        event = RunFinished(
            event_id=f"{input.run_id}:RUN_FINISHED",
            run_id=input.run_id,
            status=status,
            prompt=input.prompt,
            answer=answer,
            error=error,
            started_at=started_at,
            finished_at=workflow.now().isoformat(),
            user_id=input.user_id,
        )
        # No maximum_attempts: if Kafka is down the run waits for it rather than
        # finishing without anyone downstream hearing about it.
        await workflow.execute_activity_method(
            EventActivities.publish_run_finished,
            event,
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(maximum_interval=timedelta(seconds=30)),
        )

    @workflow.query
    def status(self) -> str:
        return self._status
