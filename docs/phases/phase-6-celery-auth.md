# Phase 6 — Celery + Auth (production shape)

**Gate: do not start this phase until Phases 1–5 all work together, end-to-end, for real.** That means: `uv`-managed Python env (Phase 1), the LangGraph agent driving the real login/password-reset UI (Phase 2), the full `BackgroundTasks`-based submit→run→ingest→persist→read-back loop (Phase 3, 3a+3b+3c), the Streamlit dashboard showing live status/traces (Phase 4), and the agent asserting on a real Mailpit-captured email (Phase 5) — all observed working together, not just each phase's own isolated exit criteria. This phase is the swap from a working prototype to the production-shaped design in `end-goal-architecture.md`; it is not where the core loop gets proven for the first time.

## 1. What changes from Phase 3

Everything in Phase 3 (3a/3b/3c) used FastAPI `BackgroundTasks` — in-process, no persistence across restarts, no retries, no auth. Phase 6 replaces the orchestration layer, not the agent or the digital-twin logic:

- `POST /test-submit` pushes a job onto a **Redis-backed Celery queue** instead of calling `bg_tasks.add_task(...)`. A separate Celery worker process (its own container) consumes the queue and runs `run_agent` — the same function built in Phase 3, unchanged in its core logic.
- `POST /auth` and an `auth_schema` table are added. `/test-submit` and the three `GET` endpoints from Phase 3b become auth-gated.
- Token-bucket rate limiting (Redis-backed) wraps all endpoints.
- Deployment moves to k8s; observability adds Prometheus/Grafana + Promtail/Loki.

This is a genuine architecture swap at the orchestration layer, not a rewrite of the agent, the per-run test-environment logic, or the Langfuse/email-twin integration — those carry over unchanged from Phases 2/3/5.

## 2. Why Celery + Redis (per the end-goal diagram's own annotations)

**Celery over continuing with `BackgroundTasks`:**
- Instant application responsiveness — the API returns immediately regardless of load; the worker pool absorbs slow work.
- Horizontal scalability — add Celery worker processes independently of the FastAPI process.
- Fault tolerance & resilience — jobs survive API restarts (queued in Redis, not held in a dead process's memory), and can be retried.

**Redis over SQS as the queue backend:**
- Redis is faster — lower latency, and this project's own goal is fast agent-response turnaround.
- Binds well with Celery — first-class, well-documented broker support.
- Trade-off accepted deliberately: Redis costs more than SQS and has no built-in dead-letter queue. Accepted anyway, in favor of latency.

## 3. Implementation shape

- Add a `redis` service to Docker Compose (or the k8s manifests, if deploying there directly).
- A Celery `app` object with `broker=redis://...`; `run_agent` becomes a `@app.task` (or is wrapped by one) instead of a plain async function passed to `bg_tasks.add_task`.
- `POST /test-submit` calls `run_agent.delay(run_id, instruction)` (or `.apply_async(...)`) instead of `bg_tasks.add_task(run_agent, ...)`.
- A separate Celery worker process/container: `celery -A myapp worker --loglevel=info`.
- `auth_schema`:

```sql
CREATE TABLE auth (
    userid TEXT PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    email TEXT NOT NULL UNIQUE,
    pwd_hash TEXT NOT NULL   -- hashed, never plaintext — the diagram names the field "pwd" but
                             -- doesn't specify hashing; treat hashing as a non-negotiable requirement
);
```

- `POST /auth` validates credentials against `auth_schema` and issues whatever session/token mechanism is chosen (not specified in the end-goal diagram — a real decision to make here, not carried over from any earlier phase).
- Rate limiting: token bucket algorithm, Redis-backed, wrapping all endpoints — implementation detail (middleware vs. per-endpoint decorator) is open, to be decided during this phase.
- `agent_response.user_id` (nullable throughout Phase 3) gets populated for real once `/auth` exists — update the write path in `run_agent`/`test-submit` accordingly.

## 4. Deployment and observability (per `end-goal-architecture.md`)

- All APIs containerized, deployed on k8s.
- Metrics: Prometheus (metric server).
- Logs: Promtail (DaemonSet) exporting container logs to Loki.
- Dashboards: Grafana.
- Explicitly not Kafka+ELK — per the diagram's own annotation, that combination was rejected specifically to reduce ingest latency.

This is real, separate infrastructure work (k8s manifests, Prometheus/Grafana/Loki setup) that hasn't been scoped in detail yet — treat as its own set of sub-tasks once this phase actually starts, not something to design fully in advance here.

## 5. Phase 6 exit criteria

- `POST /auth` issues valid credentials that gate access to `/test-submit` and all three `GET` endpoints; requests without valid auth are rejected.
- `POST /test-submit` enqueues to Redis; a separate Celery worker process picks it up and runs the same agent flow verified in Phase 3 — confirmed by a real run producing a correct result, same bar as Phase 3a's exit criteria (valid + invalid credential scenarios, real UI, real Langfuse trace).
- Killing the FastAPI process mid-run does **not** lose the job — confirmed directly (this is the concrete behavior Celery+Redis is meant to fix versus `BackgroundTasks`, so prove it, don't assume it).
- Rate limiting actually rejects requests past the configured threshold — confirmed by exceeding it deliberately.
- Whatever k8s/monitoring setup is targeted for this phase is confirmed working against a real deployment, not just manifests that look correct.

## Verification status

This doc describes work not yet started. The technology choices (Celery+Redis over arq, Redis over SQS) are transcribed directly from the end-goal diagram's own stated rationale — not independently re-verified the way Phases 1/3/5's specific technical claims were, since this phase hasn't begun. Apply the same standard once implementation starts: verify Celery/Redis setup specifics, auth token mechanism, and k8s/monitoring configuration against real running infrastructure before treating any of this section as settled, the same way Phase 3's per-run isolation and Langfuse integration were verified by direct smoke test (including a real self-hosted boot and a real trace queried back by custom ID) rather than assumed from documentation.
