from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    openai_api_key: str
    openai_base_url: str | None = None
    llm_model: str = "gpt-4o-mini"
    agent_max_steps: int = 10


@lru_cache
def get_settings() -> Settings:
    return Settings()
