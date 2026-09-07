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

    # Whether THIS process also runs the embedding worker.
    #
    # Semantic search needs `issue_embeddings` filled in, and nothing fills it
    # in on its own: migration 025 builds the whole read path and PostgreSQL has
    # no model, so the vectors arrive from a background pass. This repository
    # has no cron, no Celery and no queue broker, and adding one to run a
    # coroutine every thirty seconds would be a deployment dependency bought for
    # a `while True`. So the pass is a task on the application's own lifespan --
    # see `app.main.lifespan` -- and this flag is what starts it.
    #
    # Off by default, and that is the safe value rather than the timid one. A
    # deployment that sets nothing gets exactly the behaviour it had before this
    # existed: `embeddingsRefresh` still works, `embeddingIndexingState` still
    # reports the truth, and no process quietly starts doing model work nobody
    # asked for. Turning it ON is the deliberate act, and it is one variable.
    #
    # HOW TO RUN IT, plainly. Set `EMBEDDING_WORKER_ENABLED=true` on ONE
    # deployment of this application and start it exactly as any other:
    # `uvicorn app.main:create_app --factory`. That process then serves requests
    # AND drains the queue. To keep model work off the request path, run a
    # second deployment of the same image with the flag on and no traffic routed
    # to it -- there is no separate entrypoint to maintain, and no `scripts/`
    # module either, because the Dockerfile's build context is `app` and
    # `requirements.txt` and nothing else.
    #
    # Setting it on SEVERAL processes is safe and is the point of the queue:
    # `FOR UPDATE SKIP LOCKED` makes two workers' claims disjoint, so they share
    # the backlog rather than duplicating it. See
    # migrations/028_embedding_jobs.sql.
    embedding_worker_enabled: bool = False
    # Where this deployment's front end is served, as an origin
    # ("https://app.vector.dev"), for the one thing that cannot ask a request:
    # the deep link in a Slack message.
    #
    # Every other URL this application builds comes from a request that has a
    # Host header, or from a value a provider already holds. This one is built
    # by a background loop running on no request at all, so there is nothing to
    # infer an origin from -- and inferring one from a proxy-supplied header
    # would be a link whose host is whatever that proxy said.
    #
    # Optional, and None means the message goes out WITHOUT a link rather than
    # with a guessed one. A notification pointing at the wrong host is worse
    # than one pointing nowhere: the first sends somebody to a login page on a
    # domain that is not theirs. See `app.domain.events.message_for`.
    public_base_url: str | None = None

    # --- GitHub App -----------------------------------------------------
    #
    # Every field below is optional, and that is the whole design. Vector is
    # a product with a GitHub integration, not a GitHub client: a deployment
    # that never connects a repository must boot, serve every request and
    # pass every test with none of these set. So there is no default that
    # stands in for a credential, and nothing here is validated against
    # GitHub -- the only proof a key is right is a signature that verifies.
    #
    # With none of them set the integration reports itself UNCONFIGURED,
    # which is a different answer from DISCONNECTED and has to stay
    # different: "this deployment has no GitHub App" is an operator's problem
    # and "this workspace has not connected it yet" is a user's. See
    # app.services.github.GithubAppConfig, which is the one place that
    # decides which of the two a deployment is in.
    #
    # The three secrets are SecretStr so that a settings object caught in a
    # traceback, a repr or a log line prints `SecretStr('**********')` rather
    # than the key itself. `get_secret_value()` is the deliberate act of
    # unwrapping one, and it appears in exactly two places: signature
    # verification and the config object below.
    github_app_id: str | None = None

    # The app's RSA private key, PEM-encoded, whole -- newlines and all.
    # It signs the JWT that mints short-lived installation tokens, so it is
    # the most valuable secret this process holds and it is deliberately not
    # a database column: migrations/013_github_integration.sql says why.
    github_app_private_key: SecretStr | None = None

    # Shared with GitHub when the webhook is configured, and the only thing
    # that distinguishes a delivery from a stranger POSTing JSON at the
    # endpoint. Compared with hmac.compare_digest over the raw body; see
    # app.services.github.verify_webhook_signature.
    github_webhook_secret: SecretStr | None = None

    # The same key, read from a file instead of the environment.
    #
    # A PEM is multi-line, and a multi-line value in a `.env` is a quoting
    # problem with a different answer in every tool that reads one. GitHub
    # also hands the key over as a `.pem` download, so a path is the shape an
    # operator already has -- and it keeps the most valuable secret this
    # process holds out of the environment block, where it would be visible
    # to anything that can read `/proc/<pid>/environ` or a container inspect.
    #
    # Not a SecretStr: a filesystem path is not a credential. What it points
    # at is, and `GithubAppConfig.from_settings` is the one frame that reads
    # it. Exactly one of this and `github_app_private_key` may be set; see
    # `github_private_key` below for why both exist.
    github_private_key_path: str | None = None

    github_client_id: str | None = None
    github_client_secret: SecretStr | None = None

    # Where GitHub sends the browser back, as a whole URL.
    #
    # Registered provider-side, so this is not a preference: it is a copy of
    # a value GitHub already holds, and OAuth fails if the two disagree. It
    # is separate from `github_redirect_allowlist`, which governs where the
    # callback may send the browser NEXT -- one is the door GitHub knocks on,
    # the other is where the user ends up.
    github_oauth_callback_url: str | None = None

    # Where the install callback is allowed to send a browser afterwards, as
    # comma-separated origins ("https://app.vector.dev,http://localhost:5173").
    #
    # A plain string rather than a list because pydantic-settings parses a
    # list-typed field from the environment as JSON, and an operator writing
    # `GITHUB_REDIRECT_ALLOWLIST=https://app.vector.dev` would get a parse
    # error rather than a setting. Split in one place --
    # `GithubAppConfig.from_settings` -- so no other reader can disagree
    # about what a separator is.
    #
    # Empty by default, which means "redirect nowhere": the callback answers
    # with a plain page instead. That is the safe failure. An allowlist that
    # defaulted to something would be a redirect target chosen by whoever
    # wrote this file rather than by the deployment.
    github_redirect_allowlist: str = ""
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

    # Where Slack sends the browser back, as a whole URL, registered
    # provider-side. Sent as `redirect_uri` on both legs of the flow, because
    # Slack requires the value presented at the token exchange to match the
    # one the authorization began with, byte for byte.
    slack_oauth_callback_url: str | None = None

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
