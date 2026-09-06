"""GraphQL transport tests for membership. No database involved.

Three things are asked of this layer and nothing else is:

  * an unidentified caller is refused before any protected lookup runs;
  * a refusal reads the same whether the workspace is missing or merely
    not the viewer's;
  * a role crosses the boundary as the enum value the schema declares.

The schema is the real one, built through `build_schema`, so the masking
policy in `app.graphql.schema` applies exactly as it does in production --
which is the point of not asserting against a hand-built schema here. A
code that is not in PUBLIC_ERROR_CODES arrives with its message replaced,
so these tests fail if either error code is ever unpublished.
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID

import pytest

from app.domain.errors import WorkspaceAccessDeniedError
from app.domain.memberships import WorkspaceMembershipEntity
from app.graphql.queries.memberships import WORKSPACE_NOT_FOUND_MESSAGE
from app.graphql.schema import MASKED_ERROR_MESSAGE, build_schema
from app.graphql.viewer import UNAUTHENTICATED_MESSAGE


# Built directly rather than imported, so these tests need no DATABASE_URL.
schema = build_schema("test")


MY_WORKSPACES_QUERY = """
query MyWorkspaces {
  myWorkspaces {
    workspace {
      id
      slug
      name
    }

    role
    createdAt
  }
}
"""

MY_WORKSPACE_QUERY = """
query MyWorkspace($slug: String!) {
  myWorkspace(slug: $slug) {
    workspace {
      id
      slug
      name
    }

    role
  }
}
"""

VIEWER_USER_ID = UUID("00000000-0000-7000-8000-0000000000e1")
WORKSPACE_ID = UUID("00000000-0000-7000-8000-000000000001")

CREATED_AT = datetime(2026, 3, 1, 9, 0, 0, tzinfo=timezone.utc)


def make_membership(*, slug="vector", name="Vector", role="owner"):
    return WorkspaceMembershipEntity(
        workspace_id=WORKSPACE_ID,
        workspace_slug=slug,
        workspace_name=name,
        user_id=VIEWER_USER_ID,
        role=role,
        created_at=CREATED_AT,
    )


class Context:
    """Stands in for VectorContext, viewer included.

    `viewer()` is awaited rather than read, mirroring the real context: the
    identity comes from the request's session cookie, so it is resolved and
    not a value the caller sets. Tests still express "nobody is signed in"
    as `viewer_user_id=None`, which is what the session layer produces for a
    request with no cookie, an expired one, or one naming no session.
    """

    def __init__(self, membership_service=None, viewer_user_id=None):
        self.membership_service = membership_service
        self._viewer_user_id = viewer_user_id

    async def viewer(self):
        if self._viewer_user_id is None:
            return None

        return SimpleNamespace(id=self._viewer_user_id)


class FakeMembershipService:
    """Records every call, so "was the service reached" is answerable."""

    def __init__(self, memberships=None, membership=None):
        self._memberships = memberships if memberships is not None else []
        self._membership = membership
        self.calls: list[dict] = []

    async def list_for_user(self, *, user_id):
        self.calls.append({"method": "list_for_user", "user_id": user_id})

        return list(self._memberships)

    async def membership_for_slug(self, *, slug, user_id):
        self.calls.append(
            {"method": "membership_for_slug", "slug": slug, "user_id": user_id}
        )

        if self._membership is None:
            raise WorkspaceAccessDeniedError()

        return self._membership


class ExplodingMembershipService:
    """Fails if anything calls it. An unauthenticated request must not."""

    async def list_for_user(self, *, user_id):
        raise AssertionError("an unauthenticated caller reached the service")

    async def membership_for_slug(self, *, slug, user_id):
        raise AssertionError("an unauthenticated caller reached the service")


class BrokenMembershipService:
    async def list_for_user(self, *, user_id):
        raise RuntimeError("connection reset by peer")

    async def membership_for_slug(self, *, slug, user_id):
        raise RuntimeError("connection reset by peer")


def only_error(result):
    assert result.errors is not None
    assert len(result.errors) == 1

    return result.errors[0].formatted


# --- an unidentified caller -------------------------------------------


@pytest.mark.parametrize(
    ("document", "variables"),
    [
        (MY_WORKSPACES_QUERY, None),
        (MY_WORKSPACE_QUERY, {"slug": "vector"}),
    ],
    ids=["myWorkspaces", "myWorkspace"],
)
async def test_an_unauthenticated_request_is_refused_before_any_lookup(
    document, variables
):
    """UNAUTHENTICATED, and the service is never asked anything.

    The second half is the one worth the exploding fake: a resolver that
    fetched first and checked afterwards would produce the same response
    while having already read a protected table on behalf of a stranger.
    """
    result = await schema.execute(
        document,
        variable_values=variables,
        context_value=Context(
            membership_service=ExplodingMembershipService(),
            viewer_user_id=None,
        ),
    )

    formatted = only_error(result)

    assert formatted["message"] == UNAUTHENTICATED_MESSAGE
    assert formatted["extensions"] == {"code": "UNAUTHENTICATED"}


async def test_the_unauthenticated_message_survives_error_masking():
    """A masked error would leave a client with nothing to act on.

    Stated as its own assertion because the failure mode is silent: drop
    UNAUTHENTICATED from PUBLIC_ERROR_CODES and every test above still
    sees one error on an unauthenticated request -- just an unusable one.
    """
    result = await schema.execute(
        MY_WORKSPACES_QUERY,
        context_value=Context(viewer_user_id=None),
    )

    assert only_error(result)["message"] != MASKED_ERROR_MESSAGE


# --- a refusal that says nothing about existence ----------------------


async def test_a_refused_workspace_reads_as_not_found():
    service = FakeMembershipService(membership=None)

    result = await schema.execute(
        MY_WORKSPACE_QUERY,
        variable_values={"slug": "vector"},
        context_value=Context(service, VIEWER_USER_ID),
    )

    formatted = only_error(result)

    assert formatted["message"] == WORKSPACE_NOT_FOUND_MESSAGE
    assert formatted["extensions"] == {"code": "NOT_FOUND"}


async def test_two_refusals_are_byte_for_byte_the_same_response():
    """The service raises one error for both cases, so the boundary must
    render one response for both.

    Asserted on the whole formatted error rather than on the message, so
    an extension added later -- a slug echoed back, a reason, a hint --
    fails here rather than shipping as a distinguisher.
    """
    absent = await schema.execute(
        MY_WORKSPACE_QUERY,
        variable_values={"slug": "no-such-workspace"},
        context_value=Context(FakeMembershipService(membership=None), VIEWER_USER_ID),
    )
    unauthorized = await schema.execute(
        MY_WORKSPACE_QUERY,
        variable_values={"slug": "vector"},
        context_value=Context(FakeMembershipService(membership=None), VIEWER_USER_ID),
    )

    assert only_error(absent) == only_error(unauthorized)


async def test_the_refusal_does_not_echo_the_slug_that_was_asked_for():
    """An echoed slug is a reflected value in a message clients render."""
    result = await schema.execute(
        MY_WORKSPACE_QUERY,
        variable_values={"slug": "secret-customer-name"},
        context_value=Context(FakeMembershipService(membership=None), VIEWER_USER_ID),
    )

    assert "secret-customer-name" not in str(only_error(result))


async def test_an_unexpected_failure_is_not_dressed_up_as_a_missing_workspace():
    """Only WorkspaceAccessDeniedError is translated. A dropped connection
    reaching a client as "Workspace not found" would send them to look for
    a workspace that is there."""
    result = await schema.execute(
        MY_WORKSPACE_QUERY,
        variable_values={"slug": "vector"},
        context_value=Context(BrokenMembershipService(), VIEWER_USER_ID),
    )

    formatted = only_error(result)

    assert formatted["message"] == MASKED_ERROR_MESSAGE
    assert formatted["message"] != WORKSPACE_NOT_FOUND_MESSAGE
    assert "extensions" not in formatted


# --- what an identified caller gets -----------------------------------


async def test_the_viewer_id_comes_from_the_context_and_not_the_document():
    """The resolver takes no user argument, so this is the only source."""
    service = FakeMembershipService(memberships=[make_membership()])

    result = await schema.execute(
        MY_WORKSPACES_QUERY,
        context_value=Context(service, VIEWER_USER_ID),
    )

    assert result.errors is None
    assert service.calls == [{"method": "list_for_user", "user_id": VIEWER_USER_ID}]


async def test_a_membership_maps_onto_the_nested_workspace_and_role():
    service = FakeMembershipService(
        memberships=[
            make_membership(slug="vector", name="Vector", role="owner"),
            make_membership(slug="acme", name="Acme", role="member"),
        ]
    )

    result = await schema.execute(
        MY_WORKSPACES_QUERY,
        context_value=Context(service, VIEWER_USER_ID),
    )

    assert result.errors is None
    assert result.data == {
        "myWorkspaces": [
            {
                "workspace": {
                    "id": str(WORKSPACE_ID),
                    "slug": "vector",
                    "name": "Vector",
                },
                "role": "OWNER",
                "createdAt": CREATED_AT.isoformat(),
            },
            {
                "workspace": {
                    "id": str(WORKSPACE_ID),
                    "slug": "acme",
                    "name": "Acme",
                },
                "role": "MEMBER",
                "createdAt": CREATED_AT.isoformat(),
            },
        ]
    }


@pytest.mark.parametrize(
    ("role", "expected"),
    [("member", "MEMBER"), ("admin", "ADMIN"), ("owner", "OWNER")],
)
async def test_every_role_crosses_the_boundary_as_its_enum_value(role, expected):
    service = FakeMembershipService(membership=make_membership(role=role))

    result = await schema.execute(
        MY_WORKSPACE_QUERY,
        variable_values={"slug": "vector"},
        context_value=Context(service, VIEWER_USER_ID),
    )

    assert result.errors is None
    assert result.data is not None
    assert result.data["myWorkspace"]["role"] == expected


async def test_a_role_the_schema_cannot_name_fails_rather_than_guessing():
    """A stored role missing from the enum is a bug, and it errors.

    The masked response is the correct outcome: the alternative is telling
    a client someone holds a role the database does not say they hold.
    """
    service = FakeMembershipService(membership=make_membership(role="superuser"))

    result = await schema.execute(
        MY_WORKSPACE_QUERY,
        variable_values={"slug": "vector"},
        context_value=Context(service, VIEWER_USER_ID),
    )

    assert only_error(result)["message"] == MASKED_ERROR_MESSAGE


async def test_the_slug_reaches_the_service_exactly_as_the_client_wrote_it():
    """No trimming, no lowercasing on the way down."""
    for slug in ("  vector  ", "VECTOR", "Vector", "x' OR TRUE --"):
        service = FakeMembershipService(membership=make_membership())

        await schema.execute(
            MY_WORKSPACE_QUERY,
            variable_values={"slug": slug},
            context_value=Context(service, VIEWER_USER_ID),
        )

        assert service.calls == [
            {
                "method": "membership_for_slug",
                "slug": slug,
                "user_id": VIEWER_USER_ID,
            }
        ]


async def test_a_viewer_with_no_memberships_gets_an_empty_list_not_an_error():
    result = await schema.execute(
        MY_WORKSPACES_QUERY,
        context_value=Context(FakeMembershipService(memberships=[]), VIEWER_USER_ID),
    )

    assert result.errors is None
    assert result.data == {"myWorkspaces": []}
