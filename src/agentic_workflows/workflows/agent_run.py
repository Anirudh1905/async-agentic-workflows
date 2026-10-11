from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError

with workflow.unsafe.imports_passed_through():
    from langchain_core.messages import HumanMessage
    from langgraph.errors import GraphRecursionError
    from temporalio.contrib.langgraph import graph

    from agentic_workflows.agent.graph import AGENT_GRAPH, recursion_limit
    from agentic_workflows.events.producer import EventActivities
    from agentic_workflows.workflows.models import RunFinished, RunInput, RunResult


@workflow.defn
class AgentRunWorkflow:
    """One agent run. The LangGraph loop runs here, in workflow code; each `model`
    and `tools` node execution is its own activity (Temporal's LangGraph plugin), so
    every LLM call and tool call is recorded in the history, and a crash resumes
    from the last finished step instead of restarting the agent. Per-step timeouts
    and retries live on the nodes in agent/graph.py."""

    def __init__(self) -> None:
        self._status = "queued"

    @workflow.run
    async def run(self, input: RunInput) -> RunResult:
        started_at = workflow.now().isoformat()
        # workflow.logger is replay-safe (silent while replaying) and tags lines with the run.
        workflow.logger.info("agent run started run_id=%s user_id=%s max_steps=%d", input.run_id, input.user_id, input.max_steps)
        self._status = "running_agent"
        try:
            result = await graph(AGENT_GRAPH).compile().ainvoke(
                {"messages": [HumanMessage(input.prompt)]},
                config={"recursion_limit": recursion_limit(input.max_steps)},
            )
            answer = result["messages"][-1].content
        except (ActivityError, GraphRecursionError) as e:
            # A step ran out of retries, or the agent hit its step cap: still tell
            # downstream the run finished (as failed), then fail the workflow.
            if isinstance(e, ActivityError):
                error = str(e.cause or e)
            else:
                error = f"agent exceeded {input.max_steps} steps"
            workflow.logger.warning("agent run failed run_id=%s: %s", input.run_id, error)
            await self._publish(input, started_at, status="failed", answer=None, error=error)
            self._status = "failed"
            raise ApplicationError(f"agent failed: {error}", non_retryable=True) from e

        workflow.logger.info("agent run completed run_id=%s steps=%d answer_chars=%d", input.run_id, len(result["messages"]) - 1, len(answer))
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
            summary=f"RUN_FINISHED · {status}",
        )

    @workflow.query
    def status(self) -> str:
        return self._status
