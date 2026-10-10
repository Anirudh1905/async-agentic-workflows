"""Throwaway Streamlit chat for poking at the agent by hand. Not part of the product.

Run: uv run streamlit run ui/app.py
"""

import streamlit as st
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from agentic_workflows.agent.graph import build_graph, default_llm
from agentic_workflows.config import get_settings

st.set_page_config(page_title="Agent playground", page_icon="🔎")
st.title("Agent playground")


@st.cache_resource
def get_graph():
    return build_graph(default_llm())


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

    max_steps = get_settings().agent_max_steps
    with st.spinner("Thinking..."):
        # stream_mode="updates" yields each node's new messages as it finishes, so tool calls show up live.
        for update in get_graph().stream(
            {"messages": st.session_state.messages},
            config={"recursion_limit": max_steps * 2 + 1},
            stream_mode="updates",
        ):
            for node_output in update.values():
                for msg in node_output["messages"]:
                    st.session_state.messages.append(msg)
                    render(msg)
