"""
App settings, pulled from environment variables (or a .env file locally).
Nothing fancy - just one place so the DB URL isn't hardcoded anywhere.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg2://postgres:postgres@localhost:5433/ins_claims"


def get_settings() -> Settings:
    # not caching this on purpose - keeps tests free to monkeypatch env vars
    # between calls without worrying about a stale cached instance
    return Settings()
