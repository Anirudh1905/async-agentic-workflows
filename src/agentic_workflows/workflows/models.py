"""Payloads that cross the Temporal boundary. Plain dataclasses so the default
JSON data converter handles them and they stay importable inside the workflow sandbox."""

from dataclasses import dataclass


@dataclass
class RunInput:
    run_id: str
    prompt: str


@dataclass
class AgentInput:
    prompt: str


@dataclass
class RunResult:
    run_id: str
    answer: str
