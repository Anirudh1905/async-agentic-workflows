# **Autonomous AI QA Platform Architecture**

## **1\. Problem Statement**

Traditional end-to-end (E2E) testing requires QA engineers to write brittle, hard-coded scripts (e.g., using raw Selenium or Cypress) that break whenever a UI element changes slightly. Furthermore, writing these tests is time-consuming and difficult to scale.  
**The Goal:** Build a scalable, AI-driven QA platform where users can describe test scenarios in natural English (e.g., *"Log into the app with valid credentials and verify the dashboard loads"* or *"Request a password reset and verify the email arrives"*). The platform must autonomously navigate the target application, execute the steps, handle minor UI changes intelligently, and log the results.

> **Scope note (2026-08-31):** the target app for this project (see 2, below) is a backend admin/auth system with no shopping UI — example scenarios above reflect its actual login/password-reset surface, not a generic e-commerce flow.

> **Staged build-out (2026-09-01, corrected):** this doc describes the full target architecture. It is **not** built all at once. The real build order is **Phase 1 → Phase 2 → Phase 3 → Phase 4 → Phase 5 → Phase 6**, where Phase 3 (`docs/phases/phase-3-backend-orchestration.md`) covers the *entire* `BackgroundTasks`-based orchestration layer via three internal sub-stages — 3a (basic submit→run→ingest→persist loop, gated on a real, observed, correct end-to-end agent run against the actual UI), 3b (read-back `GET` endpoints), 3c (remaining polish, still no Celery/auth) — and **Phase 6** (`docs/phases/phase-6-celery-auth.md`) is the later swap to Celery+Redis+auth+rate-limiting+k8s, gated on Phases 1–5 all working together end-to-end. Celery and auth do not appear anywhere before Phase 6. See `docs/phases/end-goal-architecture.md` for the complete target design those Phase 6 pieces build toward. Sections 3/4 below describe the *end-goal* shape of these pieces — read them alongside the phase docs' staging notes, not as "build this immediately."

## **2\. Solution Overview**

The solution is a decoupled, modern Python stack designed for scalability into a SaaS product. It separates the user interface, orchestration, AI reasoning, and the target application environments.

> * **Frontend (Streamlit):** Lightweight dashboard for users to input natural language tests and view live execution logs.  
> * **Orchestrator (FastAPI):** Async API backend that queues tests, dispatches agents, and manages execution state.  
> * **Agent Engine (LangGraph \+ Playwright):** The core "brain" and "hands" of the system, utilizing a ReAct reasoning loop to interact with web pages dynamically.  
> * **Test Environment (Docker Compose):** Isolated, easily restorable target application — [Spree's Rails backend](https://github.com/spree/spree_starter) (`spree_starter`), used alone with **no code changes** — to ensure clean state between test runs.  
> * **Dependent Entity Twin (Mailpit only):** Real outbound email is captured by a Mailpit mock container in the isolated network for zero side-effect testing, verified by actually triggering and capturing a real email. (No payment twin — out of scope, see 4.4.)

> **Feasibility check (2026-08-31, fully verified by direct PoC — zero open items remain):** Reviewed before implementation, then re-verified by actually running the full stack and triggering real application behavior, not by trusting docs/README claims. Issues found and resolved:
> * **Python version:** Current LangGraph, Playwright, FastAPI, Streamlit, and LangChain all require Python 3.10+; the dev machine's system Python is 3.9.6. **Decision:** manage the project interpreter and venv with `uv`, pinned to **3.13** (all five confirmed compatible via current PyPI classifiers).
> * **Target application — three corrections before landing on the final pick:** (1) OWASP Juice Shop has no real email or payment integration. (2) **Parabank** was assumed to have real email — **disproven by PoC**: zero mail-related code found in any compiled class of a running container. (3) Spree's separate **Next.js storefront** (`spree/storefront`) has real cart/checkout UI, but its own emails route through Resend's HTTP API or a dev-mode file preview — **never SMTP**, confirmed by reading its source directly — so Mailpit cannot intercept them under any configuration. **Final decision:** use **`spree_starter` (Spree's Rails backend) alone**, no Next.js storefront, no code changes. Verified end-to-end: booted the full stack (Postgres + Mailpit + Rails), ran required first-time setup (`db:prepare`, `db:seed`), and triggered a real password-reset email via Devise's own method — it landed in Mailpit within seconds.
> * **Payment twin: out of scope**, by explicit decision — not attempted, not deferred. (For reference: research across 10 OSS demo apps found none support a config-only Stripe API base override; moot now since payments aren't a goal for this project.)
> * **Scope is now login/auth-flow testing, not shopping-cart testing** — `spree_starter` has no product/cart/checkout pages (those live only in the separate, unused Next.js storefront). The agent's target surface is the login page and any backend action reachable via its UI (e.g. password reset).

## **3\. High-Level Design (HLD)**

| Component | Technology Stack | Responsibility |
| :---- | :---- | :---- |
| User Interface | Streamlit | Accepts English prompts, triggers API requests, polls for and displays execution logs and screenshots. |
| API Gateway / Orchestrator | FastAPI, Uvicorn | Exposes endpoints (POST /test-submit, GET /test-status, GET /test-response, GET /test-logs, POST /auth once built). Manages background tasks (staged: BackgroundTasks → Celery+Redis, see 4.1) so the HTTP connection isn't blocked by slow browser tests. |
| Reasoning Engine | LangGraph, LLM (OpenAI/Anthropic) | Maintains the ReAct loop (Thought \-\> Action \-\> Observation). Translates high-level goals into low-level browser commands. |
| Browser Controller | Playwright (Async) | Executes the actual DOM manipulations (clicking, typing) in a headless Chromium instance. |
| Target Application & Twin | Docker Compose | Hosts Spree's Rails backend, `spree_starter` (the app being tested, unmodified) and its email digital twin (Mailpit) to ensure a consistent, zero side-effect network environment. |

## **4\. Low-Level Design (LLD)**

### **4.1 Orchestration Workflow — staged: FastAPI Background Tasks now, Celery+Redis later**

Because browser tests are long-running, FastAPI handles them asynchronously.

> **Staging decision (2026-09-01, corrected, supersedes the earlier arq note):** Phase 3 in full (3a basic loop, 3b read-back GET endpoints, 3c polish) uses `BackgroundTasks` — no Celery anywhere in Phase 3. Migration to **Celery + Redis** — not arq — is **Phase 6**, gated on Phases 1–5 all working end-to-end together, not an internal Phase 3 sub-stage. The end-goal diagram specifies Celery explicitly, with its own rationale (instant responsiveness, horizontal scalability, fault tolerance) and Redis over SQS (latency, native Celery support) — see `end-goal-architecture.md` sections 3–4 and `phase-6-celery-auth.md` for the full reasoning. `BackgroundTasks` runs in-process with no persistence, retries, or cancellation throughout Phase 3 — a server restart silently kills in-flight jobs, and status tracking must be hand-rolled. Acceptable through Phase 5; not the final architecture.

@app.post("/test-submit")  
async def submit\_test(intent: TestIntent, bg\_tasks: BackgroundTasks):  
run\_id \= generate\_uuid()  
\# Pass to LangGraph in background  
bg\_tasks.add\_task(run\_agent, run\_id, intent.instruction)  
return {"run\_id": run\_id, "status": "queued"}

### **4.2 The Agent's Tools (Generic vs. Specific)**

To ensure the agent works on *any* application, it uses generic low-level tools rather than app-specific macros (like login\_tool).

> * **The "Eyes" (Observation Tool):** Scrapes the DOM and returns a simplified representation to the LLM. It injects unique IDs (e.g., \[12\]) next to interactive elements (buttons, inputs) so the LLM knows exactly how to reference them.  
> * **The "Hands" (Action Tools):**  
  * click\_element(id: int)  
  * type\_text(id: int, text: str)  
  * press\_key(key: str)  
> * **The "Judge" (Assertion Tool):** mark\_assertion(status, reasoning). The agent uses this tool to terminate the graph and officially declare the test a PASS or FAIL based on its final observations.

> **Feasibility note (Eyes):** Pure DOM scraping with injected IDs is a real, validated pattern (this is essentially how browser-use works), but it's known to break on iframes, shadow DOM, and dynamic re-renders. Best-in-class implementations (Skyvern, browser-use) pair it with the accessibility tree. `spree_starter`'s login page is plain server-rendered Rails/ERB (Devise's default form) — confirmed by direct inspection to be a standard HTML form, no JS framework — so pure DOM scraping should hold up well here.
>
> **Feasibility note (Judge):** Two valid LangGraph implementations exist — (a) `mark_assertion` returns a `Command(goto=END, update={...})` directly, or (b) a conditional edge inspects the last message's tool_calls and routes to END when it sees `mark_assertion`. (b) is the more common idiomatic pattern in current LangGraph ReAct examples since it keeps control flow centralized in the graph rather than in the tool body — recommended default unless there's a reason to prefer (a).

### **4.3 The ReAct Loop (LangGraph Execution)**

When given a prompt like *"Test the login page"*, the LangGraph node follows this internal monologue:

> 1. **Action:** Call Observation tool.  
> 2. **Observation:** Sees \`\[15\] Login Button\`.  
> 3. **Thought:** I need to click the login button to proceed.  
> 4. **Action:** click\_element(15).  
> 5. **Thought:** I should test negative credentials first to ensure validation works.  
> 6. **Action:** type\_text(20, "bad@email") \-\> click\_element(25).  
> 7. **Observation:** Sees error message on screen.  
> 8. **Action:** mark\_assertion("PASS", "Validation caught bad email").

### **4.4 Stateful Digital Twin for Dependent Entities (Mailpit, email only)**

Instead of testing against a real mail server, the platform isolates tests by running an email-capture service (Mailpit) inside the same local Docker network as a lightweight mock or "twin". This ensures tests have zero side effects and are completely deterministic. The target app sends outgoing emails to `mailpit:1025`, and the AI agent can query `http://mailpit:8025/api/v1/messages` to assert email contents like a password-reset link.

> **Feasibility note — fully verified end-to-end, 2026-08-31:** Two earlier picks failed this bar before landing on the right one — Juice Shop has no real email at all; Parabank was assumed to (per its docs) but a direct PoC found zero mail-related code in its compiled classes; Spree's Next.js storefront has real UI but its emails bypass SMTP entirely (Resend/dev-file-preview), so Mailpit can't intercept them regardless of config. **`spree_starter` (Spree's Rails backend, used alone) is the final, fully verified target**: booted the complete stack, ran required first-time setup, and triggered Devise's real `send_reset_password_instructions` method against a seeded user — a "Shop Password Reset Instructions" email landed in Mailpit within seconds. Zero source-code changes were made anywhere in this path.
>
> **No payment twin.** Out of scope by explicit decision, not attempted. Phase 5 is email-twin only, against the login/password-reset flow specifically (this backend has no shopping cart to test).

## **5\. Important Details & Best Practices**

> * **System Prompting:** The LLM must be primed with a strong System Prompt: *"You are an autonomous QA engineer. Do not just execute the happy path. Look for edge cases, verify UI state changes, and use the mark\_assertion tool when complete."*  
> * **Idempotency in Testing:** Target apps must be easily resettable. Using the image-based Compose setup (\`ghcr.io/spree/spree:5.6.1\`, no build step), tearing down and rebuilding the Postgres database between test runs is a single command (docker-compose down \-v && docker-compose up \-d) — the image's own entrypoint re-runs \`db:prepare\` automatically and a seeded admin user is already present, so no separate setup step is needed. Noting the \`-v\` to also drop the named Postgres volume.  
> * **Playwright Contexts:** Use Playwright's lightweight contexts instead of launching a full browser binary for every test. This drastically reduces memory overhead.

## **6\. Implementation Plan**

| Phase | Focus Area | Key Deliverables |
| :---- | :---- | :---- |
| **Phase 1** | Infrastructure & Targets | \- Provision Python 3.13 project env via \`uv\` (pyproject.toml \+ uv.lock). \- Set up Docker Compose using pre-built images only — \`ghcr.io/spree/spree:5.6.1\` \+ \`postgres:18-alpine\` \+ \`axllent/mailpit\`, no clone, no build step, verified working. \- Ensure the login page is accessible via localhost:3000/users/sign\_in. |
| **Phase 2** | Agent Core (Playwright \+ LangGraph) | \- Write the DOM extraction script (injecting IDs) against the login page. \- Wrap Playwright functions into LangChain Tools. \- Build the LangGraph state machine and ReAct loop (manual \`StateGraph\`, not the deprecated \`create_react_agent\`). \- Write the QA Engineer System Prompt. |
| **Phase 3 (3a)** | Backend Orchestration — basic loop | \- Scaffold FastAPI app with \`POST /test-submit\` using \`BackgroundTasks\`. \- Build per-run ephemeral test environment spin-up/teardown, using a per-run \`--env-file\` for consistent port allocation across all \`docker compose\` calls (verified pattern \- see phase-3 doc for a real port-collision bug found and fixed). \- Stand up self-hosted Langfuse (Docker Compose, 6 containers, verified working) and wire tracing via an explicit \`CallbackHandler\` \+ \`start\_as\_current\_span(trace\_context=...)\` (not zero-code like LangSmith \- verified); call \`flush()\` before \`run\_agent\` returns. \- Persist run status \+ final response to a SQL \`agent\_response\` table. \- **Exit bar: a real, observed, correct agent run against the actual UI (valid AND invalid credentials scenarios), not just passing code.** |
| **Phase 3 (3b)** | Backend Orchestration — read-back | \- Add \`GET /test-status\`, \`GET /test-response\` (read the SQL table). \- Add \`GET /test-logs\` (read from self-hosted Langfuse via \`GET /api/public/v2/observations?traceId=...\`, verified working). \- Gated on 3a's exit bar being met, not just code existing. |
| **Phase 3 (3c)** | Backend Orchestration — polish | \- Remaining hardening surfaced by real 3a/3b runs (error handling, timeouts, orphaned-container cleanup). \- Still \`BackgroundTasks\`, still no Celery/auth. |
| **Phase 4** | Frontend Dashboard | \- Build Streamlit UI with text input for test intent (depends on 3b's GET endpoints). \- Implement polling mechanism. \- Display pass/fail status and execution traces to the user. |
| **Phase 5** | Stateful Digital Twin (email only) | \- Confirm Mailpit wiring (already default in \`spree\_starter\`'s compose file, verified). \- Add a \`check_mailpit_inbox()\` agent tool. \- Have the agent drive the login page's "forgot password" link (triggers the same Devise flow verified in the PoC) and assert on the resulting Mailpit-captured email. No payment twin — out of scope. |
| **Phase 6** | Celery \+ Auth (production shape) | \- **Gated on Phases 1–5 all working end-to-end together.** \- Swap \`BackgroundTasks\` for Celery \+ Redis queue. \- Add \`POST /auth\` \+ \`auth\_schema\`, gate other endpoints on it. \- Add token-bucket rate limiting (Redis-backed). \- Move deployment to k8s; add Prometheus/Grafana \+ Promtail/Loki. Full design in \`docs/phases/end-goal-architecture.md\` and \`docs/phases/phase-6-celery-auth.md\`. |

