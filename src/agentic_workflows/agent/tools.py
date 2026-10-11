import asyncio
import random

from ddgs import DDGS
from langchain_core.tools import tool


@tool
def web_search(query: str) -> str:
    """Search the internet and return the top results (title, link, snippet).

    Use it for anything current or that you don't already know.
    """
    try:
        results = DDGS().text(query, max_results=5)
    except Exception as e:
        return f"search failed: {e}"
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
    await asyncio.sleep(seconds)
    return f"deep analysis of {topic!r} finished after {seconds // 60}m{seconds % 60:02d}s"


TOOLS = [web_search, deep_analysis]
