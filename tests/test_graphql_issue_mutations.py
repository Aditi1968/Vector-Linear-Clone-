"""GraphQL transport tests for issueUpdate and issueArchive. No database.

The first test here is the one that matters, and it is a regression test for
a bug that every other layer would have reported as working.

`IssueUpdateInput` is a patch: every field is optional and omission means "do
not touch". GraphQL expresses optionality in exactly one way -- an input field
is required precisely when it is non-null and carries no default -- so the
three fields whose columns are NOT NULL (`title`, `priority`,
`workflowStateId`) cannot be declared `String!`/`Int!`/`UUID!` and still be
omittable. Declared that way they are mandatory on every patch, and a request
that means "change the estimate" is rejected by schema validation before any
resolver runs.

That failure is invisible from below. `IssueService._validate_patch` accepts a
one-field patch, the repository builds the right UPDATE for it, and the
service-level suites all pass -- because none of them go through a schema. It
is only visible by executing a document.

So these tests execute documents. The service is a fake that records the
patch it was handed, which is what makes "the transport preserved the
distinction between omitted and null" an assertion rather than an assumption.
"""

from uuid import UUID

import pytest

from app.domain.issues import UNSET, IssuePatch
from app.graphql.schema import build_schema
from app.repositories.issues import IssueRepository
from app.repositories.teams import TeamRepository
from app.services.issues import IssueService
from app.services.teams import TeamService

from tests.conftest import (
    TEST_WORKSPACE_SLUG,
    ExplodingPool,
    graphql_context,
    make_entity,
)


schema = build_schema("test")

ISSUE_ID = UUID("00000000-0000-7000-8000-000000000001")

UPDATE_MUTATION = """
mutation UpdateIssue($id: UUID!, $input: IssueUpdateInput!) {
  issueUpdate(id: $id, input: $input) {
    issue {
      id
      identifier
      number
    }
    errors {
      field
      code
      message
    }
  }
}
"""

ARCHIVE_MUTATION = """
mutation ArchiveIssue($slug: String!, $id: UUID!) {
  issueArchive(workspaceSlug: $slug, id: $id) {
    issue {
      id
      identifier
      archivedAt
    }
    errors {
      field
      code
      message
    }
  }
}
"""


class RecordingIssueService:
    """Records the patch it was handed and returns a fixed entity.

    `result` is what `update` and `archive` answer with; None stands for the
    issue not being there -- nonexistent, another tenant's, or archived.
    """

    def __init__(self, result=None):
        self._result = result
        self.patches: list[IssuePatch] = []
        self.archived: list[UUID] = []

    async def update(self, *, scope, issue_id, patch):
        self.patches.append(patch)

        return self._result

    async def archive(self, *, scope, issue_id):
        self.archived.append(issue_id)

        return self._result


def context(issue_service):
    """The real context, wired to one recording service.

    The viewer and the membership come from `graphql_context`'s defaults.
    Both are reached before the issue service now: `issueUpdate` authorizes
    the slug on its input and `issueArchive` the one beside its id.
    """
    return graphql_context(issue_service=issue_service)


async def _update(service, **input_fields):
    return await schema.execute(
        UPDATE_MUTATION,
        variable_values={
            "id": str(ISSUE_ID),
            # The slug is merged in rather than written into every case,
            # because it is a fact about the request and not about the patch
            # -- and a patch carrying only a slug must still read as empty.
            "input": {"workspaceSlug": TEST_WORKSPACE_SLUG} | input_fields,
        },
        context_value=context(service),
    )


@pytest.mark.parametrize(
    "field, value",
    [
        ("estimate", 5),
        ("title", "Renamed"),
        ("priority", 3),
        ("description", "described"),
        ("dueDate", "2026-03-14"),
        ("assigneeId", "00000000-0000-7000-8000-0000000000c1"),
        ("workflowStateId", "00000000-0000-7000-8000-0000000000d1"),
    ],
)
async def test_a_patch_naming_one_field_is_accepted(field, value):
    """Every field must be omittable on its own. This is the regression.

    Parameterized over all seven rather than asserted once, because the
    failure is per field: a field declared non-null with no default is
    mandatory, and one such field left in the input type makes every patch in
    the product carry it. Testing only `estimate` would pass while `title`,
    `priority` and `workflowStateId` were still required.
    """
    service = RecordingIssueService(result=make_entity(1))

    result = await _update(service, **{field: value})

    assert result.errors is None, f"omitting every field but {field} was rejected"
    assert result.data is not None
    assert result.data["issueUpdate"]["errors"] == []

    # And exactly one field arrived set; the other six stayed UNSET, which is
    # what "do not touch" is represented by.
    patch = service.patches[0]
    touched = [
        name
        for name in (
            "title",
            "description",
            "priority",
            "workflow_state_id",
            "assignee_id",
            "estimate",
            "due_date",
        )
        if getattr(patch, name) is not UNSET
    ]

    assert len(touched) == 1, f"{field} produced a patch touching {touched}"


async def test_an_explicit_null_clears_a_nullable_field(monkeypatch):
    """`assigneeId: null` must reach the service as None, not as UNSET.

    This is the other half of the distinction: if the transport collapsed
    null onto UNSET the request would silently do nothing, and there would be
    no way to unassign an issue at all.
    """
    service = RecordingIssueService(result=make_entity(1))

    result = await _update(service, assigneeId=None)

    assert result.errors is None
    assert service.patches[0].assignee_id is None
    assert service.patches[0].title is UNSET


async def test_an_explicit_null_on_a_not_null_column_is_a_field_error():
    """Refused by the service, because the schema cannot refuse it.

    Declaring `title` non-null would make GraphQL reject this before any
    resolver ran -- and would also make `title` mandatory on every patch,
    which is the bug this file exists for. Nullable-and-refused-here is the
    trade.

    The real service over a pool that refuses to be acquired: validation
    happens before any connection is taken, so reaching the pool at all would
    itself be the failure.
    """
    pool = ExplodingPool()
    service = IssueService(
        pool=pool,
        repository=IssueRepository(),
        teams=TeamService(pool=pool, repository=TeamRepository()),
    )

    result = await _update(service, title=None)

    assert result.errors is None
    assert result.data == {
        "issueUpdate": {
            "issue": None,
            "errors": [
                {
                    "field": "title",
                    "code": "NOT_NULLABLE",
                    "message": "title cannot be cleared",
                }
            ],
        }
    }
    assert pool.acquire_count == 0


async def test_an_empty_patch_is_refused():
    pool = ExplodingPool()
    service = IssueService(
        pool=pool,
        repository=IssueRepository(),
        teams=TeamService(pool=pool, repository=TeamRepository()),
    )

    result = await _update(service)

    assert result.errors is None
    assert result.data is not None
    assert result.data["issueUpdate"]["errors"] == [
        {
            "field": "input",
            "code": "EMPTY",
            "message": "At least one field must be provided",
        }
    ]
    assert pool.acquire_count == 0


async def test_updating_an_issue_that_is_not_there_reports_not_found():
    """One answer for nonexistent, another tenant's, and archived alike.

    A caller holding a guessed id who could tell those apart would have a way
    to enumerate other workspaces' issue ids one request at a time.
    """
    service = RecordingIssueService(result=None)

    result = await _update(service, title="Renamed")

    assert result.errors is None
    assert result.data == {
        "issueUpdate": {
            "issue": None,
            "errors": [
                {"field": "id", "code": "NOT_FOUND", "message": "Issue not found"}
            ],
        }
    }


async def test_archive_returns_the_archived_issue():
    """The mutation is named for what it does: it archives, it does not delete.

    A mutation called issueDelete that archived would be a lie in the SDL the
    frontend generates its client from.
    """
    entity = make_entity(4, id=ISSUE_ID)
    service = RecordingIssueService(result=entity)

    result = await schema.execute(
        ARCHIVE_MUTATION,
        variable_values={"slug": TEST_WORKSPACE_SLUG, "id": str(ISSUE_ID)},
        context_value=context(service),
    )

    assert result.errors is None
    assert result.data is not None
    assert result.data["issueArchive"]["errors"] == []
    assert result.data["issueArchive"]["issue"]["id"] == str(ISSUE_ID)
    assert service.archived == [ISSUE_ID]


async def test_archiving_an_issue_that_is_not_there_reports_not_found():
    service = RecordingIssueService(result=None)

    result = await schema.execute(
        ARCHIVE_MUTATION,
        variable_values={"slug": TEST_WORKSPACE_SLUG, "id": str(ISSUE_ID)},
        context_value=context(service),
    )

    assert result.errors is None
    assert result.data == {
        "issueArchive": {
            "issue": None,
            "errors": [
                {"field": "id", "code": "NOT_FOUND", "message": "Issue not found"}
            ],
        }
    }


async def test_the_identifier_reaches_the_client():
    """The one thing the browser smoke reported as missing.

    `teams.key` and `issues.number` both existed since 005 and neither
    reached the API, so nothing the product rendered could name an issue the
    way everyone outside the database names it.
    """
    entity = make_entity(7, id=ISSUE_ID)
    service = RecordingIssueService(result=entity)

    result = await _update(service, title="Renamed")

    assert result.errors is None
    assert result.data is not None
    assert result.data["issueUpdate"]["issue"] == {
        "id": str(ISSUE_ID),
        "identifier": f"{entity.team_key}-7",
        "number": 7,
    }
