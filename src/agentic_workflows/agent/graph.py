"""A minimal tool-calling LangGraph agent: model -> (tools -> model)* -> end.

The same graph runs two ways:
- directly (CLI, Streamlit): `build_graph().ainvoke(...)`, everything in-process;
- inside AgentRunWorkflow via Temporal's LangGraph plugin, where each `model` and
  `tools` node execution is its own activity (see the node metadata below).

Node functions are module-level because the plugin registers them as activities by
import path, and they read the LLM from `get_llm()` rather than a closure for the
same reason.
"""

from datetime import timedelta

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage, convert_to_messages
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode
from temporalio.common import RetryPolicy

from agentic_workflows.agent.tools import TOOLS

SYSTEM_PROMPT = (
    "You are a helpful assistant. Use the web_search tool to look things up, "
    "and cite the links you used. "
    "Use the deep_analysis tool only when the user asks for a deep analysis. "
    "Answer concisely once you have what you need."
)

# Name the graph is registered under with the Temporal plugin; activities are
# named "<graph>.<node>", e.g. "agent.model" and "agent.tools".
AGENT_GRAPH = "agent"

_llm: BaseChatModel | None = None
_tool_node = ToolNode(TOOLS)


def default_llm() -> BaseChatModel:
    from langchain_openai import ChatOpenAI

    from agentic_workflows.config import get_settings

    s = get_settings()
    return ChatOpenAI(
        model=s.llm_model,
        api_key=s.openai_api_key,
        base_url=s.openai_base_url,
        timeout=60,
        max_retries=2,
    )


def set_llm(llm: BaseChatModel | None) -> None:
    """Swap the model the `model` node uses (tests, or a different provider)."""
    global _llm
    _llm = llm


def get_llm() -> BaseChatModel:
    global _llm
    if _llm is None:
        _llm = default_llm()
    return _llm


# Inside an activity the state's messages arrive as plain dicts (they crossed the
# Temporal boundary as JSON), so each node turns them back into message objects.
# Run directly they are already messages, and convert_to_messages is a no-op.


async def call_model(state: MessagesState):
    model = get_llm().bind_tools(TOOLS)
    messages = [SystemMessage(SYSTEM_PROMPT), *convert_to_messages(state["messages"])]
    return {"messages": [await model.ainvoke(messages)]}


async def call_tools(state: MessagesState):
    return await _tool_node.ainvoke({"messages": convert_to_messages(state["messages"])})


async def route(state: MessagesState):
    # Async on purpose: inside a workflow, LangGraph would run a sync router in a
    # thread executor, which Temporal's workflow sandbox doesn't allow.
    return "tools" if state["messages"][-1].tool_calls else END


def _summarize_model(args, kwargs) -> str:
    try:
        return f"LLM call · {len(args[0]['messages'])} messages in context"
    except Exception:  # summaries run in workflow code and must never raise
        return "LLM call"


def _summarize_tools(args, kwargs) -> str:
    try:
        calls = args[0]["messages"][-1].tool_calls
        return " · ".join(f"{c['name']}({', '.join(map(repr, c['args'].values()))})" for c in calls)[:200]
    except Exception:
        return "tool calls"


def build_state_graph() -> StateGraph:
    """A fresh, uncompiled graph. The Temporal plugin rewrites the nodes it is given,
    so the worker and direct callers each build their own."""
    graph = StateGraph(MessagesState)
    graph.add_node(
        "model",
        call_model,
        metadata={
            "execute_in": "activity",
            "start_to_close_timeout": timedelta(minutes=2),
            "retry_policy": RetryPolicy(initial_interval=timedelta(seconds=2), maximum_attempts=3),
            "summary_fn": _summarize_model,
        },
    )
    graph.add_node(
        "tools",
        call_tools,
        metadata={
            "execute_in": "activity",
            # deep_analysis takes up to 7 minutes; web_search a few seconds.
            "start_to_close_timeout": timedelta(minutes=10),
            "retry_policy": RetryPolicy(initial_interval=timedelta(seconds=2), maximum_attempts=3),
            "summary_fn": _summarize_tools,
        },
    )
    graph.add_edge(START, "model")
    graph.add_conditional_edges("model", route, ["tools", END])
    graph.add_edge("tools", "model")
    return graph


def build_graph(llm: BaseChatModel | None = None):
    """Compiled graph for running the agent directly, outside Temporal."""
    if llm is not None:
        set_llm(llm)
    return build_state_graph().compile()


def recursion_limit(max_steps: int) -> int:
    # Each model or tool step is one superstep; cap them so a looping model can't run forever.
    return max_steps * 2 + 1


async def run_agent(prompt: str, llm: BaseChatModel | None = None, max_steps: int = 10) -> str:
    """Run the agent directly on one prompt and return its final answer."""
    result = await build_graph(llm).ainvoke(
        {"messages": [HumanMessage(prompt)]},
        config={"recursion_limit": recursion_limit(max_steps)},
    )
    return result["messages"][-1].content
