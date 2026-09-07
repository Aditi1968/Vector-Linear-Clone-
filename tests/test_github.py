"""The GitHub integration below the transport: config, rules, and the schema.

No database, no Docker, no network, and -- the point of the whole exercise --
no GitHub credentials. Everything a real deployment would be given is supplied
here as a fixture, so these tests prove the code is *configuration-driven*
rather than proving that somebody's key works.

Four questions are asked:

  * what "configured" means, and that a deployment with nothing set boots,
    answers, and says UNCONFIGURED rather than DISCONNECTED;
  * that a signature verifies against exactly the bytes that arrived, and
    fails for a tampered body, a wrong secret and a missing header alike;
  * that a redirect target is checked against the allowlist rather than
    trusted;
  * that only an admin or owner of *that* workspace can see or disconnect an
    integration, and that a refusal never says which kind of refusal it was.

tests/test_github_rest.py covers the same rules over real HTTP.
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID

import pytest

from app.domain.errors import (
    GithubNotConfiguredError,
    WorkspaceAccessDeniedError,
)
from app.domain.github import (
    GITHUB_STATUSES,
    GithubInstallationEntity,
    GithubRepositoryEntity,
)
from app.domain.tenancy import AuthorizedWorkspaceScope
from app.graphql.queries.memberships import WORKSPACE_NOT_FOUND_MESSAGE
from app.graphql.schema import build_schema
from app.graphql.types.github import GithubIntegrationStatusType
from app.graphql.viewer import UNAUTHENTICATED_MESSAGE
from app.services.github import (
    GithubAppConfig,
    GithubService,
    allowed_redirect,
    verify_webhook_signature,
)

from tests.conftest import FakePool
from tests.test_settings import PLACEHOLDER_DSN, use_environment


# Built directly rather than imported, so these tests need no DATABASE_URL.
schema = build_schema("test")

WORKSPACE_ID = UUID("00000000-0000-7000-8000-000000000001")
OTHER_WORKSPACE_ID = UUID("00000000-0000-7000-8000-0000000000b1")
VIEWER_USER_ID = UUID("00000000-0000-7000-8000-0000000000e1")

CONNECTED_AT = datetime(2026, 4, 1, 9, 0, 0, tzinfo=timezone.utc)

INSTALLATION_ID = 4242

# Obviously fake, and never sent anywhere: nothing in this module opens a
# socket. The private key is not even PEM -- nothing here parses one, which is
# itself worth pinning: a deployment's key is configuration this code carries,
# not something it validates.
FAKE_APP_ID = "123456"
FAKE_PRIVATE_KEY = "-----BEGIN RSA PRIVATE KEY-----not-a-key-----END-----"
FAKE_WEBHOOK_SECRET = "s3cret-webhook-value"
FAKE_CLIENT_ID = "Iv1.deadbeefdeadbeef"
FAKE_CLIENT_SECRET = "client-secret-value"

APP_ORIGIN = "https://app.vector.test"


def configured(**overrides) -> GithubAppConfig:
    """A fully configured deployment, unless a test knocks a field out."""
    fields = {
        "app_id": FAKE_APP_ID,
        "private_key": FAKE_PRIVATE_KEY,
        "webhook_secret": FAKE_WEBHOOK_SECRET,
        "client_id": FAKE_CLIENT_ID,
        "client_secret": FAKE_CLIENT_SECRET,
        "redirect_allowlist": (APP_ORIGIN,),
    }

    return GithubAppConfig(**(fields | overrides))


def unconfigured() -> GithubAppConfig:
    """What every field of app/config.py defaults to: nothing at all."""
    return GithubAppConfig(
        app_id=None,
        private_key=None,
        webhook_secret=None,
        client_id=None,
        client_secret=None,
        redirect_allowlist=(),
    )


def make_scope(*, role="admin", workspace_id=WORKSPACE_ID) -> AuthorizedWorkspaceScope:
    return AuthorizedWorkspaceScope(
        workspace_id=workspace_id,
        user_id=VIEWER_USER_ID,
        role=role,
    )


def make_installation(**overrides) -> GithubInstallationEntity:
    """A CONFIRMED installation unless a test says otherwise.

    `confirmed_at` defaults to set, because "connected" is what nearly every
    test here means by an installation. An unconfirmed claim -- the state the
    install callback now leaves behind -- is `make_installation(
    confirmed_at=None, account_login=None)`, and the two go together: an
    unconfirmed row may not hold an account login at all, which
    `github_installations_unconfirmed_holds_no_account` enforces in 016.
    """
    fields = {
        "installation_id": INSTALLATION_ID,
        "account_login": "acme",
        "connected_by": VIEWER_USER_ID,
        "connected_at": CONNECTED_AT,
        "updated_at": CONNECTED_AT,
        "confirmed_at": CONNECTED_AT,
    }

    return GithubInstallationEntity(**(fields | overrides))


class FakeGithubRepository:
    """Records every call and replays canned rows.

    Mirrors app.repositories.github.GithubRepository by hand, which is the
    cost of every fake here; the compensating test is
    tests/test_migration_013_db.py, which runs the real statements against a
    real schema.
    """

    def __init__(
        self,
        installation=None,
        repositories=None,
        workspace_id=None,
        confirmed=True,
        expired=False,
    ):
        self.installation = installation
        self.repositories = list(repositories or [])

        # Which workspace holds the row for INSTALLATION_ID, `confirmed`
        # whether GitHub has answered for it, and `expired` whether the claim
        # is past `CLAIM_TTL`. `confirmed` defaults to True because that is
        # what every delivery test in this file means by "this installation
        # belongs to that workspace"; a test about the claim window says so.
        self.workspace_id = workspace_id
        self.confirmed = confirmed
        self.expired = expired
        self.calls: list[tuple] = []

    async def get_installation(self, connection, *, scope):
        self.calls.append(("get_installation", scope.workspace_id))

        return self.installation

    async def list_repositories(self, connection, *, scope):
        self.calls.append(("list_repositories", scope.workspace_id))

        return list(self.repositories)

    async def insert_installation(
        self, connection, *, scope, installation_id, connected_by
    ):
        self.calls.append(
            ("insert_installation", scope.workspace_id, installation_id, connected_by)
        )

        # Unconfirmed and blank, which is the whole point: the callback records
        # a claim, and only a signed delivery turns it into a connection.
        self.installation = make_installation(
            installation_id=installation_id,
            account_login=None,
            connected_by=connected_by,
            confirmed_at=None,
        )
        self.workspace_id = scope.workspace_id
        self.confirmed = False
        self.expired = False

        return self.installation

    async def delete_installation(self, connection, *, scope):
        self.calls.append(("delete_installation", scope.workspace_id))

        existed = self.installation is not None
        self.installation = None

        return existed

    async def set_account_login(self, connection, *, scope, account_login):
        self.calls.append(("set_account_login", scope.workspace_id, account_login))

    async def find_confirmed_workspace_by_installation_id(
        self, connection, *, installation_id
    ):
        self.calls.append(
            ("find_confirmed_workspace_by_installation_id", installation_id)
        )

        return self.workspace_id if self.confirmed else None

    async def confirm_installation(self, connection, *, installation_id, within):
        self.calls.append(("confirm_installation", installation_id, within))

        if self.workspace_id is None or self.confirmed or self.expired:
            return None

        self.confirmed = True

        if self.installation is not None:
            self.installation = make_installation(
                installation_id=self.installation.installation_id,
                account_login=self.installation.account_login,
                connected_by=self.installation.connected_by,
            )

        return self.workspace_id

    async def delete_expired_claim(self, connection, *, installation_id, older_than):
        self.calls.append(("delete_expired_claim", installation_id, older_than))

        if self.workspace_id is None or self.confirmed or not self.expired:
            return False

        self.workspace_id = None
        self.installation = None
        self.expired = False

        return True

    async def add_repositories(self, connection, *, scope, repositories):
        self.calls.append(
            (
                "add_repositories",
                scope.workspace_id,
                tuple((one.repository_id, one.full_name) for one in repositories),
            )
        )
        self.repositories.extend(repositories)

    async def delete_repositories(self, connection, *, scope, repository_ids=None):
        self.calls.append(
            (
                "delete_repositories",
                scope.workspace_id,
                None if repository_ids is None else tuple(repository_ids),
            )
        )

        if repository_ids is None:
            self.repositories = []
        else:
            removed = set(repository_ids)
            self.repositories = [
                one for one in self.repositories if one.repository_id not in removed
            ]

    def called(self, name):
        return [call for call in self.calls if call[0] == name]


def build_service(config=None, **repository_kwargs) -> tuple:
    repository = FakeGithubRepository(**repository_kwargs)
    service = GithubService(
        pool=FakePool(),
        repository=repository,
        config=config if config is not None else configured(),
    )

    return service, repository


# --- what "configured" means ------------------------------------------


def test_a_deployment_with_no_github_settings_is_unconfigured():
    assert unconfigured().configured is False


def test_a_deployment_with_every_credential_is_configured():
    assert configured().configured is True


@pytest.mark.parametrize(
    "missing",
    ["app_id", "private_key", "webhook_secret", "client_id", "client_secret"],
)
def test_a_partial_configuration_is_not_a_configuration(missing):
    """Half a GitHub App is a misconfiguration, not a working one.

    Any one of the five missing breaks a different half of the flow, and all
    five break it silently -- which is why the answer is UNCONFIGURED rather
    than a Connect button that produces a half-connected workspace.
    """
    assert configured(**{missing: None}).configured is False


def test_an_empty_credential_is_not_a_credential():
    """`GITHUB_CLIENT_ID=` in an env file is an unset variable, not a value."""
    assert configured(client_id="").configured is False


def test_the_config_repr_carries_no_secret():
    """A traceback through these frames must not print an RSA private key."""
    printed = repr(configured())

    assert printed == "GithubAppConfig(configured=True)"

    for secret in (FAKE_PRIVATE_KEY, FAKE_WEBHOOK_SECRET, FAKE_CLIENT_SECRET):
        assert secret not in printed


def test_settings_with_no_github_variables_produce_an_unconfigured_app(
    monkeypatch, tmp_path
):
    """The application boots, and reports honestly, with nothing set.

    `use_environment` clears every variable Settings knows about, so this runs
    against a process that has been told nothing about GitHub -- which is the
    state the hard requirement is about.
    """
    from app.config import get_settings

    use_environment(
        monkeypatch,
        tmp_path,
        DATABASE_URL=PLACEHOLDER_DSN,
        ENVIRONMENT="test",
    )

    config = GithubAppConfig.from_settings(get_settings())

    assert config.configured is False
    assert config.redirect_allowlist == ()


def test_settings_carry_the_credentials_through_to_the_config(monkeypatch, tmp_path):
    from app.config import get_settings

    use_environment(
        monkeypatch,
        tmp_path,
        DATABASE_URL=PLACEHOLDER_DSN,
        ENVIRONMENT="test",
        GITHUB_APP_ID=FAKE_APP_ID,
        GITHUB_APP_PRIVATE_KEY=FAKE_PRIVATE_KEY,
        GITHUB_WEBHOOK_SECRET=FAKE_WEBHOOK_SECRET,
        GITHUB_CLIENT_ID=FAKE_CLIENT_ID,
        GITHUB_CLIENT_SECRET=FAKE_CLIENT_SECRET,
        GITHUB_REDIRECT_ALLOWLIST=f"{APP_ORIGIN}, http://localhost:5173/",
    )

    config = GithubAppConfig.from_settings(get_settings())

    assert config.configured is True
    assert config.webhook_secret == FAKE_WEBHOOK_SECRET
    # Origins, normalised: the trailing path is dropped and the case folded,
    # because that is what the comparison in `allowed_redirect` is against.
    assert config.redirect_allowlist == (APP_ORIGIN, "http://localhost:5173")


def test_a_malformed_allowlist_entry_is_dropped_rather_than_fatal(
    monkeypatch, tmp_path
):
    from app.config import get_settings

    use_environment(
        monkeypatch,
        tmp_path,
        DATABASE_URL=PLACEHOLDER_DSN,
        ENVIRONMENT="test",
        GITHUB_REDIRECT_ALLOWLIST=f"not-a-url,,{APP_ORIGIN},javascript:alert(1)",
    )

    assert GithubAppConfig.from_settings(get_settings()).redirect_allowlist == (
        APP_ORIGIN,
    )


# --- webhook signatures -----------------------------------------------


def signed(body: bytes, secret: str = FAKE_WEBHOOK_SECRET) -> str:
    """The header GitHub would send for this body under this secret.

    Written out with hmac here rather than imported from the module under
    test, so that a bug in the production digest cannot cancel itself out.
    """
    import hashlib
    import hmac

    return (
        "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    )


BODY = b'{"action":"created","installation":{"id":4242}}'


def test_a_correctly_signed_body_verifies():
    assert verify_webhook_signature(
        secret=FAKE_WEBHOOK_SECRET,
        body=BODY,
        header=signed(BODY),
    )


def test_a_tampered_body_does_not_verify():
    """One byte changed after signing, which is the whole threat model."""
    tampered = BODY.replace(b"4242", b"9999")

    assert not verify_webhook_signature(
        secret=FAKE_WEBHOOK_SECRET,
        body=tampered,
        header=signed(BODY),
    )


def test_a_signature_from_the_wrong_secret_does_not_verify():
    assert not verify_webhook_signature(
        secret=FAKE_WEBHOOK_SECRET,
        body=BODY,
        header=signed(BODY, secret="a-different-secret"),
    )


def test_a_missing_header_does_not_verify():
    assert not verify_webhook_signature(
        secret=FAKE_WEBHOOK_SECRET,
        body=BODY,
        header=None,
    )


@pytest.mark.parametrize(
    "header",
    [
        "",
        "sha256=",
        # The right digest under the wrong algorithm name. Comparing only the
        # hex half would accept this.
        signed(BODY).replace("sha256=", "sha1="),
        # The digest with the prefix stripped off entirely.
        signed(BODY).removeprefix("sha256="),
        # Truncated: a prefix comparison would accept it.
        signed(BODY)[:20],
        # Non-ASCII, which `hmac.compare_digest` raises TypeError on. It must
        # be refused rather than crash the one endpoint anyone can reach.
        "sha256=é" * 8,
    ],
)
def test_a_malformed_header_does_not_verify(header):
    assert not verify_webhook_signature(
        secret=FAKE_WEBHOOK_SECRET,
        body=BODY,
        header=header,
    )


def test_an_unconfigured_deployment_verifies_nothing():
    """No secret means no delivery is authentic, including an unsigned one."""
    assert not verify_webhook_signature(secret=None, body=BODY, header=signed(BODY))
    assert not verify_webhook_signature(secret=None, body=BODY, header=None)


# --- redirect allowlist -----------------------------------------------


@pytest.mark.parametrize(
    "candidate",
    [
        APP_ORIGIN,
        f"{APP_ORIGIN}/settings/integrations",
        f"{APP_ORIGIN}/settings?tab=github",
        # Case is not part of an origin's identity.
        APP_ORIGIN.replace("https://APP", "https://app")
        .upper()
        .replace("HTTPS", "https"),
    ],
)
def test_an_allowed_origin_is_kept(candidate):
    assert allowed_redirect(candidate, allowlist=(APP_ORIGIN,)) == candidate


@pytest.mark.parametrize(
    "candidate",
    [
        "https://evil.test/steal",
        # The allowed name as a subdomain prefix, which beats `startswith`.
        "https://app.vector.test.evil.test/",
        # The allowed name in the userinfo, which also beats `startswith`.
        "https://app.vector.test@evil.test/",
        # Scheme-relative: no scheme at all, so a browser would inherit one.
        "//evil.test/",
        "javascript:alert(1)",
        "/settings/integrations",
        "",
    ],
)
def test_a_target_off_the_allowlist_is_replaced_rather_than_followed(candidate):
    assert allowed_redirect(candidate, allowlist=(APP_ORIGIN,)) == APP_ORIGIN


def test_no_allowlist_means_no_redirect_at_all():
    """The safe failure: a page, not a guess at where the app lives."""
    assert allowed_redirect(f"{APP_ORIGIN}/x", allowlist=()) is None
    assert allowed_redirect(None, allowlist=()) is None


def test_an_absent_target_falls_back_to_the_configured_origin():
    assert allowed_redirect(None, allowlist=(APP_ORIGIN,)) == APP_ORIGIN


# --- the service's rules ----------------------------------------------


async def test_an_unconfigured_deployment_reports_unconfigured():
    service, _ = build_service(config=unconfigured())

    integration = await service.integration_for(make_scope())

    assert integration.status == "unconfigured"


async def test_a_configured_deployment_with_no_installation_is_disconnected():
    service, _ = build_service()

    integration = await service.integration_for(make_scope())

    assert integration.status == "disconnected"
    assert integration.installation is None


async def test_a_workspace_with_an_installation_is_connected():
    service, _ = build_service(
        installation=make_installation(),
        repositories=[GithubRepositoryEntity(repository_id=7, full_name="acme/web")],
    )

    integration = await service.integration_for(make_scope())

    assert integration.status == "connected"
    assert integration.installation is not None
    assert integration.installation.account_login == "acme"
    assert integration.repositories == (
        GithubRepositoryEntity(repository_id=7, full_name="acme/web"),
    )


async def test_removing_the_credentials_reports_unconfigured_over_connected():
    """An installation survives its deployment losing the key, and says so.

    The row is still reported -- it is real, and the workspace connected it --
    but the status is the honest one, so a UI does not offer Disconnect as the
    fix for a missing private key.
    """
    service, _ = build_service(config=unconfigured(), installation=make_installation())

    integration = await service.integration_for(make_scope())

    assert integration.status == "unconfigured"
    assert integration.installation is not None


@pytest.mark.parametrize("method", ["integration_for", "disconnect"])
async def test_an_ordinary_member_is_refused(method):
    service, repository = build_service(installation=make_installation())

    with pytest.raises(WorkspaceAccessDeniedError):
        await getattr(service, method)(make_scope(role="member"))

    # Refused before any lookup: a member must not be able to learn that an
    # integration exists by timing the refusal either.
    assert repository.calls == []


async def test_connecting_is_refused_on_an_unconfigured_deployment():
    service, repository = build_service(config=unconfigured())

    with pytest.raises(GithubNotConfiguredError):
        await service.connect(make_scope(), installation_id=INSTALLATION_ID)

    assert repository.calls == []


async def test_connecting_records_the_installation_against_the_caller():
    service, repository = build_service()

    integration = await service.connect(make_scope(), installation_id=INSTALLATION_ID)

    assert integration.status == "connected"
    assert repository.called("insert_installation") == [
        ("insert_installation", WORKSPACE_ID, INSTALLATION_ID, VIEWER_USER_ID)
    ]


async def test_reconnecting_replaces_rather_than_merges():
    """A workspace that installs into another account keeps none of the old."""
    service, repository = build_service(
        installation=make_installation(),
        repositories=[GithubRepositoryEntity(repository_id=7, full_name="old/repo")],
    )

    await service.connect(make_scope(), installation_id=99)

    assert [call[0] for call in repository.calls] == [
        "delete_repositories",
        "delete_installation",
        "insert_installation",
    ]
    assert repository.repositories == []


async def test_disconnecting_removes_the_installation_and_its_repositories():
    service, repository = build_service(
        installation=make_installation(),
        repositories=[GithubRepositoryEntity(repository_id=7, full_name="acme/web")],
    )

    integration = await service.disconnect(make_scope())

    assert integration.status == "disconnected"
    assert integration.installation is None
    # Repositories first: `github_repositories_installation_fk` is RESTRICT,
    # so the other order is a foreign key violation against a real database.
    assert [call[0] for call in repository.calls] == [
        "delete_repositories",
        "delete_installation",
    ]
    assert repository.installation is None
    assert repository.repositories == []


async def test_disconnecting_a_workspace_that_never_connected_is_not_an_error():
    service, _ = build_service()

    assert (await service.disconnect(make_scope())).status == "disconnected"


# --- webhook application ----------------------------------------------


def installation_payload(action="created", repositories=None, login="acme"):
    payload = {
        "action": action,
        "installation": {"id": INSTALLATION_ID, "account": {"login": login}},
    }

    if repositories is not None:
        payload["repositories"] = repositories

    return payload


async def test_a_delivery_for_an_unknown_installation_changes_nothing():
    """Ordinary, not an error: installed on GitHub, never finished here."""
    service, repository = build_service(workspace_id=None)

    await service.apply_webhook(event="installation", payload=installation_payload())

    assert [call[0] for call in repository.calls] == [
        "find_workspace_by_installation_id"
    ]


async def test_an_installation_event_fills_in_the_account_and_repositories():
    service, repository = build_service(workspace_id=WORKSPACE_ID)

    await service.apply_webhook(
        event="installation",
        payload=installation_payload(
            repositories=[
                {"id": 7, "full_name": "acme/web"},
                {"id": 8, "full_name": "acme/api"},
            ]
        ),
    )

    assert repository.called("set_account_login") == [
        ("set_account_login", WORKSPACE_ID, "acme")
    ]
    assert repository.called("add_repositories") == [
        ("add_repositories", WORKSPACE_ID, ((7, "acme/web"), (8, "acme/api")))
    ]


async def test_an_installation_event_with_no_repository_list_leaves_the_set_alone():
    """`installation.suspend` says nothing about repositories, so nor do we.

    Treating "not mentioned" as "none" would empty a workspace's repository
    list on an event that had nothing to do with it.
    """
    existing = [GithubRepositoryEntity(repository_id=7, full_name="acme/web")]
    service, repository = build_service(
        workspace_id=WORKSPACE_ID,
        repositories=existing,
    )

    await service.apply_webhook(
        event="installation",
        payload=installation_payload(action="suspend"),
    )

    assert repository.called("delete_repositories") == []
    assert repository.repositories == existing


async def test_an_uninstall_removes_the_workspace_installation():
    service, repository = build_service(
        workspace_id=WORKSPACE_ID,
        installation=make_installation(),
        repositories=[GithubRepositoryEntity(repository_id=7, full_name="acme/web")],
    )

    await service.apply_webhook(
        event="installation",
        payload=installation_payload(action="deleted"),
    )

    assert repository.installation is None
    assert repository.repositories == []


async def test_a_repository_delta_adds_and_removes():
    service, repository = build_service(
        workspace_id=WORKSPACE_ID,
        repositories=[GithubRepositoryEntity(repository_id=7, full_name="acme/web")],
    )

    await service.apply_webhook(
        event="installation_repositories",
        payload={
            "action": "added",
            "installation": {"id": INSTALLATION_ID, "account": {"login": "acme"}},
            "repositories_added": [{"id": 8, "full_name": "acme/api"}],
            "repositories_removed": [{"id": 7, "full_name": "acme/web"}],
        },
    )

    assert repository.repositories == [
        GithubRepositoryEntity(repository_id=8, full_name="acme/api")
    ]


async def test_a_redelivered_delta_lands_on_the_same_state():
    """GitHub retries; at-least-once is the only delivery there is.

    The added ids are deleted before they are inserted, so the second delivery
    is not a primary key violation.
    """
    service, repository = build_service(workspace_id=WORKSPACE_ID)
    payload = {
        "action": "added",
        "installation": {"id": INSTALLATION_ID, "account": {"login": "acme"}},
        "repositories_added": [{"id": 8, "full_name": "acme/api"}],
    }

    await service.apply_webhook(event="installation_repositories", payload=payload)
    await service.apply_webhook(event="installation_repositories", payload=payload)

    assert repository.repositories == [
        GithubRepositoryEntity(repository_id=8, full_name="acme/api")
    ]


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"installation": None},
        {"installation": {}},
        {"installation": {"id": "4242"}},
        {"installation": {"id": 0}},
        {"installation": {"id": -1}},
        # bool is a subclass of int in Python, and `True` would otherwise be
        # read as installation 1 -- a real row in somebody's database.
        {"installation": {"id": True}},
    ],
)
async def test_a_payload_naming_no_installation_reaches_no_lookup(payload):
    service, repository = build_service(workspace_id=WORKSPACE_ID)

    await service.apply_webhook(event="installation", payload=payload)

    assert repository.calls == []


async def test_an_event_with_no_rule_is_accepted_and_ignored():
    service, repository = build_service(workspace_id=WORKSPACE_ID)

    await service.apply_webhook(
        event="push",
        payload={"installation": {"id": INSTALLATION_ID}},
    )

    assert repository.calls == []


async def test_a_malformed_repository_entry_is_skipped_not_fatal():
    service, repository = build_service(workspace_id=WORKSPACE_ID)

    await service.apply_webhook(
        event="installation",
        payload=installation_payload(
            repositories=[
                {"id": 7, "full_name": "acme/web"},
                {"id": None, "full_name": "acme/broken"},
                {"id": 9},
                "not-a-repository",
                # A duplicate would be a primary key violation on the way in.
                {"id": 7, "full_name": "acme/web-renamed"},
            ]
        ),
    )

    assert repository.called("add_repositories") == [
        ("add_repositories", WORKSPACE_ID, ((7, "acme/web-renamed"),))
    ]


# --- an installation id is a claim, not proof -------------------------
#
# The defect this section exists for, stated as the attack that reproduced it:
#
#   an admin of their OWN workspace starts a legitimate install, gets a valid
#   state, and finishes the callback with `installation_id=N` for an N
#   belonging to somebody else's organisation. Nothing in the flow proves the
#   caller owns N. 013's unique constraint made the claim first-come, so from
#   then on every `installation` and `installation_repositories` delivery for
#   the victim's organisation resolved to the ATTACKER's workspace and wrote
#   the victim's account login and private repository names there -- readable
#   through `githubIntegration`. Installation ids are small sequential
#   integers, so pre-claiming a range is cheap, and the real owner was
#   permanently refused with a 409.
#
# The fix is that a callback-supplied id records a CLAIM and nothing more.
# Only a delivery whose HMAC verifies -- the one thing in this system GitHub
# has signed -- can turn a claim into a connection, and only while the claim
# is young. See migrations/016_github_installation_trust.sql.


def confirming_payload(action="created", **kwargs):
    return installation_payload(action=action, **kwargs)


async def test_a_claim_is_not_a_connection():
    """The callback's write, read straight back: PENDING, and blank."""
    service, _ = build_service()

    integration = await service.connect(make_scope(), installation_id=INSTALLATION_ID)

    assert integration.status == "pending"
    assert integration.installation is not None
    assert integration.installation.confirmed_at is None
    assert integration.installation.account_login is None
    assert integration.repositories == ()


async def test_a_delivery_for_an_unconfirmed_claim_writes_nothing():
    """The attack itself: the victim's repository names, going nowhere.

    `installation_repositories` is the delivery an attacker is actually
    waiting for -- it carries private `full_name`s and it fires whenever the
    victim's organisation changes what the app can see. It is not a confirming
    action, so a claim cannot be promoted by it and the workspace it names is
    never resolved.
    """
    service, repository = build_service(workspace_id=WORKSPACE_ID, confirmed=False)

    await service.apply_webhook(
        event="installation_repositories",
        payload={
            "action": "added",
            "installation": {"id": INSTALLATION_ID, "account": {"login": "victim-org"}},
            "repositories_added": [{"id": 8, "full_name": "victim-org/secrets"}],
        },
    )

    assert repository.called("set_account_login") == []
    assert repository.called("add_repositories") == []
    assert repository.repositories == []


@pytest.mark.parametrize("action", ["suspend", "deleted", "added", "removed"])
async def test_an_action_that_is_not_a_confirmation_confirms_nothing(action):
    """Only the actions that accompany a live installation may confirm one.

    `deleted` is in the list on purpose: an uninstall names an installation
    GitHub is ending, and letting it confirm a claim would hand the claimant a
    connection to an organisation that has just revoked the app.
    """
    service, repository = build_service(workspace_id=WORKSPACE_ID, confirmed=False)

    await service.apply_webhook(
        event="installation",
        payload=installation_payload(action=action, repositories=[]),
    )

    assert repository.confirmed is False
    assert repository.called("set_account_login") == []


async def test_a_delivery_for_an_installation_nobody_claimed_writes_nothing():
    service, repository = build_service(workspace_id=None)

    await service.apply_webhook(event="installation", payload=confirming_payload())

    assert [call[0] for call in repository.calls] == [
        "find_confirmed_workspace_by_installation_id",
        "confirm_installation",
    ]
    assert repository.repositories == []


async def test_an_expired_claim_is_not_confirmed_by_a_late_delivery():
    """The window is the whole defence.

    A claim on an installation created months ago is never confirmed, because
    the events that could confirm it were delivered and dropped long before it
    existed. This is the same statement with the clock wound forward: the
    delivery arrives, the claim is too old, and nothing is written.
    """
    service, repository = build_service(
        workspace_id=WORKSPACE_ID,
        confirmed=False,
        expired=True,
    )

    await service.apply_webhook(
        event="installation",
        payload=confirming_payload(repositories=[{"id": 8, "full_name": "acme/api"}]),
    )

    assert repository.confirmed is False
    assert repository.called("set_account_login") == []
    assert repository.repositories == []


@pytest.mark.parametrize(
    "action", ["created", "new_permissions_accepted", "unsuspend"]
)
async def test_a_signed_delivery_confirms_the_claim_it_names(action):
    """The honest path, for each action that accompanies a live installation.

    `new_permissions_accepted` and `unsuspend` are here because
    `installation.created` is dispatched once and can lose the race with the
    browser redirect that records the claim; a workspace whose claim missed it
    still has a way through without another install.
    """
    service, repository = build_service(workspace_id=WORKSPACE_ID, confirmed=False)

    await service.apply_webhook(
        event="installation",
        payload=confirming_payload(
            action=action,
            repositories=[{"id": 7, "full_name": "acme/web"}],
        ),
    )

    assert repository.confirmed is True
    assert repository.called("set_account_login") == [
        ("set_account_login", WORKSPACE_ID, "acme")
    ]
    assert repository.repositories == [
        GithubRepositoryEntity(repository_id=7, full_name="acme/web")
    ]


async def test_a_confirmed_installation_is_not_reconfirmed():
    """The confirmation is asked for only when there is a claim to promote."""
    service, repository = build_service(workspace_id=WORKSPACE_ID)

    await service.apply_webhook(event="installation", payload=confirming_payload())

    assert repository.called("confirm_installation") == []


async def test_a_claim_clears_an_expired_one_before_taking_the_id():
    """A stale claim must not lock the real owner out for good.

    The refusal a live claim earns is a 409 the loser can act on; a claim that
    nobody ever confirmed is not a refusal anyone should still be serving, so
    the next claim on that id sweeps it. Ordered after the caller's own rows
    go and before the insert, all inside one transaction.
    """
    service, repository = build_service(
        workspace_id=OTHER_WORKSPACE_ID,
        confirmed=False,
        expired=True,
    )

    integration = await service.connect(make_scope(), installation_id=INSTALLATION_ID)

    assert [call[0] for call in repository.calls] == [
        "delete_repositories",
        "delete_installation",
        "delete_expired_claim",
        "insert_installation",
    ]
    assert integration.status == "pending"


# --- the GraphQL boundary ---------------------------------------------


GITHUB_INTEGRATION_QUERY = """
query GithubIntegration($slug: String!) {
  githubIntegration(workspaceSlug: $slug) {
    status
    accountLogin
    connectedAt
    connectedById

    repositories {
      repositoryId
      fullName
    }
  }
}
"""

GITHUB_DISCONNECT_MUTATION = """
mutation GithubDisconnect($slug: String!) {
  githubDisconnect(input: { workspaceSlug: $slug }) {
    status
    accountLogin
  }
}
"""


class Context:
    """Stands in for VectorContext, viewer included.

    Mirrors tests/test_graphql_memberships.py: the identity is resolved rather
    than set, because in the real context it comes from a session cookie.
    """

    def __init__(self, membership_service=None, github_service=None, viewer=None):
        self.membership_service = membership_service
        self.github_service = github_service
        self._viewer_user_id = viewer

    async def viewer(self):
        if self._viewer_user_id is None:
            return None

        return SimpleNamespace(id=self._viewer_user_id)


class FakeMembershipService:
    """Answers for one slug and refuses every other, like the real lookup.

    A slug that names no workspace and a workspace the caller is not in are
    the same absent row in `MembershipRepository.find_membership`, so this
    fake cannot tell them apart either -- which is the property under test.
    """

    def __init__(self, *, slug="vector", role="admin", workspace_id=WORKSPACE_ID):
        self._slug = slug
        self._role = role
        self._workspace_id = workspace_id
        self.calls: list[dict] = []

    async def authorized_scope_for_slug(self, *, slug, user_id):
        self.calls.append({"slug": slug, "user_id": user_id})

        if slug != self._slug:
            raise WorkspaceAccessDeniedError()

        return AuthorizedWorkspaceScope(
            workspace_id=self._workspace_id,
            user_id=user_id,
            role=self._role,
        )


def execute(document, *, context, variables=None):
    return schema.execute(
        document,
        variable_values=variables or {"slug": "vector"},
        context_value=context,
    )


@pytest.mark.parametrize(
    "document", [GITHUB_INTEGRATION_QUERY, GITHUB_DISCONNECT_MUTATION]
)
async def test_an_unauthenticated_caller_reaches_no_service(document):
    membership_service = FakeMembershipService()
    result = await execute(
        document,
        context=Context(membership_service=membership_service, viewer=None),
    )

    assert result.errors is not None
    assert result.errors[0].message == UNAUTHENTICATED_MESSAGE
    assert membership_service.calls == []


@pytest.mark.parametrize(
    "document", [GITHUB_INTEGRATION_QUERY, GITHUB_DISCONNECT_MUTATION]
)
async def test_a_member_of_another_workspace_is_told_nothing(document):
    """Workspace A's member asks about workspace B and learns it does not
    exist -- which is also what a caller asking about a typo is told."""
    service, repository = build_service(installation=make_installation())
    result = await execute(
        document,
        context=Context(
            membership_service=FakeMembershipService(slug="vector"),
            github_service=service,
            viewer=VIEWER_USER_ID,
        ),
        variables={"slug": "other-workspace"},
    )

    assert result.errors is not None
    assert result.errors[0].message == WORKSPACE_NOT_FOUND_MESSAGE
    assert result.errors[0].extensions == {"code": "NOT_FOUND"}
    # Nothing was read, and nothing was deleted.
    assert repository.calls == []


@pytest.mark.parametrize(
    "document", [GITHUB_INTEGRATION_QUERY, GITHUB_DISCONNECT_MUTATION]
)
async def test_an_ordinary_member_gets_the_same_answer_as_a_stranger(document):
    """The role refusal must not double as confirmation the integration is
    there. Same message, same code, same shape as the query above."""
    service, repository = build_service(installation=make_installation())
    result = await execute(
        document,
        context=Context(
            membership_service=FakeMembershipService(role="member"),
            github_service=service,
            viewer=VIEWER_USER_ID,
        ),
    )

    assert result.errors is not None
    assert result.errors[0].message == WORKSPACE_NOT_FOUND_MESSAGE
    assert result.errors[0].extensions == {"code": "NOT_FOUND"}
    assert repository.calls == []


async def test_an_admin_sees_the_connected_integration():
    service, _ = build_service(
        installation=make_installation(),
        repositories=[GithubRepositoryEntity(repository_id=7, full_name="acme/web")],
    )
    result = await execute(
        GITHUB_INTEGRATION_QUERY,
        context=Context(
            membership_service=FakeMembershipService(),
            github_service=service,
            viewer=VIEWER_USER_ID,
        ),
    )

    assert result.errors is None
    assert result.data == {
        "githubIntegration": {
            "status": "CONNECTED",
            "accountLogin": "acme",
            "connectedAt": CONNECTED_AT.isoformat(),
            "connectedById": str(VIEWER_USER_ID),
            # An ID, serialised as a string: GitHub's ids outgrow a 32-bit Int.
            "repositories": [{"repositoryId": "7", "fullName": "acme/web"}],
        }
    }


async def test_an_unconfigured_deployment_answers_the_admin_honestly():
    """The UI's disconnected state has to be distinguishable from this one."""
    service, _ = build_service(config=unconfigured())
    result = await execute(
        GITHUB_INTEGRATION_QUERY,
        context=Context(
            membership_service=FakeMembershipService(),
            github_service=service,
            viewer=VIEWER_USER_ID,
        ),
    )

    assert result.errors is None
    assert result.data is not None
    assert result.data["githubIntegration"]["status"] == "UNCONFIGURED"
    assert result.data["githubIntegration"]["accountLogin"] is None
    assert result.data["githubIntegration"]["repositories"] == []


async def test_disconnect_removes_the_installation_and_reports_the_new_state():
    service, repository = build_service(
        installation=make_installation(),
        repositories=[GithubRepositoryEntity(repository_id=7, full_name="acme/web")],
    )
    result = await execute(
        GITHUB_DISCONNECT_MUTATION,
        context=Context(
            membership_service=FakeMembershipService(),
            github_service=service,
            viewer=VIEWER_USER_ID,
        ),
    )

    assert result.errors is None
    assert result.data == {
        "githubDisconnect": {"status": "DISCONNECTED", "accountLogin": None}
    }
    assert repository.installation is None
    assert repository.repositories == []


def test_the_status_enum_matches_the_domain_vocabulary():
    """Three copies of one vocabulary, pinned rather than left to agree."""
    assert (
        tuple(status.value for status in GithubIntegrationStatusType) == GITHUB_STATUSES
    )


# --- nothing secret is reachable through the schema -------------------


FORBIDDEN_IN_SDL = ("privateKey", "clientSecret", "webhookSecret", "accessToken")


@pytest.mark.parametrize("name", FORBIDDEN_IN_SDL)
def test_no_credential_shaped_field_exists_anywhere_in_the_schema(name):
    """The blunt version of the rule, asserted against the whole SDL.

    A field named for a secret is the way a secret reaches a client, and the
    schema is the one artefact where that is visible before it ships. Blunt on
    purpose: this catches the field added to some other type by someone who
    never read app/services/github.py.
    """
    assert name not in schema.as_str()


def test_the_integration_type_exposes_only_facts_about_the_connection():
    """The field list, pinned. Adding one is then a deliberate act."""
    integration = schema.as_str().split("type GithubIntegration {")[1].split("}")[0]
    fields = sorted(
        line.strip().split("(")[0].split(":")[0]
        for line in integration.strip().splitlines()
        if line.strip()
    )

    assert fields == [
        "accountLogin",
        "connectedAt",
        "connectedById",
        "repositories",
        "status",
    ]
