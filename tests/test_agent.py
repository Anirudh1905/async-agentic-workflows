import asyncio

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, ToolMessage

from agentic_workflows.agent import tools
from agentic_workflows.agent.graph import build_graph, run_agent
from agentic_workflows.agent.tools import web_search


class FakeToolModel(FakeMessagesListChatModel):
    """Replays scripted responses; bind_tools is a no-op so the graph can wire it up."""

    def bind_tools(self, tools, **kwargs):
        return self


class FakeDDGS:
    """Stands in for ddgs.DDGS so tests never hit the network."""

    def __init__(self, results=None, error=None):
        self.results, self.error = results, error

    def __call__(self):
        return self

    def text(self, query, max_results):
        if self.error:
            raise self.error
        return self.results


def test_web_search_formats_results(monkeypatch):
    monkeypatch.setattr(tools, "DDGS", FakeDDGS([{"title": "T", "href": "https://x.io", "body": "snippet"}]))
    assert web_search.invoke({"query": "x"}) == "T\nhttps://x.io\nsnippet"


def test_web_search_handles_empty_and_errors(monkeypatch):
    monkeypatch.setattr(tools, "DDGS", FakeDDGS([]))
    assert web_search.invoke({"query": "x"}) == "no results"
    monkeypatch.setattr(tools, "DDGS", FakeDDGS(error=RuntimeError("rate limited")))
    assert web_search.invoke({"query": "x"}) == "search failed: rate limited"


def test_agent_calls_tool_then_answers(monkeypatch):
    monkeypatch.setattr(tools, "DDGS", FakeDDGS([{"title": "Paris", "href": "https://p.fr", "body": "capital"}]))
    llm = FakeToolModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[{"name": "web_search", "args": {"query": "capital of France"}, "id": "call_1"}],
            ),
            AIMessage(content="Paris."),
        ]
    )
    result = asyncio.run(build_graph(llm).ainvoke({"messages": [("user", "capital of France?")]}))

    tool_msgs = [m for m in result["messages"] if isinstance(m, ToolMessage)]
    assert [m.content for m in tool_msgs] == ["Paris\nhttps://p.fr\ncapital"]
    assert result["messages"][-1].content == "Paris."


def test_run_agent_returns_final_answer():
    llm = FakeToolModel(responses=[AIMessage(content="hello")])
    assert asyncio.run(run_agent("hi", llm=llm)) == "hello"
