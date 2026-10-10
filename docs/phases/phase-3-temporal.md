# Phase 3: Temporal workflow and agent run API

**Goal:** agent runs become durable, async Temporal workflows started from an HTTP API.

```mermaid
sequenceDiagram
    autonumber
    actor C as Client
    participant API as Agent run API (FastAPI)
    participant T as Temporal server
    participant W as Worker
    participant A as run_agent_activity
    C->>API: POST /runs {prompt}
    API->>T: start AgentRunWorkflow(workflow_id=run_id)
    API-->>C: 202 {run_id}
    T->>W: workflow task (agent-runs queue)
    W->>A: execute activity (timeout + retry policy)
    A->>A: LangGraph agent (LLM ⇄ web_search)
    A-->>W: result
    W-->>T: workflow completed
    C->>API: GET /runs/{run_id}
    API->>T: describe / query
    API-->>C: {status, result}
```

## Design

- **Activity** `run_agent_activity(prompt) -> str` wraps Phase 1's `run_agent`. LLM calls aren't deterministic, so they must live in an activity, never in workflow code.
  - `start_to_close_timeout` plus a `RetryPolicy` for LLM and network failures
  - heartbeats for long runs
- **Workflow** `AgentRunWorkflow(run_id, prompt)`: runs the activity and returns the result. A query handler exposes the current status.
- **Worker** process (`uv run worker`) on the `agent-runs` task queue.
- **Agent run API** (FastAPI):
  - `POST /runs {prompt}` → starts the workflow with `workflow_id = run_id` and returns `202 {run_id}` right away
  - `GET /runs/{run_id}` → status and result, read via a workflow describe/query

## Done when

- [ ] `POST /runs` returns immediately and the run shows up in the Temporal UI
- [ ] Killing the worker mid-run and restarting it resumes the run
- [ ] Integration test using Temporal's time-skipping test environment
