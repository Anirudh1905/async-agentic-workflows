"""The "Eyes": DOM scraping with injected IDs.

Not a tool the LLM calls — this is the plain async function run by the
`observe` graph node, unconditionally, after every `tools` step.
"""

import asyncio

from playwright.async_api import Page, Error as PlaywrightError

_SCRAPE_JS = """
() => {
  const selector = 'a, button, input, select, textarea, [role="button"], [onclick]';
  const elements = Array.from(document.querySelectorAll(selector));
  const results = [];
  let nextId = 1;

  for (const el of elements) {
    const rect = el.getBoundingClientRect();
    const visible = rect.width > 0 && rect.height > 0 &&
      window.getComputedStyle(el).visibility !== 'hidden' &&
      window.getComputedStyle(el).display !== 'none';
    if (!visible) continue;

    const id = nextId++;
    el.setAttribute('data-agent-id', String(id));

    const tag = el.tagName.toLowerCase();
    const type = el.getAttribute('type');
    const role = el.getAttribute('role');
    const text = (el.innerText || el.value || el.getAttribute('placeholder') || '').trim().slice(0, 80);
    const label = el.labels && el.labels.length ? el.labels[0].innerText.trim() : null;

    results.push({ id, tag, type, role, text, label });
  }

  return results;
}
"""


def _format_element(el: dict) -> str:
    """Render one scraped element as a single line of the text observation."""
    tag = el["tag"]
    if el.get("type"):
        tag = f'{tag}[type="{el["type"]}"]'
    descriptor = el.get("label") or el.get("text") or el.get("role") or ""
    descriptor = f' "{descriptor}"' if descriptor else ""
    return f'[{el["id"]}] <{tag}>{descriptor}'


async def scrape_dom(page: Page) -> str:
    """Inject numbered IDs into visible interactive elements and format as text.

    Returns a newline-separated list like:
        [1] <input[type="email"]> "Email"
        [2] <input[type="password"]> "Password"
        [3] <input[type="submit"]> "Log in"
    """
    # A click can trigger a chain of redirects (e.g. login -> a second
    # redirect to an admin landing page); the settle-wait after the click can
    # return between hops, destroying this evaluate's execution context
    # mid-flight. Retry once — real content, not a workaround for a bug in
    # our own code.
    try:
        elements = await page.evaluate(_SCRAPE_JS)
    except PlaywrightError as e:
        if "Execution context was destroyed" not in str(e):
            raise
        await asyncio.sleep(0.5)
        elements = await page.evaluate(_SCRAPE_JS)

    if not elements:
        return "(no interactive elements found on the current page)"
    lines = [_format_element(el) for el in elements]
    return "\n".join(lines)
