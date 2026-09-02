# Phase 2 — Agent Core (Playwright + LangGraph)

Goal: a LangGraph graph that runs a ReAct loop over a small tool set (click, type, press key, mark_assertion) against `spree_starter`'s login page (`/users/sign_in`) via Playwright, with DOM observation running as a forced graph step rather than an LLM-invoked tool (see section 2).

**Status: implemented and verified end-to-end, 2026-09-02.** All four exit criteria pass against the real running `target-app` stack. This doc has been updated with real bugs found and fixed during implementation — read section 7 before touching this code, since several of these are not obvious from the design alone.

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

`@tool`-decorated async functions wrapping the corresponding Playwright calls — **not** as simple as first designed. See section 7.2/7.3 for why these need an ordering primitive and a post-action settle-wait, not just a direct Playwright call each:

- `click_element(id: int)` → takes a turn (ordering primitive), locates by `data-agent-id`, `.click()`, waits for any resulting navigation to settle.
- `type_text(id: int, text: str)` → takes a turn, locates by ID, `.fill()`. No settle-wait — filling a field doesn't navigate.
- `press_key(key: str)` → takes a turn, `page.keyboard.press(key)`, waits for any resulting navigation to settle (e.g. Enter submitting a form).
- `mark_assertion(status: Literal["PASS","FAIL"], reasoning: str)` → no page interaction, just returns its args as the tool result; the graph's `extract_verdict` node (not the tool itself) reads them into `state["verdict"]`, per section 3.

## 6. System prompt

Per the architecture doc's Section 5 — prime the agent explicitly to look for edge cases and not just the happy path, and to always call `mark_assertion` before stopping (otherwise the graph has no terminating signal and will just idle at `END` with `verdict` still `None`).

**One addition beyond the original design, required by 7.6**: the prompt (`src/agent/prompts.py`) also has to tell the agent to treat a changed URL (surfaced at the top of every observation, per 7.6) as evidence a prior action already succeeded, and to never act on a page it wasn't asked to test. Without this, the agent reliably tried to log in a second time on whatever page it landed on after a successful first login — not a hallucination, a literal reading of "confirm the login succeeds" that didn't account for landing somewhere unexpected.

## 7. Real bugs found and fixed during implementation

Everything below was hit by actually running the graph against the live `target-app` stack, not anticipated in the design. Read this before modifying `src/agent/`.

### 7.1 The graph needs a priming observation before the loop starts

The `observe` node only runs on the `tools → observe → agent` edge — nothing observes the page *before* the very first `agent` turn. Without an initial observation, the LLM's first tool call is a pure guess against a page nobody has scraped, and it reliably picks nonexistent `data-agent-id` values (confirmed directly — first attempt guessed `id=1` against a page where no IDs had been injected yet, causing a 30s Playwright timeout). **Fix**: `run.py` calls `scrape_dom()` once before invoking the graph and seeds it as an extra `HumanMessage` alongside the instruction. This isn't mentioned in the original graph design (section 2) because it's an artifact of how the entrypoint wires up the *first* state, not the graph's internal edges — but it's required for the graph to be usable at all.

### 7.2 `ToolNode` runs all of a turn's tool calls concurrently — this breaks concurrent fills

LangGraph's `ToolNode` executes every tool call from one LLM turn via `asyncio.gather`, with no ordering guarantee. A single Playwright `Page` has one execution context: two concurrent `.fill()` calls on different fields **race and corrupt each other** — confirmed directly: filling email and password concurrently landed both strings concatenated into the *second* field, leaving the first empty. Worse, a `click_element` batched in the same turn as its preceding `type_text` calls can start executing **before** those fills land, since `gather` provides no cross-call ordering at all — confirmed directly: a submit click fired while the email/password fields were still empty.

**Fix, in `src/agent/tools.py`**: a ticket-based ordering primitive (not a plain `asyncio.Lock`, which gives mutual exclusion but not ordering) ensures each tool call executes strictly in the order the LLM listed them in its response, one at a time. `_take_turn()`/`_release_turn()` hand out sequential tickets at coroutine-creation time (matches LLM-listed order, since `asyncio.gather` starts coroutines in argument order and each one synchronously grabs its ticket before its first `await`) and block until it's that ticket's turn to run. Verified directly: rerunning the same concurrent-fill reproduction with the fix in place produced correct field values and a correct login.

### 7.3 A click's navigation isn't waited for automatically

`locator.click()` auto-waits for the click itself to be actionable, but returns as soon as the click fires — not once whatever page transition it triggers has settled. Without an explicit wait, `observe` can scrape a page mid-navigation, or fail outright with `Page.evaluate: Execution context was destroyed, most likely because of a navigation`. **Fix**: a `_settle()` helper (`src/agent/tools.py`) calls `page.wait_for_load_state("networkidle", timeout=5000)` after `click_element` and `press_key`, tolerating the common case where nothing navigated at all (`TimeoutError` is caught and ignored). A redirect chain with more than one hop (e.g. login → an intermediate redirect) can still destroy `observe`'s evaluate context between the settle-wait returning and the scrape running — `scrape_dom()` (`src/agent/observe.py`) catches that specific error and retries once after a short delay.

### 7.4 Two separate Devise user models exist — know which one a route authenticates

`spree_starter` (via the `ghcr.io/spree/spree` image) ships **two independent Devise scopes**: `Spree::User` (customer-facing, route `/users/sign_in`) and `Spree::AdminUser` (admin panel, route `/admin_user/sign_in` — note the singular "user", distinct from the also-existing generic `/admin_users/sign_in` with a plural). They are not the same account and do not share credentials. **Confirmed by direct testing**: the image's own seed data creates a `Spree::AdminUser` but zero `Spree::User` records — testing `/users/sign_in` against the seeded admin's credentials always fails, not because of any bug, but because that account doesn't exist in the model backing that route. A real `Spree::User` had to be created directly (`Spree.user_class.new(email:, password:, password_confirmation:).save!`) to test the customer login flow at all.

### 7.5 Real, reproducible bug: the admin panel's post-login redirect loops forever

`/admin_user/sign_in` accepts valid `Spree::AdminUser` credentials, authenticates successfully (confirmed: SolidQueue and server logs show a real 302 after `POST`), but every subsequent page — including the redirect target itself — bounces back to `/admin_user/sign_in` in an infinite loop, eventually surfacing as `chrome-error://chromewebdata/` once Chrome's own redirect-count safety limit trips. Root cause, read directly from `spree_admin`'s `base_controller.rb`: `redirect_unauthorized_access` checks `try_spree_current_user`; when that returns falsy despite `sign_in(resource_name, resource)` having just run in the sessions controller, every admin-protected page (including the sign-in page's own `require_no_authentication` guard, which fires for authenticated users) redirects back to sign-in. **Confirmed not a version fluke**: reproduced identically on both `ghcr.io/spree/spree:5.6.1` and `:5.5.3`, with a completely fresh browser context (rules out session/cookie pollution from repeated manual testing). **Decision**: do not test the admin panel. Use `/users/sign_in` (the customer login) for this phase — it works correctly end-to-end. The admin-panel bug is a legitimate fact about this application, not a blocker; if the admin UI is ever back in scope, this is exactly the kind of thing worth having the agent detect and report, not something to silently route around.

### 7.6 A successful customer login lands on the (broken) admin sign-in page — this is expected, not a bug in our code

Because this Spree instance has no Store/storefront configured, `Spree::User`'s `after_sign_in_path_for` falls back to the app's `root_path`, which 301-redirects to `/admin` — which, per 7.5, bounces to `/admin_user/sign_in`. So a **correct, successful** customer login visibly lands on a login form for a completely different account type. Confirmed this is real Devise/Rails routing behavior, not a bug in our tools, by testing the identical sequence via raw Playwright calls outside the graph.

**This created a real agent-behavior bug worth knowing about**: the first several implementation attempts had the agent see this second login form and try to log in *again* on it (a different failure — that page's submit button also happens to be Turbo-driven and gets stuck `disabled` on a stale click attempt). **Fix, two parts**: (1) `observe_node` and the priming observation in `run.py` now both prefix every page-state message with `Current URL: {page.url}`, giving the agent ground truth about navigation independent of what the new page's DOM looks like; (2) the system prompt (`src/agent/prompts.py`) explicitly instructs the agent to treat a changed URL as evidence of a successful prior action, and to never act on a page it wasn't asked to test. Confirmed fixed: reran the exact same instruction after this change and the agent correctly called `mark_assertion("PASS", ...)` citing the URL change, without touching the second form.

### 7.7 This app shows no visible feedback on two flows that actually work

Neither a failed login nor a successful password-reset request produces any visible on-page message (`inner_text('body')` confirmed no error/confirmation text in either case) — despite the backend genuinely doing the right thing in both cases (login correctly rejected; reset email genuinely sent and confirmed landing in Mailpit). This isn't a scraper bug — the flash message rendering appears to be missing from this app's views. **Consequence for exit criteria**: the agent's FAIL verdicts on "verify the error/confirmation message appears" instructions are *correct*, not evidence of a broken agent — the instruction asked it to verify something that genuinely doesn't happen on this page. Phrase future test instructions around what's actually observable (e.g. "confirm the URL doesn't change" rather than "confirm an error message appears") if that distinction matters for a given scenario.

## Phase 2 exit criteria — all confirmed against the real running stack, 2026-09-02

- [x] Given "log in with valid test credentials, confirm the login succeeds," the graph runs to completion and calls `mark_assertion("PASS", ...)`, correctly reasoning from the observed URL change.
- [x] Given deliberately bad credentials, the graph reaches a reasoned `mark_assertion("FAIL", ...)` (not a crash or infinite loop) — correctly, since this app shows no visible error message and the URL genuinely doesn't change (see 7.7).
- [x] Given "request a password reset and verify a confirmation message appears," the graph submits the reset form and reaches a reasoned verdict based on what it actually observed (FAIL is the *correct* verdict here — see 7.7 — the email itself was independently confirmed to land in Mailpit).
- [x] Manual inspection of the state's `messages` list shows the expected Thought → Action → Observation pattern, including tool_calls and their results, across all three scenarios above.

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
