from functools import lru_cache
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


Environment = Literal["development", "test", "production"]


class Settings(BaseSettings):
    database_url: SecretStr
    environment: Environment = "development"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="forbid",
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Resolve settings on first use, not at import time.

    A module-level `Settings()` made importing anything that transitively
    reached this module require DATABASE_URL -- which meant unit tests,
    linters and `--help` in a container all failed for want of a database
    they never touch.

    Configuration is still mandatory, just demanded later and by the code
    that actually needs it. The composition root (app.main) resolves it
    eagerly and so still fails fast on a misconfigured deployment; library
    modules take the environment as an argument instead.

    Cached because settings are immutable for a process lifetime; call
    `get_settings.cache_clear()` to force a re-read.
    """
    return Settings()
