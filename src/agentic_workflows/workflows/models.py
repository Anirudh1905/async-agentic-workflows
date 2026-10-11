"""Payloads that cross the Temporal boundary. Plain dataclasses so the default
JSON data converter handles them and they stay importable inside the workflow sandbox."""

from dataclasses import dataclass


@dataclass
class RunInput:
    run_id: str
    prompt: str
    user_id: str | None = None  # who started the run; defaulted so pre-phase-5 histories still decode


@dataclass
class AgentInput:
    prompt: str


@dataclass
class RunResult:
    run_id: str
    answer: str


@dataclass
class RunFinished:
    """The RUN_FINISHED event. Built in the workflow, published by an activity,
    parsed by the consumers. Timestamps are ISO-8601 strings to keep the JSON plain."""

    event_id: str  # deterministic (one event per run) so consumers can dedupe
    run_id: str
    status: str  # "completed" | "failed"
    prompt: str
    answer: str | None
    error: str | None
    started_at: str
    finished_at: str
    user_id: str | None = None  # who started the run; None for runs from before login existed
