# Phase 2 — Agent Core (Playwright + LangGraph)

Goal: a LangGraph graph that runs a ReAct loop over a small tool set (click, type, press key, mark_assertion) against `spree_starter`'s login page (`/users/sign_in`) via Playwright, with DOM observation running as a forced graph step rather than an LLM-invoked tool (see section 2).

## 1. Why not `create_react_agent` or `create_agent`

`langgraph.prebuilt.create_react_agent` carries an explicit "deprecated" badge on LangGraph's current API reference, superseded by `create_agent` from **`langchain.agents`** (not a bare `langchain` import; constructor param is `system_prompt`, not `prompt`). Several other `langgraph.prebuilt` symbols (`AgentState`, `ValidationNode`, `HumanInterrupt*`) are deprecated in the same v1 cleanup — treat them as superseded if seen in older tutorials.

`create_agent` itself is more capable than `create_react_agent` was — it has a real middleware system (`before_model`, `after_model`, `wrap_model_call`, `wrap_tool_call`, all with async variants). **Checked this directly against the installed SDK (langchain 1.3.18) before deciding**: this project's DOM-observation step needs to run exactly once per turn, after however many tools the LLM called, unconditionally — not as something the LLM opts into. The only post-tool-execution hook, `wrap_tool_call`, fires once **per individual tool call**, not once per turn — its `ToolCallRequest` (confirmed via `inspect.signature`) carries no batch position or "is this the last call" signal. Making it fire exactly once per turn would need a hand-written counter stashed in state to detect batch completion — real bookkeeping, just relocated into a middleware class instead of a graph node. A manual `StateGraph` gets this for free: an `observe` node placed on the `tools → agent` edge runs once per turn by construction, no counter needed. That's why this project builds the `StateGraph` manually rather than using either prebuilt entry point.

## 2. Graph skeleton (manual `StateGraph`)

Three nodes, not two — `observe` is the piece that makes DOM refresh happen automatically rather than optionally:

```
START
  │
  ▼
┌─────────┐
│  agent  │◄────────────┐
└────┬────┘              │
     │                   │
     ▼                   │
  tools_condition        │
  (has tool_calls?)      │
     │                   │
  ┌──┴──┐                │
  │     │                │
 END   tools             │
       (ToolNode)        │
        │                │
        ▼                │
     observe ─────────────┘
   (scrape DOM,
   inject into state)
```

- **State schema**: a `TypedDict` (or `MessagesState` from `langgraph.graph.message`) holding at minimum a `messages: list` field (accumulates via `add_messages` reducer) plus a `verdict` field (`None` until `mark_assertion` sets it).
- **Nodes**:
  - `agent` — the only node that calls the LLM (tools bound via `llm.bind_tools([...])`), appends the resulting `AIMessage` to state. Makes the only "decision" in the graph.
  - `tools` — a `ToolNode` instance (from `langgraph.prebuilt`) that executes whatever tool calls are in the last `AIMessage`. Plain deterministic dispatch, no model call.
  - `observe` — plain deterministic code, no model call. Runs the DOM-scrape-with-injected-IDs step (section 4) and appends the result as a message. Exists as its own node specifically so it runs unconditionally after every tool-execution step, regardless of which tool(s) fired — see section 1 for why this can't just be a `wrap_tool_call` middleware hook.
- **Edges**:
  - `START → agent`
  - conditional edge from `agent` using `langgraph.prebuilt.tools_condition` — inspects the last `AIMessage`'s `tool_calls`; routes to `tools` if present, else to `END`.
  - `tools → observe` (normal edge — always re-observe after acting).
  - `observe → agent` (normal edge, closes the loop).

`tools_condition` is the idiomatic routing function name for the `agent → {tools, END}` branch — don't hand-roll an equivalent. The `tools → observe → agent` portion is fully custom, since no prebuilt helper accounts for a forced observation step.

## 3. Terminating on `mark_assertion` — pick one deliberately

`mark_assertion` must route straight to `END`, **bypassing `observe`** — there's nothing left to re-scrape once the agent has already reached a verdict, and running `observe` after `mark_assertion` would waste a Playwright round-trip on a page nobody's going to look at.

Two valid options (see architecture doc's feasibility note):

- **(a) Conditional-edge approach (recommended default)**: `mark_assertion` is just another tool executed by the `tools` node; the routing function *out of `tools`* (not `tools_condition`, which only governs the `agent → {tools, END}` edge) checks if the tool call name was `mark_assertion` and routes straight to `END` instead of to `observe`. Keeps all control flow in the graph definition.
- **(b) `Command`-based approach**: `mark_assertion`'s tool function itself returns `Command(goto=END, update={"verdict": {...}})`, skipping `observe` directly. Valid and slightly less code, but scatters graph control flow into a tool body.

Use (a) unless there's a concrete reason to prefer (b) once you're actually writing this.

## 4. The "Eyes" — DOM scraping with injected IDs, lives in the `observe` node

Not a tool. The LLM never chooses to call this — it's the plain async function inside the `observe` node from section 2, which the graph runs unconditionally after every `tools` step. No `@tool` decorator, no binding to the model, no tool schema for the LLM to see.

Implementation shape:

1. `await page.evaluate(js_snippet)` where the JS walks the DOM (`document.querySelectorAll` over interactive selectors, filtered by visibility/bounding-rect), and stamps a `data-agent-id` attribute (or renders a numbered overlay) on each match.
2. Return a serializable list from the JS call: `[{id, tag, role, text, bbox}, ...]`.
3. Format that list into the simplified text representation the LLM sees (e.g. `[12] <button> "Login"`).

Use `page.evaluate()` (confirmed current async API); `page.add_init_script()` is the alternative if the labeling script needs to run before the page's own scripts do.

**Known limitation (per feasibility review): this breaks on iframes, shadow DOM, and dynamic re-renders.** Confirmed by direct inspection: `spree_starter`'s login page (`/users/sign_in`) is a plain Devise-rendered ERB form with no client-side framework — pure DOM scraping should work cleanly here. (No cart/checkout UI exists in this app to worry about — see phase-5 doc; the Next.js storefront that would have those JS-driven widgets is explicitly not part of this project.)

**Reference implementation worth reading (not copying wholesale)**: browser-use's DOM subsystem (`browser_use/dom/service.py`, `views.py`, `serializer/` on GitHub) is the most commonly cited open-source implementation of exactly this "inject IDs into visible interactive elements, serialize to a numbered list" pattern. Their newer versions drive Chrome via CDP rather than Playwright, so only the labeling/serialization logic transfers, not the browser-driving layer.

## 5. The "Hands" and "Judge" tools

Straightforward `@tool`-decorated async functions wrapping the corresponding Playwright calls:

- `click_element(id: int)` → look up the element by `data-agent-id`, `.click()`.
- `type_text(id: int, text: str)` → look up by ID, `.fill()` or `.type()`.
- `press_key(key: str)` → `page.keyboard.press(key)`.
- `mark_assertion(status: Literal["PASS","FAIL"], reasoning: str)` → sets `state["verdict"]`, terminates per section 3.

## 6. System prompt

Per the architecture doc's Section 5 — prime the agent explicitly to look for edge cases and not just the happy path, and to always call `mark_assertion` before stopping (otherwise the graph has no terminating signal and will just idle at `END` with `verdict` still `None`).

## Phase 2 exit criteria

- Given the instruction "log into the app with valid test credentials," the graph runs to completion and calls `mark_assertion("PASS", ...)`.
- Given deliberately bad credentials, the graph observes Devise's error message and calls `mark_assertion` with a reasoned verdict (not a crash or infinite loop).
- Given the instruction "request a password reset and verify the confirmation message appears," the graph clicks the "forgot password" link, submits an email, and observes the resulting on-page confirmation (the actual email arriving in Mailpit is asserted via a separate tool added in Phase 5, not this phase).
- Manual inspection of the state's `messages` list shows the expected Thought → Action → Observation pattern.

## Verification status

Checked against LangGraph's current API reference and Playwright's docs on 2026-08-31:

| Claim | Status |
| --- | --- |
| `create_react_agent` deprecated, `create_agent` is the replacement | **Confirmed** — explicit deprecation badge on current LangGraph API reference |
| `create_agent` import path | **Corrected**: `langchain.agents`, not a bare `langchain` import |
| `tools_condition` still current, not deprecated | **Confirmed** — no deprecation tag on its reference entry, unlike `create_react_agent` next to it |
| `page.evaluate()` is the correct async DOM-injection API | **Confirmed** against Playwright's own docs |
| `create_agent`'s middleware hooks (`before_model`/`after_model`/`wrap_model_call`/`wrap_tool_call`) exist, with async variants | **Confirmed by direct inspection** of installed `langchain` 1.3.18 (`inspect.signature` on the real classes) |
| `wrap_tool_call` fires once per turn (not once per individual tool call) | **Disproven by direct inspection** — its `ToolCallRequest` (checked via `inspect.signature`) carries no batch-position/last-call signal; it fires per tool call, confirmed from source, not docs. This is why the manual `StateGraph` with an explicit `observe` node was chosen over `create_agent` + middleware. |

### Not verifiable from docs — confirm at Phase 2 kickoff (runtime-only)

- Exact `data-agent-id`-style attribute name and JS selector list — need to inspect the login page's actual DOM (element IDs, ARIA roles present or absent) once the app is running, not assumed in advance. (No iframes/shadow DOM concern remains — confirmed the login page is a plain Devise form with no such complexity, and there's no cart/checkout page in scope to worry about.)
- Whether the LLM provider/model chosen (OpenAI/Anthropic per the architecture doc) reliably produces well-formed tool_calls for this tool set on the first few tries — validate empirically once wired up, prompt-engineer the system prompt if not.
