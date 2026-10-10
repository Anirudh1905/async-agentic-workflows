"""Dependencies for the routes. Tests swap these via app.dependency_overrides."""

from fastapi import Request
from temporalio.client import Client

from agentic_workflows.config import get_settings


def get_temporal_client(request: Request) -> Client:
    return request.app.state.temporal_client


def get_task_queue() -> str:
    return get_settings().temporal_task_queue
