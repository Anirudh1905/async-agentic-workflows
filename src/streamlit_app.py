"""Manual test UI for the Phase 2 agent — not the Phase 3 API, just a way to
drive src.agent.run.run_agent() by hand without the CLI.
"""

import asyncio
import sys
from pathlib import Path

# Streamlit execs this file directly and doesn't add the project root to
# sys.path the way `python -m` does — without this, `from src.agent...`
# fails with ModuleNotFoundError regardless of how the process was launched.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import streamlit as st
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from src.agent.run import run_agent

st.set_page_config(page_title="Agent test runner", layout="wide")
st.title("Agent test runner")

with st.form("run_form"):
    instruction = st.text_area(
        "Instruction",
        value="Log in with email agent-test@example.com and password TestPassword123! and confirm the login succeeds.",
        height=100,
    )
    start_path = st.text_input("Start path", value="/users/sign_in")
    submitted = st.form_submit_button("Run")

if submitted:
    with st.spinner("Running agent..."):
        result = asyncio.run(run_agent(instruction, start_path))

    verdict = result.get("verdict")
    if verdict:
        status = verdict.get("status")
        (st.success if status == "PASS" else st.error)(
            f"**{status}** — {verdict.get('reasoning')}"
        )
    else:
        st.warning("No verdict recorded — the agent stopped without calling mark_assertion.")

    st.subheader("Trace")
    for msg in result["messages"]:
        if isinstance(msg, SystemMessage):
            continue  # not interesting to see on every run
        elif isinstance(msg, HumanMessage):
            with st.chat_message("human"):
                st.text(msg.content)
        elif isinstance(msg, AIMessage):
            with st.chat_message("assistant"):
                if msg.content:
                    st.write(msg.content)
                for call in msg.tool_calls:
                    st.code(f"{call['name']}({call['args']})", language="python")
        elif isinstance(msg, ToolMessage):
            with st.chat_message("assistant", avatar="🛠️"):
                st.text(msg.content)
