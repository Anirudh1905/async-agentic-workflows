"""The "Hands" and "Judge": LLM-invoked tools wrapping Playwright actions."""

import asyncio
from typing import Literal

from langchain_core.tools import tool
from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError


async def _settle(page: Page) -> None:
    """Wait for any navigation the last action may have triggered.

    Clicking a submit button (or pressing Enter in a form) can navigate away
    from the current page. Playwright's action auto-wait covers the click
    itself, not what happens after — without this, `observe` can scrape the
    page mid-navigation or a stale DOM snapshot from before the click.
    Tolerates the common case where the action didn't navigate at all.
    """
    try:
        await page.wait_for_load_state("networkidle", timeout=5000)
    except PlaywrightTimeoutError:
        pass


def make_tools(page: Page) -> list:
    """Bind the Playwright page into a fresh set of tool closures for one run."""

    # LangGraph's ToolNode runs every tool call from one LLM turn concurrently
    # (asyncio.gather) with no ordering guarantee between them. A single
    # Playwright page has one execution context: concurrent fill() calls race
    # (confirmed directly — two concurrent type_text calls landed both values
    # in the second field) and a click can start before an earlier fill in
    # the same batch has actually applied (confirmed directly — a submit
    # click execued before its preceding fills landed).
    #
    # A plain asyncio.Lock only gives mutual exclusion, not FIFO ordering
    # relative to gather's scheduling, so it doesn't fix the second case.
    # Instead, each call takes a ticket in the order ToolNode invoked it
    # (Python dicts/lists preserve insertion order, so this matches the
    # order the LLM listed the calls in its response) and waits for its turn.
    _next_ticket = 0
    _serving_ticket = 0
    _turn_changed = asyncio.Condition()

    async def _take_turn():
        nonlocal _next_ticket
        my_ticket = _next_ticket
        _next_ticket += 1
        async with _turn_changed:
            await _turn_changed.wait_for(lambda: _serving_ticket == my_ticket)

    async def _release_turn():
        nonlocal _serving_ticket
        async with _turn_changed:
            _serving_ticket += 1
            _turn_changed.notify_all()

    @tool
    async def click_element(id: int) -> str:
        """Click the interactive element with the given numbered id."""
        await _take_turn()
        try:
            locator = page.locator(f'[data-agent-id="{id}"]')
            await locator.click()
            await _settle(page)
        finally:
            await _release_turn()
        return f"Clicked element {id}."

    @tool
    async def type_text(id: int, text: str) -> str:
        """Type text into the input element with the given numbered id."""
        await _take_turn()
        try:
            locator = page.locator(f'[data-agent-id="{id}"]')
            await locator.fill(text)
        finally:
            await _release_turn()
        return f"Typed into element {id}."

    @tool
    async def press_key(key: str) -> str:
        """Press a keyboard key (e.g. 'Enter', 'Tab')."""
        await _take_turn()
        try:
            await page.keyboard.press(key)
            await _settle(page)
        finally:
            await _release_turn()
        return f"Pressed key {key}."

    @tool
    async def mark_assertion(status: Literal["PASS", "FAIL"], reasoning: str) -> str:
        """Declare the test result and end the run. Call this once you have
        observed enough to reach a verdict — never stop without calling it."""
        return f"Verdict recorded: {status}. {reasoning}"

    return [click_element, type_text, press_key, mark_assertion]
