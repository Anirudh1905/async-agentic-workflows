"""ASGI entry point: uvicorn agentic_workflows.api.main:app"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from temporalio.client import Client

from agentic_workflows import db
from agentic_workflows.api import auth, runs
from agentic_workflows.config import get_settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    # HS256 keys under 32 bytes are brute-forceable (RFC 7518 §3.2).
    if not s.jwt_secret or len(s.jwt_secret) < 32:
        raise RuntimeError("JWT_SECRET must be set to at least 32 characters; generate one with `openssl rand -hex 32`")
    app.state.temporal_client = await Client.connect(s.temporal_address, namespace=s.temporal_namespace)
    app.state.db = await db.connect(s.database_url)
    try:
        yield
    finally:
        await app.state.db.close()


app = FastAPI(title="Async agentic workflows", lifespan=lifespan)
app.include_router(auth.router)
app.include_router(runs.router)


@app.get("/healthz")
async def healthz():
    return {"ok": True}
