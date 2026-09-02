# Phase 3 — Backend Orchestration (FastAPI `BackgroundTasks`, no Celery)

**Corrected phase structure, 2026-09-01**: Phase 3 in its entirety uses `BackgroundTasks` — no Celery anywhere in this phase. It has three internal sub-stages (3a/3b/3c), all still `BackgroundTasks`-based:

- **3a**: FastAPI `BackgroundTasks` triggers the agent run against a **per-run ephemeral test environment**, ingests the agent's thinking/trace logs to **self-hosted Langfuse**, and dumps the final agent response to the **agent DB**. No GET endpoints yet, no auth. Exit bar is a real, observed, correct agent run — not just "the code doesn't crash."
- **3b**: add `GET` endpoints to read back what 3a already writes — status, logs, and the final response.
- **3c**: whatever polish/hardening remains within `BackgroundTasks` scope (error handling, edge cases surfaced by 3a/3b's real runs) — still no Celery, no auth.

**Overall project build order**: Phase 1 → Phase 2 → Phase 3 (3a→3b→3c) → Phase 4 (frontend) → Phase 5 (digital twins) → **everything through Phase 5 working end-to-end** → **Phase 6** (Celery + Redis + auth + rate limiting + k8s, see `phase-6-celery-auth.md`). Celery and auth are Phase 6 only — not part of Phase 3 at all. This corrects an earlier draft of this doc that mislabeled the Celery/auth work as "3c" and the GET endpoints as a separate "Phase 6" — both were wrong; see `phase-6-celery-auth.md` for the real Phase 6.

**Infra note**: 3a needs **two separate** Docker Compose stacks running — the per-run `spree_starter`+Postgres+Mailpit stack (per-run, ephemeral, see below) AND a **separate, long-lived** self-hosted Langfuse stack (Postgres+ClickHouse+Redis+MinIO+web+worker, 6 containers — booted once, not per-run, since it's the shared observability backend all runs report into). Don't run these on the same Postgres instance or same Compose project; keep them fully independent.

## 3a. FastAPI `BackgroundTasks` + per-run ephemeral environment + Langfuse + agent DB

### Endpoint and background job

```python
@app.post("/test-submit")
async def submit_test(intent: TestIntent, bg_tasks: BackgroundTasks):
    run_id = generate_uuid()
    await db.create_run(run_id, user_id=None, status="queued", user_question=intent.instruction)
    bg_tasks.add_task(run_agent, run_id, intent.instruction)
    return {"run_id": run_id, "status": "queued"}
```

`user_id` is `None`/nullable throughout Phase 3 — there's no `/auth` until Phase 6. Don't fake a UUID for it; a real placeholder (`NULL`) is more honest than a value that looks real but isn't tied to anything.

### Per-run ephemeral test environment — verified working, with one real bug found and fixed

Per the end-goal diagram, each agent run gets its **own** freshly-spun-up test environment, not a shared long-lived one.

**Verified by direct smoke test, 2026-09-01, against the (now superseded) dev-mode clone+build setup**: booted two fully isolated stacks concurrently (`docker compose -p run-alpha ... up -d` and `-p run-beta ...`, each with its own host ports for `web`/Postgres/Mailpit). Confirmed genuine isolation: triggered a password-reset email on `run-alpha` only, and `run-beta`'s Mailpit inbox stayed empty for the whole test while `run-alpha`'s captured it correctly. Both stacks' login pages worked independently throughout.

**Real bug found and fixed during that test — still applies regardless of which Compose setup is used**: passing port overrides as ad hoc shell env vars on one `docker compose` invocation (e.g. `up -d`) and omitting them on a later invocation against the *same* run causes Compose to fall back to the compose file's *default* ports on the second call — silently reusing `1025`/`8025`/`5433` instead of the run's actual allocated ports, and recreating/colliding with the dependency containers. **Fix, confirmed working**: write each run's port allocations to a dedicated `--env-file` and pass `--env-file` consistently on **every** `docker compose` call for that run — `up` and `down` alike. Never rely on ad hoc shell-exported env vars across multiple invocations against the same run.

**Simplified since Phase 1's image-based rewrite (2026-09-02)**: with `ghcr.io/spree/spree:5.6.1` (see `phase-1-infrastructure.md`), there's no build step and no manual `db:prepare`/`db:seed` — the image's own entrypoint handles `db:prepare` automatically on every boot and already ships a seeded admin user. This simplifies `run_agent`'s per-run sequence:

1. Allocate a port set for this run (e.g. derive from a counter or a small free-port scan) and write them into a per-run env file.
2. `docker compose -p run-{run_id} --env-file /path/to/run-{run_id}.env -f docker-compose.yml up -d` — `web` boots ready to use, no separate setup calls needed. (The base compose file pins `name: target-app` for the shared long-lived stack; `-p run-{run_id}` on this per-run invocation overrides that with a distinct isolated project name, exactly as intended.)
3. Point Playwright at this run's allocated port for `web`.
4. On completion (pass, fail, or error — always, via try/finally): `docker compose -p run-{run_id} --env-file ... down -v`, then delete the per-run env file.

**Not yet re-verified**: the concurrent-isolation smoke test above was run against the old dev-mode setup, not this simplified image-based sequence. The underlying mechanism (Compose project-name scoping, `--env-file`-based port allocation) is unchanged and should carry over, but re-run the two-concurrent-runs test against the actual image-based compose file before trusting it in 3a's real implementation.

### Langfuse (self-hosted) ingestion (agent thinking logs)

**Decision, 2026-09-01**: use **self-hosted Langfuse** instead of LangSmith — open-source, no external API key/signup needed. This was fully verified by a direct PoC (below), not assumed from docs, and it surfaced two real gotchas worth knowing before writing the integration code.

**Self-hosting requirement — heavier than a single container**: Langfuse's official `docker-compose.yml` (from `github.com/langfuse/langfuse`) runs **6 containers**: `langfuse-web`, `langfuse-worker`, `postgres`, `clickhouse`, `redis`, and `minio` (S3-compatible blob storage). This is meaningfully heavier than Mailpit's single container, but it's real and it works — **confirmed by direct boot**: all 6 came up healthy, web UI responded 200, within about 30 seconds on this machine. Use the compose file as-is; don't hand-roll a lighter version, since Langfuse genuinely needs all of these services (Postgres for app data, ClickHouse for trace/observation storage, Redis for its job queue, MinIO for event/media blobs).

To skip the UI signup step, set these env vars before first boot (in a `.env` file next to `docker-compose.yml`) to auto-provision a project and API keys:

```bash
LANGFUSE_INIT_ORG_ID=<any-id>
LANGFUSE_INIT_ORG_NAME=<any-name>
LANGFUSE_INIT_PROJECT_ID=<any-id>
LANGFUSE_INIT_PROJECT_NAME=<any-name>
LANGFUSE_INIT_PROJECT_PUBLIC_KEY=<pick your own pk-lf-... string>
LANGFUSE_INIT_PROJECT_SECRET_KEY=<pick your own sk-lf-... string>
LANGFUSE_INIT_USER_EMAIL=<any-email>
LANGFUSE_INIT_USER_NAME=<any-name>
LANGFUSE_INIT_USER_PASSWORD=<any-password>
```

**Confirmed by direct PoC**: these init vars really do skip the manual signup/project-creation flow — the API key pair worked immediately on first boot with `docker compose up -d`.

**Not zero-code, unlike LangSmith — confirmed by direct inspection of the installed `langfuse` 3.7.0 SDK**: Langfuse's LangChain integration requires an explicit `CallbackHandler` (`from langfuse.langchain import CallbackHandler`) passed into every `.invoke()`/`.stream()` call's `config={"callbacks": [handler]}` — env vars (`LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY`/`LANGFUSE_HOST`) only authenticate the client, they do not auto-instrument LangChain calls the way LangSmith's `LANGSMITH_TRACING=true` does. Also note: `langfuse.langchain.CallbackHandler` requires the full `langchain` package installed (not just `langchain-core`) — confirmed by a real `ModuleNotFoundError` hit during the PoC when only `langchain-core` was installed.

**Real bug found and fixed during the PoC — the first attempt at custom trace IDs silently failed**: passing a custom trace ID via `config={"metadata": {"langfuse_trace_id": trace_id}}` does **nothing** — that metadata key doesn't exist in the `CallbackHandler`'s source (confirmed by reading `CallbackHandler.py` directly; it only recognizes `langfuse_session_id`, `langfuse_user_id`, and `langfuse_tags`). The graph still traced successfully, but under a random Langfuse-generated trace ID, not the intended one — a silent failure, not an error, so it would be easy to ship this without noticing. **Correct, verified-working pattern**: wrap the invocation in `langfuse.get_client().start_as_current_span(name=..., trace_context={"trace_id": trace_id})`, with the `CallbackHandler` still passed via `config={"callbacks": [handler]}` inside that `with` block:

```python
from langfuse import Langfuse, get_client
from langfuse.langchain import CallbackHandler

run_id = "..."  # this project's own run_id
trace_id = Langfuse.create_trace_id(seed=run_id)  # deterministic — same run_id always maps to the same trace_id

lf = get_client()
handler = CallbackHandler()
with lf.start_as_current_span(name="agent-run", trace_context={"trace_id": trace_id}):
    result = compiled_graph.invoke(initial_state, config={"callbacks": [handler]})

lf.flush()  # see below — required in a short-lived process
```

**Confirmed working end-to-end**: queried this exact `trace_id` back via the API afterward and got the real observations, including `input`/`output` fields showing the actual graph state — see the query pattern below.

**Async-flush concern — same category of issue LangSmith had, confirmed present in Langfuse too**: Langfuse batches and sends trace data asynchronously. `Langfuse.flush()` (or `get_client().flush()`) is a real, current method — call it at the end of `run_agent`, after `mark_assertion` fires, before the function returns. Skipping this risks losing the trace if the background task's process considers itself done before the async send completes (untested how large this risk actually is at 3.13/BackgroundTasks scale, but the mechanism is real and the fix is cheap — always flush).

### Querying a trace back by this project's `run_id` (relevant to 3b, verified here since it's the same PoC)

Langfuse's default deployment (the current `docker-compose.yml`, "v4 events_only mode") does **not** support the older `GET /api/public/traces/{id}` or `GET /api/public/observations` endpoints — confirmed by hitting both directly and getting an explicit "not available in v4 events_only mode" error. The current, correct endpoint is **`GET /api/public/v2/observations`**, filtered by `traceId`:

```bash
curl -u "$LANGFUSE_PUBLIC_KEY:$LANGFUSE_SECRET_KEY" \
  "http://localhost:3000/api/public/v2/observations?traceId=<trace_id>&fields=core,basic,io,metadata"
```

This returns one row **per observation** (not one combined trace object) — group by `traceId` in code if the full step-by-step trace needs reconstructing, per Langfuse's own docs. **Confirmed by direct query**: this returned the real `LangGraph`/`agent`-named observations with correct `input`/`output` JSON for the PoC run above. Since `trace_id` is generated deterministically from this project's own `run_id` (via `Langfuse.create_trace_id(seed=run_id)`), the same `run_id` always maps to the same `trace_id` — no separate mapping table needed.

### Agent DB — response persistence

Matches the diagram's `agent_response_schema`. Using SQL from the start (SQLite is fine for Phase 3; matches the diagram's own reasoning — "SQL DB because we are sure of the schema"):

```sql
CREATE TABLE agent_response (
    run_id TEXT PRIMARY KEY,
    user_id TEXT,                -- NULL until /auth exists (Phase 6)
    status TEXT NOT NULL,        -- queued | running | passed | failed | error
    user_question TEXT NOT NULL,
    agent_response TEXT          -- final result/verdict, NULL until the run completes
);
```

`run_agent` writes `status` transitions itself (`queued` → `running` → terminal) and fills `agent_response` once `mark_assertion` fires. No separate `run_events` table — the detailed step-by-step trace lives in Langfuse, not duplicated into SQL.

### 3a exit criteria — real E2E agent-driven UI run, not just "the pieces don't crash"

3a is not done when the code runs without errors. It's done when a **full agent-driven test against the real UI produces the expected input and output**, observed directly:

- `POST /test-submit` with a real instruction (e.g. "log in with valid credentials and confirm success") returns a `run_id` immediately; the agent run happens after the response is sent.
- The agent's per-run test environment spins up in isolation (per the verified pattern above), Playwright actually drives the real `spree_starter` login page (not a mock), and the agent reaches a genuine `mark_assertion` verdict based on what it actually observed on the page — confirmed by querying Langfuse (`GET /api/public/v2/observations?traceId=...`) and seeing real Thought → Action → Observation steps referencing the actual page content, not a hallucinated or stubbed result.
- Run this against **both** a valid-credentials scenario (expect PASS) and a deliberately invalid-credentials scenario (expect FAIL, agent correctly identifies the error) — confirm the agent produces the *correct* verdict for each, not just *a* verdict.
- The completed run's row in `agent_response` has `status` and `agent_response` populated correctly and matches what was actually observed.
- The full trace is visible and coherent in the Langfuse UI (`http://localhost:3000`) for that run.
- Per-run container teardown happens after both a passing and a failing run — confirmed no leaked containers after several runs.

Do not move to 3b until this bar is met with real, observed runs — not assumed from unit-level pieces working in isolation.

## 3b. GET endpoints (read-only, no new write paths)

```python
@app.get("/test-response")
async def get_response(run_id: str):
    return await db.get_response(run_id)   # reads agent_response table

@app.get("/test-status")
async def get_status(run_id: str):
    return await db.get_status(run_id)     # reads agent_response.status

@app.get("/test-logs")
async def get_logs(run_id: str):
    trace_id = Langfuse.create_trace_id(seed=run_id)  # same deterministic derivation used in 3a
    return await query_langfuse_observations(trace_id)  # GET /api/public/v2/observations?traceId=...
```

`GET /test-logs` reads from Langfuse, not SQL — use the `GET /api/public/v2/observations?traceId=...` endpoint (see 3a's verified pattern above; `read_run`/`list_runs`-style REST calls don't exist on Langfuse's current v4 deployment). Since `trace_id` is deterministically derived from `run_id` via `Langfuse.create_trace_id(seed=run_id)`, no separate mapping table is needed — just re-derive the same `trace_id` from `run_id` at read time. Group the returned observation rows by their shared `traceId` if reconstructing a single ordered trace matters for the response shape.

### 3b exit criteria

- All three GET endpoints return correct data for a run that 3a already completed and verified.
- `GET /test-logs` successfully retrieves the matching Langfuse trace using this project's `run_id` (via the deterministic `run_id` → `trace_id` derivation).

## 3c. Remaining polish (still `BackgroundTasks`, no Celery/auth)

Whatever hardening 3a/3b's real runs surface as necessary — e.g. better error messages on agent failure, handling a run that times out, cleanup of orphaned containers from a crashed process. Scope this once 3a/3b are actually running for real; don't pre-design it now. **Still explicitly not the place for Celery, Redis, or auth** — those are Phase 6, gated on Phase 5 also being complete.

## Legacy note: arq (superseded, kept for history only)

An earlier version of this doc recommended `arq` as the upgrade path off `BackgroundTasks`. Superseded by the Phase 6 Celery+Redis decision — see `phase-6-celery-auth.md`. Do not implement arq for this project.

## Verification status

| Claim | Status |
| --- | --- |
| Langfuse self-hosted `docker-compose.yml` boots all 6 services healthy | **Confirmed by direct boot** — Postgres, ClickHouse, Redis, MinIO, `langfuse-web`, `langfuse-worker` all came up healthy, web UI returned 200 |
| `LANGFUSE_INIT_*` env vars auto-provision a project/API-key pair, skipping manual UI signup | **Confirmed by direct boot** — the pre-set key pair authenticated successfully on first run |
| Langfuse's LangChain integration requires an explicit `CallbackHandler` in `config={"callbacks": [...]}` — NOT a zero-code env-var switch like LangSmith | **Confirmed by direct inspection and successful run** — env vars alone did not auto-instrument; the handler had to be passed explicitly |
| `langfuse.langchain.CallbackHandler` requires the full `langchain` package, not just `langchain-core` | **Confirmed by direct failure** — hit a real `ModuleNotFoundError` with only `langchain-core` installed |
| Passing a custom trace ID via `config={"metadata": {"langfuse_trace_id": ...}}` works | **Disproven by direct test** — this metadata key doesn't exist in the `CallbackHandler` source; the trace still sent, but under a random trace ID, not the intended one — a silent failure |
| `Langfuse.create_trace_id(seed=...)` + `get_client().start_as_current_span(trace_context={"trace_id": ...})` is the correct way to set a custom trace ID | **Confirmed by direct successful test** — queried the exact intended `trace_id` back afterward and got the real trace |
| `Langfuse.flush()` / `get_client().flush()` are real, current methods resolving the same async-flush concern LangSmith had | **Confirmed by direct inspection and successful call** during the PoC |
| Old `GET /api/public/traces/{id}` and `GET /api/public/observations` endpoints work on the current default deployment | **Disproven by direct test** — both returned an explicit "not available on deployments running in Langfuse v4 events_only mode" error |
| `GET /api/public/v2/observations?traceId=...` is the current correct query endpoint | **Confirmed by direct successful query** — returned real observation rows with correct `input`/`output` for the PoC run |
| Two concurrent, isolated per-run Compose stacks (the target-app/Mailpit environment, `docker-compose.yml` at repo root) work with no cross-contamination | **Confirmed by direct smoke test against the old dev-mode clone setup** — booted two real stacks, triggered email on one, confirmed the other's inbox stayed empty throughout. **Not yet re-run against the current image-based setup** (`ghcr.io/spree/spree:5.6.1`) — the mechanism should carry over unchanged, but hasn't been re-verified directly. |
| Ad hoc shell-exported ports across separate `docker compose` calls silently revert to defaults, causing collisions | **Confirmed by direct failure and fix** — hit this exact bug during the smoke test; fixed via consistent `--env-file` usage |

### Not verifiable from docs — confirm during 3a implementation

- Whether SQLite's default file-locking behavior causes contention once `run_agent` (writing) and 3b's GET endpoints (reading) run concurrently under real load.
- Real-world flush-timing risk inside an actual FastAPI `BackgroundTasks` context at production scale (the PoC ran the trace+flush in a standalone script, not literally inside a `BackgroundTasks` function) — the mechanism (`flush()` blocks until sent) is confirmed; the exact risk window inside a real background task hasn't been separately measured.
- Langfuse self-hosted resource footprint (CPU/RAM for 6 containers) under real concurrent load, versus the light single-run PoC tested here.
- Dynamic port allocation strategy for concurrent runs at scale (the smoke test used two hardcoded port sets; a real implementation needs an actual allocation scheme — e.g. a free-port scan or a reserved range divided by max concurrency — not decided yet).
