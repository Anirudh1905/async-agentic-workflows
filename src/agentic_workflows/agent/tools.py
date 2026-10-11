import asyncio
import logging
import random
import time

from ddgs import DDGS
from langchain_core.tools import tool

from agentic_workflows.logs import step_logger

_log = logging.getLogger(__name__)


@tool
def web_search(query: str) -> str:
    """Search the internet and return the top results (title, link, snippet).

    Use it for anything current or that you don't already know.
    """
    started = time.monotonic()
    try:
        results = DDGS().text(query, max_results=5)
    except Exception as e:
        step_logger(_log).warning("web_search failed query=%r: %s", query, e)
        return f"search failed: {e}"
    step_logger(_log).info("web_search query=%r results=%d in %.1fs", query, len(results or []), time.monotonic() - started)
    if not results:
        return "no results"
    return "\n\n".join(f"{r['title']}\n{r['href']}\n{r['body']}" for r in results)


@tool
async def deep_analysis(topic: str) -> str:
    """Run a slow, in-depth analysis of a topic. Takes 5-7 minutes.

    Only use it when the user explicitly asks for a deep analysis.
    """
    # Testing aid: simulates a long-running tool so long agent runs can be exercised end to end.
    seconds = random.randint(5 * 60, 7 * 60)
    step_logger(_log).info("deep_analysis started topic=%r will_take=%ds", topic, seconds)
    await asyncio.sleep(seconds)
    step_logger(_log).info("deep_analysis finished topic=%r", topic)
    return f"deep analysis of {topic!r} finished after {seconds // 60}m{seconds % 60:02d}s"


TOOLS = [web_search, deep_analysis]
