# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A LangGraph agent run as durable Temporal workflows. A FastAPI app (`/auth` with JWT login, `/runs`) has Redis rate limits and a per-user active-run cap in front of it. Each run publishes a `RUN_FINISHED` event to Kafka, and two consumers react to it: one writes Postgres, one POSTs a webhook. The project was built in phases, and `docs/phases/phase-N-*.md` is the design doc for each. Python 3.13, managed with `uv`.

## Commands

```bash
uv sync                       # deps (add --group ui for the Streamlit playground)
make                          # list Makefile targets; each wraps one of the commands below
make up && make smoke         # compose stack: Postgres + pgweb, Temporal + UI, Kafka + UI, Redis, webhook-tester
tilt up                       # (make dev) stack + api + worker + both consumers, dashboard :10350 (restarts on src/ edits)
make psql                     # SQL shell on the compose Postgres (agentic db)
make redis-cli                # Redis shell (rate-limit buckets, active-run sets)

uv run worker                                            # Temporal worker (workflow + agent activities)
uv run uvicorn agentic_workflows.api.main:app --port 8000
uv run consumer persistence                              # RUN_FINISHED → Postgres runs
uv run consumer notifications                            # RUN_FINISHED → webhook
uv run agent "<prompt>"                                  # agent directly, no Temporal
uv run python scripts/send_test_run.py "<prompt>"        # log in as dev@example.com, start a run
uv run --group ui streamlit run ui/app.py                # manual test chat

uv run pytest                                            # all tests
uv run pytest tests/test_workflow.py::test_agent_stops_at_max_steps -q   # one test
```

There's no linter or formatter configured. `.env` (copied from `.env.example`) needs `OPENAI_API_KEY` (plus `OPENAI_BASE_URL` for a gateway) and a `JWT_SECRET` of at least 32 characters.

**How the tests run.** No test calls an LLM or the network:
- **Temporal tests** use the time-skipping test server; the first run downloads it.
- **Integration tests** hit the compose Postgres, Kafka and Redis, and skip themselves when those aren't reachable, so start the stack (`make up`) for full coverage.
- **API tests** use `httpx.ASGITransport`, which does **not** run the app's lifespan. So no Temporal client, DB pool, rate limiter or active-run limiter exists there. Tests inject what they need through `app.dependency_overrides` on the `get_*` functions in `api/deps.py` and `api/routes/auth.get_current_user`. The `get_limiter` and `get_active_runs` dependencies return `None` when nothing is configured, which disables them.

## Architecture: the parts that span files

**Request path.**
1. `POST /runs` (JWT) checks the token bucket, then the active-run cap.
2. It starts `AgentRunWorkflow` with `id = run_id` and `memo={"user_id": ...}`, and returns `202`.
3. The worker runs the LangGraph loop inside the workflow.
4. It publishes `RUN_FINISHED` to Kafka, keyed by `run_id`.
5. The consumers upsert `runs` in Postgres and notify.

**The agent's steps are Temporal activities.** See `workflows/agent_run.py` and `agent/graph.py`. `temporalio.contrib.langgraph.LangGraphPlugin` turns each graph node marked `execute_in: "activity"` into an activity named `agent.<node>`. Its timeout and retry policy come from the node's metadata. This has consequences:
- **Node functions must be module-level** (the plugin registers them by import path). They get the model from `get_llm()`; tests swap it with `set_llm()`.
- **The router (`route`) must be `async`.** LangGraph runs sync routers in a thread, which the workflow sandbox forbids.
- **Messages cross the activity boundary as dicts,** so every node calls `convert_to_messages`.
- **Both the worker and the API client use `pydantic_data_converter`,** and so does `WorkflowEnvironment` in tests. They must match.
- **`build_state_graph()` returns a fresh graph each call,** because the plugin mutates the one it's given. The worker and the direct paths (`build_graph()`, used by the CLI and Streamlit) each build their own.
- **Workflow code must stay deterministic.** That means LLM calls, tools and Kafka only inside activities, and time from `workflow.now()`. Anything replay must see consistently goes in `RunInput`, the way `max_steps` does.
- **Changing the workflow's sequence of commands breaks runs still in flight.** Use `workflow.patched()`. New `RunInput` and `RunFinished` fields need defaults, so older histories still decode.

**Ownership.** `GET /runs/{id}` reads the owner with `describe()` and then `await desc.memo_value("user_id")`, compares it with the caller, and returns **404, not 403**, for other users' runs. `GET /runs` reads the Postgres `runs` table (finished runs only).

**Events.** The code is in `events/producer.py` and `consumers/`.
- **One dataclass, `workflows/models.RunFinished`,** is the activity input, the JSON on the wire, and the consumers' parse target. There's no second schema.
- **A failed run still publishes,** with `status: failed`.
- **The consumers are at-least-once.** They commit manually, after success or after parking the message in the DLQ (`RUN_FINISHED.DLQ`, after 3 attempts). So handlers must be idempotent: persistence upserts on `run_id`, and notifications claim `event_id` in `notifications_sent` before sending and release the claim if sending fails.

**Postgres schema.** There's no migration tool. `db.SCHEMA` is idempotent, add-only DDL, run at startup by the API and the consumers under an advisory lock. Altering an existing table means it's time to introduce migrations.

**Limits.** They're in `api/limits/`: rate limits in `ratelimit.py` and `token_bucket.lua`, the active-run cap in `active_runs.py` and `active_runs.lua`. Routes are in `api/routes/`.
- **Token buckets:** checked by one atomic Lua script that reads Redis's `TIME`. Login and register are limited per client IP, run routes per user, with limit strings like `"5/min"` from settings.
- **The active-run cap:** a sorted set per user. Slots are never released when a run finishes. Instead, at the cap the API `describe()`s the held runs and drops the ones Temporal no longer reports as running.
- **Both fail open** when Redis is down.

**Logging.** The setup is in `logs.py`.
- **Setup:** every entry point calls `setup_logging("<service>")` once.
- **Inside activities,** use `step_logger(logger)`, which prefixes `[run_id=… agent.tools attempt=N]`. In workflow code, use the replay-safe `workflow.logger`.
- **Format:** log `key=value` pairs.
- **Never log** passwords, tokens or failed-login emails, and log prompts and answers by size only. `tests/test_logs.py` enforces part of this.

## Infra gotchas

- **Host ports are non-default** so they don't clash locally:
  - Postgres `5433`, Redis `6380`, Temporal `7233`
  - Temporal UI `8233`, Kafka `9092`, Kafka UI `8081`, webhook-tester `8082`, pgweb (Postgres UI) `8083`
  - API `8000`

  Everything binds to `127.0.0.1`. Kafka advertises `127.0.0.1`, not `localhost`.
- **Temporal 1.32 (`temporalio/server`) has no built-in config.** `infra/temporal/config.yaml` is vendored from the matching server release; update it whenever the image tag changes.
  - **The one-shot `temporal-schema` service** creates and migrates the schema.
  - **The one-shot `temporal-init` service** registers the `default` namespace.
  - **The worker, API and UI** wait for `temporal-init` to finish.
- **The Tiltfile pins `project_name`.** Tilt ignores the compose file's `name:` and would otherwise start a second stack with the same ports.
- **The notification webhook:** `.env.example` sets `NOTIFY_WEBHOOK_URL` to the local inbox `http://127.0.0.1:8082/a9e7c0de-0000-4000-8000-000000000001`, whose UI is at `/s/<same uuid>`. The code default is unset, which means the consumer only logs.

## Docs

- **Phase docs:** each one has Mermaid diagrams. Keep them, the README's phase table and `.env.example` in sync with code changes.
- **The architecture diagram is a single archify diagram** in `docs/architecture/`:
  - `architecture.json` is the source
  - `architecture.html` is the render
  - `architecture.png`, used in the README, is exported from the viewer's Export → PNG

  Update the diagram in place, never as extra copies. `.archify/` is scratch output and is gitignored.
