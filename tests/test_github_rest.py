"""The install flow and the webhook receiver, over real HTTP. No network.

Everything a GitHub App would be given arrives here as a fixture, and nothing
in this module opens a socket to github.com: the install is asserted by reading
the Location header rather than following it, and a delivery is a body this
file signs itself with the fixture secret.

What is under test is the part that is dangerous rather than the part that is
new. An OAuth callback with an unchecked `state` will attach an attacker's
installation to a signed-in victim's workspace; a webhook without a verified
signature will let anyone rewrite which repositories a tenant is connected to;
and a callback that forwards to a URL from its own query string is an open
redirect on a domain the user has just been asked to trust. Each has a section
below.
"""

import hashlib
import hmac
import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import FastAPI

from app.config import get_settings
from app.domain.errors import (
    GithubInstallationClaimedError,
    WorkspaceAccessDeniedError,
)
from app.domain.github import GithubRepositoryEntity
from app.domain.tenancy import AuthorizedWorkspaceScope
from app.http_cookies import SESSION_COOKIE_NAME
from app.main import create_app
from app.rest.github import (
    GITHUB_AUTHORIZE_URL,
    STATE_COOKIE_NAME,
    GithubHttpServices,
    build_services,
    router,
)
from app.services.github import GithubService

from tests.conftest import FakePool
from tests.test_github import (
    APP_ORIGIN,
    FAKE_CLIENT_ID,
    FAKE_WEBHOOK_SECRET,
    INSTALLATION_ID,
    VIEWER_USER_ID,
    WORKSPACE_ID,
    FakeGithubRepository,
    configured,
    make_installation,
    unconfigured,
)
from tests.test_settings import PLACEHOLDER_DSN, use_environment


SESSION_TOKEN = "session-token-for-the-installing-admin"
OTHER_SESSION_TOKEN = "session-token-for-somebody-else"

WORKSPACE_SLUG = "vector"

# A URL on the allowlist and one that is not. The second is the one an open
# redirect would follow.
ALLOWED_RETURN = f"{APP_ORIGIN}/settings/integrations"
FOREIGN_RETURN = "https://evil.test/collect"


class FakeAuthService:
    """Maps a session token to a user, and everything else to nobody."""

    def __init__(self, tokens=None):
        self._tokens = tokens if tokens is not None else {SESSION_TOKEN: VIEWER_USER_ID}
        self.calls: list[str | None] = []

    async def authenticate(self, token):
        self.calls.append(token)

        user_id = self._tokens.get(token)

        return None if user_id is None else SimpleNamespace(id=user_id)


class FakeMembershipService:
    """One workspace, one role. Every other slug is an absent row."""

    def __init__(self, *, slug=WORKSPACE_SLUG, role="admin"):
        self._slug = slug
        self._role = role
        self.calls: list[dict] = []

    async def authorized_scope_for_slug(self, *, slug, user_id):
        self.calls.append({"slug": slug, "user_id": user_id})

        if slug != self._slug:
            raise WorkspaceAccessDeniedError()

        return AuthorizedWorkspaceScope(
            workspace_id=WORKSPACE_ID,
            user_id=user_id,
            role=self._role,
        )


class ClaimedGithubRepository(FakeGithubRepository):
    """Whose installation id already belongs to somebody else."""

    async def insert_installation(self, connection, *, scope, **kwargs):
        raise GithubInstallationClaimedError()


def build_services_for(
    *,
    config=None,
    role="admin",
    tokens=None,
    repository=None,
) -> GithubHttpServices:
    return GithubHttpServices(
        github=GithubService(
            pool=FakePool(),
            repository=repository if repository is not None else FakeGithubRepository(),
            config=config if config is not None else configured(),
        ),
        auth=FakeAuthService(tokens),
        memberships=FakeMembershipService(role=role),
        environment="test",
    )


def build_client(services: GithubHttpServices) -> httpx.AsyncClient:
    """The router alone, with its one dependency replaced.

    A bare FastAPI application rather than `create_app()`, so these tests need
    no DATABASE_URL and no lifespan -- the real composition root is asserted
    separately at the bottom of this file, which is the only thing it can tell
    us that this cannot.
    """
    app = FastAPI()

    app.include_router(router)
    app.dependency_overrides[build_services] = lambda: services

    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://vector.test",
    )


def signed(body: bytes, secret: str = FAKE_WEBHOOK_SECRET) -> dict[str, str]:
    """The headers GitHub would send for these exact bytes."""
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()

    return {
        "X-Hub-Signature-256": f"sha256={digest}",
        "X-GitHub-Event": "installation",
        "Content-Type": "application/json",
    }


def state_from(response: httpx.Response) -> str:
    """The state the install redirect put in the URL GitHub is handed."""
    query = parse_qs(urlsplit(response.headers["location"]).query)

    return query["state"][0]


async def start_install(client: httpx.AsyncClient, **params) -> httpx.Response:
    """Sign in and start an install, leaving the state cookie in the jar."""
    client.cookies.set(SESSION_COOKIE_NAME, params.pop("session", SESSION_TOKEN))

    return await client.get(
        "/github/install",
        params={"workspace": WORKSPACE_SLUG, **params},
    )


# --- starting an install ----------------------------------------------


async def test_an_unconfigured_deployment_offers_no_install():
    """404, and no state cookie: there is nothing here to install.

    Not a 501 and not a message naming a missing setting. This endpoint is
    reachable by anyone who can reach the port, and both would describe how
    this deployment is provisioned.
    """
    async with build_client(build_services_for(config=unconfigured())) as client:
        response = await start_install(client)

    assert response.status_code == 404
    assert STATE_COOKIE_NAME not in response.cookies


async def test_an_unauthenticated_caller_cannot_start_an_install():
    async with build_client(build_services_for()) as client:
        response = await client.get(
            "/github/install", params={"workspace": WORKSPACE_SLUG}
        )

    assert response.status_code == 401
    assert STATE_COOKIE_NAME not in response.cookies


async def test_an_ordinary_member_cannot_start_an_install():
    """Refused here rather than after GitHub, where the refusal is expensive."""
    async with build_client(build_services_for(role="member")) as client:
        response = await start_install(client)

    assert response.status_code == 404
    assert STATE_COOKIE_NAME not in response.cookies


async def test_a_member_of_another_workspace_cannot_start_an_install():
    async with build_client(build_services_for()) as client:
        client.cookies.set(SESSION_COOKIE_NAME, SESSION_TOKEN)
        response = await client.get(
            "/github/install", params={"workspace": "somebody-elses-workspace"}
        )

    assert response.status_code == 404


async def test_an_admin_is_redirected_to_github_with_a_fresh_state():
    async with build_client(build_services_for()) as client:
        response = await start_install(client)

        assert response.status_code == 302

        location = urlsplit(response.headers["location"])
        query = parse_qs(location.query)

        assert f"{location.scheme}://{location.netloc}{location.path}" == (
            GITHUB_AUTHORIZE_URL
        )
        assert query["client_id"] == [FAKE_CLIENT_ID]
        # 32 bytes of entropy, base64url: long enough that this assertion is
        # about the generator rather than the format.
        assert len(query["state"][0]) >= 40
        assert STATE_COOKIE_NAME in client.cookies


async def test_two_installs_do_not_share_a_state():
    async with build_client(build_services_for()) as client:
        first = state_from(await start_install(client))
        second = state_from(await start_install(client))

    assert first != second


async def test_the_state_cookie_is_not_readable_by_script():
    async with build_client(build_services_for()) as client:
        response = await start_install(client)

    header = response.headers["set-cookie"]

    assert "httponly" in header.lower()
    # Site-wide. The App's registered callback lives under `/integrations`
    # while the install begins under `/github`, and a cookie scoped to either
    # prefix is never sent to the other -- which refused legitimate flows. The
    # scope is not what protects this cookie; HttpOnly, the ten-minute expiry,
    # single use, and the session digest checked in the callback are.
    assert "path=/;" in header.lower() or header.lower().rstrip().endswith("path=/")
    assert "samesite=lax" in header.lower()
    # Not Secure in a test environment, which is what makes the cookie work
    # over plain http locally; production is the only environment that sets it.
    assert "secure" not in header.lower()


# --- finishing one ----------------------------------------------------


async def test_a_valid_callback_records_the_installation():
    repository = FakeGithubRepository()

    async with build_client(build_services_for(repository=repository)) as client:
        state = state_from(await start_install(client, return_to=ALLOWED_RETURN))

        response = await client.get(
            "/github/callback",
            params={"state": state, "installation_id": INSTALLATION_ID},
        )

    assert response.status_code == 302
    assert response.headers["location"] == ALLOWED_RETURN
    assert repository.called("insert_installation") == [
        ("insert_installation", WORKSPACE_ID, INSTALLATION_ID, VIEWER_USER_ID)
    ]


async def test_a_callback_with_no_state_at_all_connects_nothing():
    """The bare CSRF: a link in an email, aimed at a signed-in victim."""
    repository = FakeGithubRepository()

    async with build_client(build_services_for(repository=repository)) as client:
        client.cookies.set(SESSION_COOKIE_NAME, SESSION_TOKEN)

        response = await client.get(
            "/github/callback", params={"installation_id": INSTALLATION_ID}
        )

    assert response.status_code == 400
    assert repository.calls == []


async def test_a_foreign_state_connects_nothing():
    """A state this browser never minted matches no cookie it holds."""
    repository = FakeGithubRepository()

    async with build_client(build_services_for(repository=repository)) as client:
        await start_install(client)

        response = await client.get(
            "/github/callback",
            params={
                "state": "a-state-from-somebody-elses-install",
                "installation_id": INSTALLATION_ID,
            },
        )

    assert response.status_code == 400
    assert repository.calls == []


async def test_a_replayed_state_connects_nothing_the_second_time():
    """Single use: the callback consumes the cookie, so the replay has none."""
    repository = FakeGithubRepository()

    async with build_client(build_services_for(repository=repository)) as client:
        state = state_from(await start_install(client))
        params = {"state": state, "installation_id": INSTALLATION_ID}

        first = await client.get("/github/callback", params=params)
        second = await client.get("/github/callback", params=params)

    assert first.status_code in (200, 302)
    assert second.status_code == 400
    assert len(repository.called("insert_installation")) == 1


async def test_a_state_minted_under_another_session_connects_nothing():
    """The binding: holding the cookie is not enough without the session.

    This is the attack the session digest exists for -- someone who can write
    a cookie into the victim's browser (a compromised sibling subdomain) but
    cannot read the HttpOnly session token it was bound to.
    """
    repository = FakeGithubRepository()
    services = build_services_for(
        repository=repository,
        tokens={
            SESSION_TOKEN: VIEWER_USER_ID,
            OTHER_SESSION_TOKEN: VIEWER_USER_ID,
        },
    )

    async with build_client(services) as client:
        state = state_from(await start_install(client))

        # Same browser, same state cookie, different signed-in session.
        client.cookies.set(SESSION_COOKIE_NAME, OTHER_SESSION_TOKEN)

        response = await client.get(
            "/github/callback",
            params={"state": state, "installation_id": INSTALLATION_ID},
        )

    assert response.status_code == 400
    assert repository.calls == []


async def test_a_callback_with_no_installation_records_nothing():
    """`setup_action=request`: an org owner still has to approve."""
    repository = FakeGithubRepository()

    async with build_client(build_services_for(repository=repository)) as client:
        state = state_from(await start_install(client))

        response = await client.get(
            "/github/callback", params={"state": state, "setup_action": "request"}
        )

    assert response.status_code == 400
    assert repository.calls == []


async def test_an_installation_another_workspace_already_holds_is_refused():
    async with build_client(
        build_services_for(repository=ClaimedGithubRepository())
    ) as client:
        state = state_from(await start_install(client))

        response = await client.get(
            "/github/callback",
            params={"state": state, "installation_id": INSTALLATION_ID},
        )

    assert response.status_code == 409


async def test_a_signed_out_caller_cannot_finish_somebody_elses_install():
    repository = FakeGithubRepository()

    async with build_client(build_services_for(repository=repository)) as client:
        state = state_from(await start_install(client))

        client.cookies.delete(SESSION_COOKIE_NAME)

        response = await client.get(
            "/github/callback",
            params={"state": state, "installation_id": INSTALLATION_ID},
        )

    # 400 rather than 401: the state was bound to a session that is no longer
    # being presented, so the state check refuses before identity is asked.
    assert response.status_code == 400
    assert repository.calls == []


# --- where the browser is sent afterwards -----------------------------


async def test_a_return_target_off_the_allowlist_is_not_followed():
    """The open redirect, refused. The user still lands back in the app."""
    async with build_client(build_services_for()) as client:
        state = state_from(await start_install(client, return_to=FOREIGN_RETURN))

        response = await client.get(
            "/github/callback",
            params={"state": state, "installation_id": INSTALLATION_ID},
        )

    assert response.status_code == 302
    assert response.headers["location"] == APP_ORIGIN


async def test_a_forged_state_cookie_cannot_redirect_anywhere_it_likes():
    """The cookie is re-validated on the way out, not merely on the way in.

    A cookie is not a value this server chose: it travels through a client
    that can rewrite it. Hand-built here with a target that was never checked,
    to prove the callback checks it again.
    """
    import base64

    services = build_services_for()

    async with build_client(services) as client:
        state = state_from(await start_install(client))

        forged = json.loads(base64.urlsafe_b64decode(client.cookies[STATE_COOKIE_NAME]))
        forged["return_to"] = FOREIGN_RETURN

        client.cookies.set(
            STATE_COOKIE_NAME,
            base64.urlsafe_b64encode(json.dumps(forged).encode()).decode(),
            path="/github",
        )

        response = await client.get(
            "/github/callback",
            params={"state": state, "installation_id": INSTALLATION_ID},
        )

    assert response.status_code == 302
    assert response.headers["location"] == APP_ORIGIN


async def test_no_allowlist_ends_the_flow_on_a_page_rather_than_a_guess():
    services = build_services_for(config=configured(redirect_allowlist=()))

    async with build_client(services) as client:
        state = state_from(await start_install(client, return_to=ALLOWED_RETURN))

        response = await client.get(
            "/github/callback",
            params={"state": state, "installation_id": INSTALLATION_ID},
        )

    assert response.status_code == 200
    assert "location" not in response.headers


# --- deliveries -------------------------------------------------------


def installation_body(
    action="created",
    login="acme",
    repositories=None,
    installation_id=INSTALLATION_ID,
) -> bytes:
    payload = {
        "action": action,
        "installation": {"id": installation_id, "account": {"login": login}},
    }

    if repositories is not None:
        payload["repositories"] = repositories

    # Deliberately not the canonical separators the server would produce if it
    # re-serialised the payload. A signature verified over a round-tripped
    # document would pass for the wrong bytes; over these it can only pass for
    # the ones that arrived.
    return json.dumps(payload, indent=2, sort_keys=False).encode("utf-8")


async def test_a_correctly_signed_delivery_is_applied():
    repository = FakeGithubRepository(workspace_id=WORKSPACE_ID)
    body = installation_body(repositories=[{"id": 7, "full_name": "acme/web"}])

    async with build_client(build_services_for(repository=repository)) as client:
        response = await client.post(
            "/github/webhook", content=body, headers=signed(body)
        )

    assert response.status_code == 204
    assert repository.called("set_account_login") == [
        ("set_account_login", WORKSPACE_ID, "acme")
    ]
    assert repository.repositories == [
        GithubRepositoryEntity(repository_id=7, full_name="acme/web")
    ]


async def test_a_tampered_body_is_rejected_with_no_processing():
    repository = FakeGithubRepository(workspace_id=WORKSPACE_ID)
    body = installation_body()
    headers = signed(body)

    async with build_client(build_services_for(repository=repository)) as client:
        response = await client.post(
            "/github/webhook",
            content=body.replace(b'"acme"', b'"attacker"'),
            headers=headers,
        )

    assert response.status_code == 401
    assert repository.calls == []


async def test_a_signature_from_the_wrong_secret_is_rejected():
    repository = FakeGithubRepository(workspace_id=WORKSPACE_ID)
    body = installation_body()

    async with build_client(build_services_for(repository=repository)) as client:
        response = await client.post(
            "/github/webhook",
            content=body,
            headers=signed(body, secret="not-the-configured-secret"),
        )

    assert response.status_code == 401
    assert repository.calls == []


async def test_an_unsigned_delivery_is_rejected():
    repository = FakeGithubRepository(workspace_id=WORKSPACE_ID)
    body = installation_body()

    async with build_client(build_services_for(repository=repository)) as client:
        response = await client.post(
            "/github/webhook",
            content=body,
            headers={"X-GitHub-Event": "installation"},
        )

    assert response.status_code == 401
    assert repository.calls == []


async def test_an_unconfigured_deployment_processes_no_delivery():
    """No secret means nothing is authentic, however well-formed it looks."""
    repository = FakeGithubRepository(workspace_id=WORKSPACE_ID)
    body = installation_body()

    async with build_client(
        build_services_for(config=unconfigured(), repository=repository)
    ) as client:
        response = await client.post(
            "/github/webhook", content=body, headers=signed(body)
        )

    assert response.status_code == 401
    assert repository.calls == []


async def test_a_rejected_delivery_is_told_nothing():
    """One answer for a missing header, a wrong one, and no secret at all."""
    body = installation_body()
    answers = set()

    for headers in (
        {"X-GitHub-Event": "installation"},
        signed(body, secret="wrong"),
    ):
        async with build_client(build_services_for()) as client:
            response = await client.post(
                "/github/webhook", content=body, headers=headers
            )

            answers.add((response.status_code, response.text))

    async with build_client(build_services_for(config=unconfigured())) as client:
        response = await client.post(
            "/github/webhook", content=body, headers=signed(body)
        )

        answers.add((response.status_code, response.text))

    assert len(answers) == 1

    for _, text in answers:
        assert FAKE_WEBHOOK_SECRET not in text


async def test_a_signed_uninstall_removes_the_installation():
    repository = FakeGithubRepository(
        workspace_id=WORKSPACE_ID,
        installation=make_installation(),
        repositories=[GithubRepositoryEntity(repository_id=7, full_name="acme/web")],
    )
    body = installation_body(action="deleted")

    async with build_client(build_services_for(repository=repository)) as client:
        response = await client.post(
            "/github/webhook", content=body, headers=signed(body)
        )

    assert response.status_code == 204
    assert repository.installation is None
    assert repository.repositories == []


async def test_a_signed_delivery_that_is_not_json_is_refused():
    body = b"not json at all"

    async with build_client(build_services_for()) as client:
        response = await client.post(
            "/github/webhook", content=body, headers=signed(body)
        )

    assert response.status_code == 400


async def test_a_signed_delivery_this_server_has_no_rule_for_is_accepted():
    """204, so GitHub does not retry a payload nothing will ever handle."""
    repository = FakeGithubRepository(workspace_id=WORKSPACE_ID)
    body = installation_body()
    headers = signed(body) | {"X-GitHub-Event": "push"}

    async with build_client(build_services_for(repository=repository)) as client:
        response = await client.post("/github/webhook", content=body, headers=headers)

    assert response.status_code == 204
    assert repository.calls == []


# --- an installation id is a claim, not proof -------------------------
#
# The whole defect end to end, over HTTP, with nothing faked but the database
# and the session lookup. An admin who is entitled to every request they make
# here still cannot take delivery of an organisation they do not own.


VICTIM_INSTALLATION_ID = 5150


def repositories_body(installation_id=VICTIM_INSTALLATION_ID) -> bytes:
    """An `installation_repositories` delivery: the one that carries names."""
    return json.dumps(
        {
            "action": "added",
            "installation": {
                "id": installation_id,
                "account": {"login": "victim-org"},
            },
            "repositories_added": [{"id": 99, "full_name": "victim-org/payments"}],
        },
        indent=2,
    ).encode("utf-8")


async def test_a_valid_callback_records_a_claim_and_nothing_more():
    """The install callback's own answer, which used to read "connected".

    Everything about this request is legitimate -- the admin's own workspace,
    their own state, their own session. It is the installation id that is
    unproven, so what is recorded is a claim: unconfirmed and blank.
    """
    repository = FakeGithubRepository()

    async with build_client(build_services_for(repository=repository)) as client:
        state = state_from(await start_install(client))

        response = await client.get(
            "/github/callback",
            params={"state": state, "installation_id": INSTALLATION_ID},
        )

    assert response.status_code == 302
    assert repository.confirmed is False
    assert repository.installation is not None
    assert repository.installation.confirmed_at is None
    assert repository.installation.account_login is None


async def test_claiming_another_organisations_installation_yields_nothing():
    """The attack, start to finish.

    An admin of their OWN workspace starts a legitimate install, gets a valid
    state, and finishes the callback naming an installation id belonging to
    somebody else's organisation. Then that organisation's own delivery
    arrives, correctly signed, as one will every time they change what the app
    can see.

    Before migrations/016_github_installation_trust.sql the claim was recorded
    as a connection and this delivery wrote `victim-org` and
    `victim-org/payments` into the attacker's workspace, readable through
    `githubIntegration`. Now the claim resolves nothing:
    `installation_repositories` cannot confirm a claim, and it is excluded for
    exactly this reason.
    """
    repository = FakeGithubRepository()

    async with build_client(build_services_for(repository=repository)) as client:
        state = state_from(await start_install(client))

        await client.get(
            "/github/callback",
            params={"state": state, "installation_id": VICTIM_INSTALLATION_ID},
        )

        body = repositories_body()
        delivery = await client.post(
            "/github/webhook",
            content=body,
            headers=signed(body) | {"X-GitHub-Event": "installation_repositories"},
        )

    # Accepted, because refusing would earn GitHub a redelivery loop for a
    # payload that will never be handled differently. Applied to nobody.
    assert delivery.status_code == 204
    assert repository.confirmed is False
    assert repository.called("set_account_login") == []
    assert repository.repositories == []


async def test_a_signed_installation_created_confirms_the_claim_it_names():
    """The honest path over HTTP: claim, then GitHub says so, then connected."""
    repository = FakeGithubRepository()

    async with build_client(build_services_for(repository=repository)) as client:
        state = state_from(await start_install(client))

        await client.get(
            "/github/callback",
            params={"state": state, "installation_id": INSTALLATION_ID},
        )

        body = installation_body(repositories=[{"id": 7, "full_name": "acme/web"}])
        delivery = await client.post(
            "/github/webhook", content=body, headers=signed(body)
        )

    assert delivery.status_code == 204
    assert repository.confirmed is True
    assert repository.called("set_account_login") == [
        ("set_account_login", WORKSPACE_ID, "acme")
    ]
    assert repository.repositories == [
        GithubRepositoryEntity(repository_id=7, full_name="acme/web")
    ]


async def test_a_delivery_for_an_installation_this_workspace_did_not_claim():
    """A signed delivery is not a licence to write into whoever is nearby.

    The workspace holds a claim on one installation and the delivery names a
    different one, so nothing resolves and nothing is written -- and in
    particular this workspace's claim is not confirmed by somebody else's
    installation being created.
    """
    repository = FakeGithubRepository()

    async with build_client(build_services_for(repository=repository)) as client:
        state = state_from(await start_install(client))

        await client.get(
            "/github/callback",
            params={"state": state, "installation_id": INSTALLATION_ID},
        )

        body = installation_body(installation_id=VICTIM_INSTALLATION_ID)
        delivery = await client.post(
            "/github/webhook", content=body, headers=signed(body)
        )

    assert delivery.status_code == 204
    assert repository.confirmed is False
    assert repository.called("set_account_login") == []


# --- the real composition root ----------------------------------------


@pytest.fixture
def clear_settings_cache():
    get_settings.cache_clear()

    yield

    get_settings.cache_clear()


def test_the_application_boots_and_mounts_the_routes_with_no_github_settings(
    monkeypatch, tmp_path, clear_settings_cache
):
    """The hard requirement, asserted at the composition root.

    `use_environment` clears every variable Settings knows about, so this
    builds the real application on a process that has been told nothing about
    GitHub -- and the routes are there anyway. The mount is unconditional on
    purpose: a deployment that is given credentials tomorrow starts working
    without a code change, and an unconfigured one refuses in the handler,
    where the answer is the same 404 a stranger gets for any other reason.

    The routes are read off the application rather than called, because
    calling one needs the lifespan's connection pool and the subject here is
    composition rather than behaviour -- which every test above covers over
    real HTTP.

    Read through `openapi()` rather than by walking `app.routes`, which in this
    FastAPI version holds an opaque wrapper per included router rather than the
    routes themselves.
    """
    use_environment(
        monkeypatch,
        tmp_path,
        DATABASE_URL=PLACEHOLDER_DSN,
        ENVIRONMENT="test",
    )

    paths = create_app().openapi()["paths"]

    assert "get" in paths["/github/install"]
    assert "get" in paths["/github/callback"]
    assert "post" in paths["/github/webhook"]
