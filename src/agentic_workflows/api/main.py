"""ASGI entry point: uvicorn agentic_workflows.api.main:app"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from temporalio.client import Client

from agentic_workflows.api import runs
from agentic_workflows.config import get_settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    app.state.temporal_client = await Client.connect(s.temporal_address, namespace=s.temporal_namespace)
    yield


app = FastAPI(title="Async agentic workflows", lifespan=lifespan)
app.include_router(runs.router)


@app.get("/healthz")
async def healthz():
    return {"ok": True}
