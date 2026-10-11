"""Dependencies for the routes. Tests swap these via app.dependency_overrides."""

import asyncpg
from fastapi import Request
from temporalio.client import Client

from agentic_workflows.config import get_settings


def get_temporal_client(request: Request) -> Client:
    return request.app.state.temporal_client


def get_db(request: Request) -> asyncpg.Pool:
    return request.app.state.db


def get_limiter(request: Request):
    """The RateLimiter, or None when rate limiting is off (or the app wasn't
    started through its lifespan, as in unit tests)."""
    return getattr(request.app.state, "limiter", None)


def get_active_runs(request: Request):
    """The ActiveRunLimiter, or None when the cap is off (or, as above, in unit tests)."""
    return getattr(request.app.state, "active_runs", None)


def get_task_queue() -> str:
    return get_settings().temporal_task_queue
