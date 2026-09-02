"""The agent's StateGraph: agent <-> tools <-> observe, with mark_assertion
bypassing observe straight to END.
"""

from typing import Annotated, Literal, Optional, TypedDict

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import BaseTool
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode
from playwright.async_api import Page

from src.agent.observe import scrape_dom


class AgentState(TypedDict):
    messages: Annotated[list, add_messages]
    verdict: Optional[dict]


def _last_ai_message(state: AgentState) -> AIMessage:
    for msg in reversed(state["messages"]):
        if isinstance(msg, AIMessage):
            return msg
    raise ValueError("No AIMessage found in state.")


def build_graph(page: Page, tools: list[BaseTool], model: str = "gpt-4o-mini"):
    llm = ChatOpenAI(model=model)
    llm_with_tools = llm.bind_tools(tools)
    tool_node = ToolNode(tools)

    async def agent_node(state: AgentState) -> dict:
        response = await llm_with_tools.ainvoke(state["messages"])
        return {"messages": [response]}

    async def observe_node(state: AgentState) -> dict:
        dom_snapshot = await scrape_dom(page)
        content = f"Current URL: {page.url}\nCurrent page state:\n{dom_snapshot}"
        return {"messages": [HumanMessage(content=content)]}

    async def extract_verdict_node(state: AgentState) -> dict:
        # mark_assertion's ToolMessage is the last message once ToolNode has run it.
        last_ai = _last_ai_message(state)
        for call in last_ai.tool_calls:
            if call["name"] == "mark_assertion":
                return {"verdict": {"status": call["args"]["status"], "reasoning": call["args"]["reasoning"]}}
        return {}

    def route_after_agent(state: AgentState) -> Literal["tools", "__end__"]:
        last_ai = _last_ai_message(state)
        if not last_ai.tool_calls:
            return END
        return "tools"

    def route_after_tools(state: AgentState) -> Literal["observe", "extract_verdict"]:
        last_ai = _last_ai_message(state)
        if any(call["name"] == "mark_assertion" for call in last_ai.tool_calls):
            return "extract_verdict"
        return "observe"

    graph = StateGraph(AgentState)
    graph.add_node("agent", agent_node)
    graph.add_node("tools", tool_node)
    graph.add_node("observe", observe_node)
    graph.add_node("extract_verdict", extract_verdict_node)

    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", route_after_agent, {"tools": "tools", END: END})
    graph.add_conditional_edges("tools", route_after_tools, {"observe": "observe", "extract_verdict": "extract_verdict"})
    graph.add_edge("observe", "agent")
    graph.add_edge("extract_verdict", END)

    return graph.compile()
