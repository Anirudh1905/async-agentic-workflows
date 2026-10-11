"""ASGI entry point: uvicorn agentic_workflows.api.main:app"""

import logging
from contextlib import asynccontextmanager

import redis.asyncio as redis
from fastapi import FastAPI
from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter

from agentic_workflows import db
from agentic_workflows.api.routes import auth, runs
from agentic_workflows.api.limits.active_runs import ActiveRunLimiter
from agentic_workflows.api.limits.ratelimit import Limit, RateLimiter
from agentic_workflows.config import get_settings
from agentic_workflows.logs import setup_logging

setup_logging("api")
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    # HS256 keys under 32 bytes are brute-forceable (RFC 7518 §3.2).
    if not s.jwt_secret or len(s.jwt_secret) < 32:
        raise RuntimeError("JWT_SECRET must be set to at least 32 characters; generate one with `openssl rand -hex 32`")
    # Same converter as the worker, so both sides encode workflow payloads the same way.
    app.state.temporal_client = await Client.connect(
        s.temporal_address, namespace=s.temporal_namespace, data_converter=pydantic_data_converter
    )
    app.state.db = await db.connect(s.database_url)
    redis_client = None
    if s.rate_limit_enabled:
        # Short timeouts: a slow Redis must not slow every request (we fail open instead).
        redis_client = redis.from_url(s.redis_url, socket_timeout=0.25, socket_connect_timeout=0.25)
        limits = {
            "auth": Limit.parse(s.rate_limit_auth),
            "runs_create": Limit.parse(s.rate_limit_runs_create),
            "runs_read": Limit.parse(s.rate_limit_runs_read),
        }
        app.state.limiter = RateLimiter(redis_client, limits)
        log.info("rate limiting on redis=%s %s", s.redis_url, {k: f"{v.capacity} burst, {v.per_second * 60:g}/min" for k, v in limits.items()})
        if s.max_active_runs_per_user > 0:
            app.state.active_runs = ActiveRunLimiter(redis_client, app.state.temporal_client, s.max_active_runs_per_user)
            log.info("active runs capped at %d per user", s.max_active_runs_per_user)
    else:
        log.warning("rate limiting is OFF (RATE_LIMIT_ENABLED=false)")
    log.info("api ready temporal=%s namespace=%s task_queue=%s", s.temporal_address, s.temporal_namespace, s.temporal_task_queue)
    try:
        yield
    finally:
        await app.state.db.close()
        if redis_client is not None:
            await redis_client.aclose()
        log.info("api stopped")


app = FastAPI(title="Async agentic workflows", lifespan=lifespan)
app.include_router(auth.router)
app.include_router(runs.router)


@app.get("/healthz")
async def healthz():
    return {"ok": True}
