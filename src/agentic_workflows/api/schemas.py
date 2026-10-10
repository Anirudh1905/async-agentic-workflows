from pydantic import BaseModel, Field


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
