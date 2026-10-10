from temporalio import activity

from agentic_workflows.agent.graph import run_agent
from agentic_workflows.config import get_settings
from agentic_workflows.workflows.models import AgentInput


@activity.defn
async def run_agent_activity(input: AgentInput) -> str:
    """Run the LangGraph agent. Lives in an activity because LLM and web calls
    are non-deterministic; Temporal retries it on failure per the workflow's policy."""
    return await run_agent(input.prompt, max_steps=get_settings().agent_max_steps)
