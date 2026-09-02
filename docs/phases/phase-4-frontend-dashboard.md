# Phase 4 — Frontend Dashboard (Streamlit)

Goal: a Streamlit page where a user types a natural-language test instruction, submits it, and watches the run's status/trace update live by polling Phase 3's FastAPI endpoints.

**Dependency note (2026-09-01, corrected):** this phase needs Phase 3's **3b** sub-stage complete (the `GET` endpoints) — it can't be built against 3a alone, since 3a has no way to read anything back. Overall build order: Phase 1 → Phase 2 → Phase 3 (3a → 3b → 3c, all `BackgroundTasks`-based, no Celery/auth anywhere in Phase 3) → **this phase (Phase 4)** → Phase 5 → Phase 6 (Celery + auth, gated on Phases 1–5 all working end-to-end — see `phase-6-celery-auth.md`).

## 1. Basic structure

Streamlit reruns the whole script top-to-bottom on every interaction, so run state must live in `st.session_state`, not local variables.

- A text input (or `st.text_area`) for the instruction, plus a submit button.
- On submit: `requests.post("http://localhost:8000/test-submit", json={"instruction": ...})`, store the returned `run_id` in `st.session_state`. (Endpoint path matches the end-goal diagram's naming — `/test-submit`, not the earlier placeholder `/api/tests`.)
- Once a `run_id` exists, poll `GET /test-status?run_id=...` and `GET /test-logs?run_id=...` on a timer, appending new events to a `st.session_state` list and re-rendering.

## 2. Polling pattern

Streamlit has no native "run this every N seconds" primitive outside of a full script rerun. Two workable approaches:

- **`st.fragment` with `run_every`** (current Streamlit versions support this) — wrap the polling+rendering block in a fragment that reruns on an interval independent of the rest of the page, avoiding a full-page rerun/flicker for just the log tail.
- **Simple rerun loop**: `time.sleep(1)` then `st.rerun()` inside a `while` guarded by the run's status not yet being terminal. Simpler, but reruns the entire script each tick — fine for a single-page dashboard at this scale.

Start with the simple rerun loop for Phase 4; only reach for `st.fragment` if the full-page rerun visibly flickers or feels slow once running against a real multi-step agent trace.

## 3. Displaying the trace

**Changed 2026-09-01**: Phase 3 no longer keeps a local `run_events` table — the step-by-step thought/action/observation trace lives in self-hosted Langfuse (open-source, chosen over LangSmith), and `GET /test-logs` (Phase 3, 3b) fetches it from there via `GET /api/public/v2/observations?traceId=...`. Render whatever that endpoint returns (a list of trace steps/messages) — e.g. `st.chat_message` per step, or a simpler `st.write` per line if that's overkill. Show the final `status` and `agent_response` prominently once the run reaches a terminal state (`passed`/`failed`/`error`), reading those from `GET /test-response`/`GET /test-status`.

## 4. Phase 4 exit criteria

- Submitting an instruction shows `queued` immediately, then transitions through `running` to a terminal status without a manual page refresh.
- The trace view grows incrementally as the agent progresses — confirmed by watching a real multi-step run, not just checking the final state.
- Submitting a second instruction while the dashboard is open starts a fresh run cleanly (no stale state bleeding from the previous run).

## Verification status

This doc describes Streamlit's own polling/rerun primitives (`st.session_state`, `st.rerun`, `st.fragment`) as of current Streamlit versions (1.62.0, confirmed 3.13-compatible in Phase 1's verification pass) — these are stable, long-documented Streamlit APIs, not project-specific claims requiring the same level of fact-checking as the Docker images/LangGraph API surface in earlier phases. No corrections needed here from the verification pass.

### Not verifiable from docs — confirm at Phase 4 kickoff (runtime-only)

- Whether `st.fragment(run_every=...)` is actually needed, or whether the simple rerun-loop approach feels fine in practice once the real trace volume from a multi-step login/password-reset test run is visible on screen — a judgment call to make by trying it, not by reading docs.
