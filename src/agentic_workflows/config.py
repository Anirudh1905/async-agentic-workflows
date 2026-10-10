from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Optional so processes that never call the LLM (the API) start without it;
    # the worker fails on the first LLM call if it's missing.
    openai_api_key: str | None = None
    openai_base_url: str | None = None
    llm_model: str = "gpt-4o-mini"
    agent_max_steps: int = 10

    temporal_address: str = "127.0.0.1:7233"
    temporal_namespace: str = "default"
    temporal_task_queue: str = "agent-runs"


@lru_cache
def get_settings() -> Settings:
    return Settings()
