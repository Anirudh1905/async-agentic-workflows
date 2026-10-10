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


TOOLS = [web_search]
