"""Entrypoint: launch Playwright, compile the graph, run one instruction."""

import asyncio

from langchain_core.messages import SystemMessage, HumanMessage
from playwright.async_api import async_playwright

from src.agent.graph import build_graph
from src.agent.observe import scrape_dom
from src.agent.prompts import SYSTEM_PROMPT
from src.agent.tools import make_tools

BASE_URL = "http://localhost:3000"


async def run_agent(instruction: str, start_path: str = "/users/sign_in") -> dict:
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        await page.goto(f"{BASE_URL}{start_path}")

        tools = make_tools(page)
        graph = build_graph(page, tools)

        # Prime the agent with an initial observation — the graph's `observe`
        # node only runs after `tools`, so without this the first tool call
        # would be a guess against a page nobody has scraped yet.
        initial_dom = await scrape_dom(page)
        initial_observation = f"Current URL: {page.url}\nCurrent page state:\n{initial_dom}"

        result = await graph.ainvoke(
            {
                "messages": [
                    SystemMessage(content=SYSTEM_PROMPT),
                    HumanMessage(content=instruction),
                    HumanMessage(content=initial_observation),
                ],
                "verdict": None,
            },
            config={"recursion_limit": 50},
        )

        await browser.close()
        return result


if __name__ == "__main__":
    import sys

    instruction = sys.argv[1] if len(sys.argv) > 1 else "Log into the app with valid test credentials."
    start_path = sys.argv[2] if len(sys.argv) > 2 else "/users/sign_in"
    final_state = asyncio.run(run_agent(instruction, start_path))
    print("\n=== VERDICT ===")
    print(final_state.get("verdict"))
