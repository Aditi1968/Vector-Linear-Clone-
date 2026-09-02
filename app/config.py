from functools import lru_cache
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


Environment = Literal["development", "test", "production"]


class Settings(BaseSettings):
    """Runtime configuration, read from the process environment or .env.

    `environment` deliberately has no default. Defaulting it to
    "development" meant a deployment that merely forgot to set ENVIRONMENT
    got development behaviour -- GraphiQL served, introspection answered --
    and got it silently, because nothing was missing as far as the process
    could tell. That is the least safe value chosen by omission.

    Required instead, so the failure mode inverts: a deployment that forgets
    it cannot start, which is loud and fixable, rather than starting with
    every environment-gated protection quietly switched off at once.
    """

    database_url: SecretStr
    environment: Environment

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
