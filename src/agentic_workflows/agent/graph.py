"""A minimal tool-calling LangGraph agent: model -> (tools -> model)* -> end."""

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition

from agentic_workflows.agent.tools import TOOLS

SYSTEM_PROMPT = (
    "You are a helpful assistant. Use the web_search tool to look things up, "
    "and cite the links you used. "
    "Use the deep_analysis tool only when the user asks for a deep analysis. "
    "Answer concisely once you have what you need."
)


def build_graph(llm: BaseChatModel, tools=TOOLS):
    model = llm.bind_tools(tools)

    def call_model(state: MessagesState):
        messages = [SystemMessage(SYSTEM_PROMPT), *state["messages"]]
        return {"messages": [model.invoke(messages)]}

    graph = StateGraph(MessagesState)
    graph.add_node("model", call_model)
    graph.add_node("tools", ToolNode(tools))
    graph.add_edge(START, "model")
    graph.add_conditional_edges("model", tools_condition)
    graph.add_edge("tools", "model")
    return graph.compile()


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


async def run_agent(prompt: str, llm: BaseChatModel | None = None, max_steps: int = 10) -> str:
    """Run the agent on one prompt and return its final answer."""
    graph = build_graph(llm or default_llm())
    # Each model or tool step is one superstep; cap them so a looping model can't run forever.
    result = await graph.ainvoke(
        {"messages": [HumanMessage(prompt)]},
        config={"recursion_limit": max_steps * 2 + 1},
    )
    return result["messages"][-1].content
