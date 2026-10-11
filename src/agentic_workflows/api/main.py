"""ASGI entry point: uvicorn agentic_workflows.api.main:app"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter

from agentic_workflows import db
from agentic_workflows.api import auth, runs
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
    log.info("api ready temporal=%s namespace=%s task_queue=%s", s.temporal_address, s.temporal_namespace, s.temporal_task_queue)
    try:
        yield
    finally:
        await app.state.db.close()
        log.info("api stopped")


app = FastAPI(title="Async agentic workflows", lifespan=lifespan)
app.include_router(auth.router)
app.include_router(runs.router)


@app.get("/healthz")
async def healthz():
    return {"ok": True}
