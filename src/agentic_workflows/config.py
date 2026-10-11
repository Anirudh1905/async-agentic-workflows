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

    kafka_bootstrap_servers: str = "127.0.0.1:9092"
    run_finished_topic: str = "RUN_FINISHED"
    run_finished_dlq_topic: str = "RUN_FINISHED.DLQ"

    database_url: str = "postgresql://postgres:postgres@127.0.0.1:5433/agentic"
    # If set, the notification consumer POSTs each RUN_FINISHED event here; otherwise it only logs.
    notify_webhook_url: str | None = None

    # Signs login tokens. Only the API needs it, so it's optional here and the API
    # refuses to start without it. Generate one with: openssl rand -hex 32
    jwt_secret: str | None = None
    jwt_ttl_minutes: int = 30


@lru_cache
def get_settings() -> Settings:
    return Settings()
