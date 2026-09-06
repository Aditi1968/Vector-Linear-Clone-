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

    # Slack, and all three optional.
    #
    # Optional is the whole point rather than a convenience. Vector is a
    # project management tool that can talk to Slack, not a Slack app: a
    # deployment that never connects Slack must boot, serve every request and
    # pass every test with none of these set, and the day someone supplies
    # them the integration has to work with no code change. Requiring them
    # would make an optional feature a deployment prerequisite; defaulting
    # them to a placeholder would make "misconfigured" indistinguishable from
    # "deliberately not used".
    #
    # None here means UNCONFIGURED, which is a different answer from
    # DISCONNECTED -- see `slack_configured` and the GraphQL enum it feeds.
    # An operator who has set nothing needs to be told to set something; an
    # admin whose workspace has simply not connected yet needs a button.
    # Collapsing the two would show one of them the wrong screen forever.
    slack_client_id: str | None = None

    # SecretStr, like `database_url` and unlike `slack_client_id`. The client
    # id is public by design -- it travels in the authorize URL the browser is
    # redirected to -- while these two are bearer credentials: the secret
    # exchanges an OAuth code for a bot token, and the signing secret is the
    # only thing that distinguishes a real Slack delivery from anyone on the
    # internet posting to the events endpoint. SecretStr keeps both out of a
    # repr, a traceback and a log line by default, so leaking one has to be a
    # deliberate `.get_secret_value()` rather than an accident of printing a
    # settings object.
    slack_client_secret: SecretStr | None = None
    slack_signing_secret: SecretStr | None = None

    @property
    def slack_configured(self) -> bool:
        """Whether this deployment has been given a Slack app at all.

        All three or nothing, deliberately. A partial configuration cannot do
        anything useful -- an OAuth start with no client secret produces a
        callback that cannot exchange its code, and an events endpoint with no
        signing secret cannot verify a single request -- so treating it as
        "configured" would mean advertising a connect button that dead-ends,
        and treating it as configured-but-broken would mean inventing a fourth
        status for a state whose only fix is the same as UNCONFIGURED's.
        """
        return (
            self.slack_client_id is not None
            and self.slack_client_secret is not None
            and self.slack_signing_secret is not None
        )

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
