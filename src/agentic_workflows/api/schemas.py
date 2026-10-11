from datetime import datetime

from pydantic import BaseModel, EmailStr, Field


class CreateRun(BaseModel):
    prompt: str = Field(min_length=1, max_length=4000)


class RunCreated(BaseModel):
    run_id: str


class RunStatus(BaseModel):
    run_id: str
    status: str  # Temporal execution status: running, completed, failed, ...
    step: str | None = None  # workflow-level progress from the status query, while running
    answer: str | None = None
    error: str | None = None


class RunSummary(BaseModel):
    """A finished run, as recorded in Postgres by the persistence consumer."""

    run_id: str
    status: str
    prompt: str
    answer: str | None
    error: str | None
    started_at: datetime
    finished_at: datetime


class RegisterUser(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)


class User(BaseModel):
    id: str
    email: str


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int  # seconds
