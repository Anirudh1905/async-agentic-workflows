# End-Goal Architecture (Target Design)

This captures the full target architecture as specified in the end-goal diagram (2026-08-31/09-01). **This is not what gets built first.** It's the destination — see `phase-3-backend-orchestration.md` for the staged, `BackgroundTasks`-based path through Phase 5, and `phase-6-celery-auth.md` for the actual swap to this doc's Celery+Redis+auth design. Treat this doc as the spec Phase 6 builds toward, once Phases 1–5 all work end-to-end together.

## 1. Request flow

```text
User
 ├─ POST /test-submit  ──────────────▶ Redis Queue ──▶ Celery Worker
 ├─ POST /auth         ──────────────▶ Authentication ──▶ (issues/validates credentials, gates the GETs below)
 ├─ GET /test-response ─(run_id)─────▶ SQL DB
 ├─ GET /test-status   ─(run_id)─────▶ SQL DB
 └─ GET /test-logs     ─(run_id)─────▶ SQL DB
```

All APIs are FastAPI, containerized, deployed on k8s, and rate-limited (token bucket algorithm, backed by Redis).

## 2. Celery Worker internals

Each queued job spins up:

- **Agent Run**: a LangGraph agent ("Langraph Agent" in the diagram) driving a set of UI tools (Playwright actions — click/type/observe, per `phase-2-agent-core.md`).
- **Test Environment per run**: an *isolated* set of containers spun up fresh for that specific run — a dummy test-application and a dummy Gmail twin (this generalizes the Mailpit email-twin pattern from `phase-5-digital-twins.md` into a per-run, ephemeral environment rather than one long-lived shared Compose stack). Diagram note: *"Whenever an agent run spins up, it spins up an isolated test environment container for the testing purpose."*

Two outputs per run:

- **Agent Response** → written to the **SQL DB**.
- **Agent thinking logs** (the ReAct trace — thought/action/observation) → ingested into **Langfuse** (self-hosted, per the Phase 3 decision — see `phase-3-backend-orchestration.md`), not the SQL DB. The SQL DB holds the structured final result; Langfuse holds the detailed execution trace.

## 3. Why Redis (queue) over SQS

Per the diagram's own annotation:

- Redis is faster — lower latency is a requirement for agent response turnaround.
- Binds well with Celery (Celery's Redis broker support is first-class).
- Trade-off acknowledged: Redis is costlier than SQS and has no built-in dead-letter queue (DLQ) — accepted despite this, in favor of latency.

## 4. Why Celery workers (not just BackgroundTasks, long-term)

Per the diagram's own annotation, three reasons:

- Instant application responsiveness (the API returns immediately; the worker does the slow part).
- Horizontal scalability (add more Celery workers independently of the API process).
- Fault tolerance & resilience (jobs survive API restarts, can retry).

This supersedes an earlier draft's arq-as-upgrade-path suggestion — the diagram formalizes Celery specifically as the destination. See `phase-6-celery-auth.md` for the actual migration plan (Phase 6, gated on Phases 1–5 working end-to-end); `phase-3-backend-orchestration.md` covers the `BackgroundTasks`-only work that precedes it.

## 5. Data model

**`agent_response_schema`** (SQL DB):

| Field | Notes |
| --- | --- |
| `run_id` | primary lookup key for all three GET endpoints |
| `user_id` | ties a run to an authenticated user (added once `/auth` exists) |
| `status` | e.g. queued/running/passed/failed/error |
| `user_question` | the natural-language test instruction submitted |
| `agent_response` | the final result/verdict (mark_assertion's output) |

**`auth_schema`** (SQL DB):

| Field | Notes |
| --- | --- |
| `userid` | |
| `username` | |
| `email` | |
| `pwd` | store hashed, never plaintext — the diagram names the field but doesn't specify hashing; treat this as an implicit requirement, not optional |

**Why SQL, not NoSQL** — per the diagram's own annotation: *"SQL DB because we are sure of the schema."* Both schemas above are fixed and well-defined upfront, which is the classic case for a relational DB over a schemaless store.

## 6. Observability

- **Traces** (agent thinking/reasoning): Langfuse (self-hosted — open-source choice made in Phase 3, superseding the diagram's original LangSmith label).
- **Metrics**: Prometheus (metric server).
- **Logs**: Promtail (DaemonSet) exporting container logs to Loki.
- **Dashboards**: Grafana.

**Explicitly rejected**: Kafka + ELK — per the diagram's annotation, *"Discarding Kafka+ELK because we want to reduce ingest latency."* Promtail→Loki was chosen specifically over the ELK-style pipeline for lower ingest latency.

## 7. API surface, deployment, rate limiting

- All APIs are FastAPI, containerized, deployed on k8s.
- Rate limiting: token bucket algorithm, backed by Redis.

## 8. Relationship to already-built phase docs

- `phase-1-infrastructure.md` / `phase-5-digital-twins.md`: those docs verified a **fixed, shared** dev environment (one Compose stack, long-lived). **Resolved, 2026-09-01**: Phase 3 (3a) implements the end-goal's ephemeral **per-run** environment on top of that foundation — confirmed working by direct smoke test (two concurrent, fully isolated `spree_starter`+Mailpit stacks, each with its own Compose project name and `--env-file`-scoped ports, zero cross-contamination). Phase 3 uses `BackgroundTasks` to drive this, not yet the Celery worker shown here — that swap is Phase 6.
- `phase-2-agent-core.md`: the LangGraph agent and its tool set (Eyes/Hands/Judge) carry over unchanged into the Celery Worker's "Agent Run" box — no redesign needed there, just a change in what invokes it (a Celery task in Phase 6, vs. a FastAPI `BackgroundTasks` function in Phase 3).
- `phase-3-backend-orchestration.md`: implements this doc's request-flow and per-run-environment design using `BackgroundTasks`, not Celery — the queue/worker swap to match this doc's "Redis Queue → Celery Worker" box exactly is `phase-6-celery-auth.md`, gated on Phases 1–5 all working end-to-end first.
- `phase-6-celery-auth.md`: the doc that actually builds the Celery+Redis+auth+k8s pieces shown in this design.

## Verification status

This doc transcribes the pasted diagram's content and annotations directly — it is a specification of *intent*, not a set of externally-verifiable technical claims like the earlier phase docs. Where it references already-verified facts (Celery+Redis pairing, rate-limiting patterns), those are standard, well-documented technology combinations and were not independently re-verified here; flag if any specific claim in this doc needs the same PoC-level scrutiny applied to Phases 1/5 before implementation.
