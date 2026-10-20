"""Runtime settings, read from ``PIPELINEOS_*`` environment variables (or a local ``.env``).

Nothing here has a default that could point a write at a real portal: the token and the
allowed portal id are both unset by default, so a fresh checkout can only run against the
in-memory fake.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PIPELINEOS_", env_file=".env", extra="ignore")

    hubspot_token: SecretStr | None = None
    hubspot_base_url: str = "https://api.hubapi.com"
    # HubSpot's date-versioned API release. Every path is built from this one value.
    hubspot_api_version: str = "2026-09"
    allowed_portal: int | None = None

    ollama_url: str | None = None
    ollama_model: str = "llama3.1:8b"


@lru_cache
def get_settings() -> Settings:
    return Settings()
