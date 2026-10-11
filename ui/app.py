"""Throwaway Streamlit chat for poking at the agent by hand. Not part of the product.

Run: uv run streamlit run ui/app.py
"""

import asyncio

import streamlit as st
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from agentic_workflows.agent.graph import build_graph, default_llm, recursion_limit
from agentic_workflows.config import get_settings

st.set_page_config(page_title="Agent playground", page_icon="🔎")
st.title("Agent playground")


@st.cache_resource
def get_graph():
    return build_graph(default_llm())


@st.cache_resource
def event_loop():
    # One loop for the whole session: the cached OpenAI client keeps HTTP connections
    # bound to the loop it first ran on, so a fresh asyncio.run() per turn would break
    # the second turn with "Event loop is closed".
    return asyncio.new_event_loop()


if "messages" not in st.session_state:
    st.session_state.messages = []

with st.sidebar:
    st.caption(f"Model: `{get_settings().llm_model}`")
    if st.button("Clear chat"):
        st.session_state.messages = []
        st.rerun()


def render(msg):
    if isinstance(msg, HumanMessage):
        st.chat_message("user").markdown(msg.content)
    elif isinstance(msg, AIMessage):
        with st.chat_message("assistant"):
            for call in msg.tool_calls:
                st.caption(f"🔧 `{call['name']}({call['args']})`")
            if msg.content:
                st.markdown(msg.content)
    elif isinstance(msg, ToolMessage):
        with st.expander(f"Tool result: {msg.name}"):
            st.text(msg.content)


for msg in st.session_state.messages:
    render(msg)

if prompt := st.chat_input("Ask something that needs a web search..."):
    user_msg = HumanMessage(prompt)
    st.session_state.messages.append(user_msg)
    render(user_msg)

    # The graph's nodes are async (Temporal's LangGraph plugin needs that), so drive it
    # with astream. stream_mode="updates" yields each node's new messages as it finishes,
    # so tool calls show up live.
    async def run_turn():
        async for update in get_graph().astream(
            {"messages": st.session_state.messages},
            config={"recursion_limit": recursion_limit(get_settings().agent_max_steps)},
            stream_mode="updates",
        ):
            for node_output in update.values():
                for msg in node_output["messages"]:
                    st.session_state.messages.append(msg)
                    render(msg)

    with st.spinner("Thinking..."):
        event_loop().run_until_complete(run_turn())
