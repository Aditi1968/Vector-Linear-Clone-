"""Issue templates, without a database.

A template is the one thing in this product that is attacker-controlled data
NAMING OTHER ROWS, written once and replayed later by other people. The
database refuses a cross-tenant reference on the way in -- that half is
tests/test_migration_020_db.py -- and this file covers the half above it:

  * validation rejects a bad draft before a connection is ever acquired, and
    reports every violation rather than the first;
  * a foreign-key refusal becomes the field error for the value the client
    sent, and any constraint not in that table stays an error;
  * applying a template resolves it under the APPLYING caller's scope and
    re-checks every id by going through `IssueService` and `LabelService`,
    rather than writing the stored values anywhere directly;
  * a team-scoped template cannot be applied into a different team.
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID

import asyncpg
import pytest
import strawberry

from app.domain.errors import ValidationError, WorkspaceAccessDeniedError
from app.domain.recurrence import RecurrenceEntity
from app.domain.templates import IssueTemplateDraft, IssueTemplateEntity
from app.domain.tenancy import AuthorizedWorkspaceScope
from app.graphql.schema import build_schema
from app.graphql.scope import WORKSPACE_NOT_FOUND_MESSAGE
from app.graphql.viewer import UNAUTHENTICATED_MESSAGE
from app.services.labels import LABELS_PER_ISSUE_MAX
from app.services.templates import (
    LABELS_PER_TEMPLATE_MAX,
    TEMPLATES_MAX,
    TemplateService,
)

from tests.conftest import (
    ExplodingPool,
    FakePool,
    graphql_context,
    make_entity,
)


WORKSPACE_ID = UUID("00000000-0000-7000-8000-0000000000a1")
TEAM_ID = UUID("00000000-0000-7000-8000-0000000000a2")
OTHER_TEAM_ID = UUID("00000000-0000-7000-8000-0000000000a3")
TEMPLATE_ID = UUID("00000000-0000-7000-8000-0000000000d1")
ASSIGNEE_ID = UUID("00000000-0000-7000-8000-0000000000e2")
PROJECT_ID = UUID("00000000-0000-7000-8000-0000000000f1")
CYCLE_ID = UUID("00000000-0000-7000-8000-0000000000f2")
LABEL_ONE = UUID("00000000-0000-7000-8000-000000000101")
LABEL_TWO = UUID("00000000-0000-7000-8000-000000000102")
VIEWER_ID = UUID("00000000-0000-7000-8000-0000000000e1")

BASE_TIME = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

SCOPE = AuthorizedWorkspaceScope(
    workspace_id=WORKSPACE_ID,
    user_id=VIEWER_ID,
    role="member",
)

schema = build_schema("test")


def draft(**overrides) -> IssueTemplateDraft:
    return IssueTemplateDraft(**({"name": "Bug report"} | overrides))


def template(**overrides) -> IssueTemplateEntity:
    fields = {
        "id": TEMPLATE_ID,
        "team_id": None,
        "name": "Bug report",
        "title": "Bug: ",
        "description": "Steps to reproduce",
        "priority": 2,
        "estimate": 3,
        "assignee_id": None,
        "project_id": None,
        "cycle_id": None,
        "label_ids": (),
        # Nearly every template has none, and the schedule is read by a
        # separate statement anyway -- see `TemplateService._with_recurrences`.
        "recurrence": None,
        "created_at": BASE_TIME,
        "updated_at": BASE_TIME,
    }

    return IssueTemplateEntity(**(fields | overrides))


class RecordingRepository:
    """Records the drafts it was handed and replays one canned template."""

    def __init__(self, entity: IssueTemplateEntity | None = None):
        self.entity = entity if entity is not None else template()
        self.drafts: list[IssueTemplateDraft] = []
        self.gets: list[dict] = []
        self.lists: list[dict] = []

    async def create(self, connection, *, scope, draft):
        self.drafts.append(draft)

        return self.entity

    async def update(self, connection, *, scope, template_id, draft):
        self.drafts.append(draft)

        return self.entity

    async def get(self, connection, *, scope, template_id):
        self.gets.append({"scope": scope, "template_id": template_id})

        return self.entity

    async def list_for_team(self, connection, *, scope, team_id, limit):
        self.lists.append({"scope": scope, "team_id": team_id, "limit": limit})

        return [self.entity]


class RecordingRecurrenceRepository:
    """The schedule half, recorded, with nothing scheduled by default.

    Deliberately answers an empty mapping from `find_many_for_templates`, which
    is what almost every template's schedule is -- so a test about the shape of
    a template save is not also a test about recurrences.
    """

    def __init__(self, schedules: dict | None = None):
        self.schedules = schedules if schedules is not None else {}
        self.upserts: list[dict] = []
        self.deletes: list[dict] = []
        self.lookups: list[dict] = []

    async def upsert(
        self, connection, *, scope, template_id, team_id, rule, next_run_on
    ):
        self.upserts.append(
            {
                "scope": scope,
                "template_id": template_id,
                "team_id": team_id,
                "rule": rule,
                "next_run_on": next_run_on,
            }
        )

        return RecurrenceEntity(
            template_id=template_id,
            team_id=team_id,
            frequency=rule.frequency,
            interval_count=rule.interval_count,
            weekdays=rule.weekdays,
            day_of_month=rule.day_of_month,
            starts_on=rule.starts_on,
            due_in_days=rule.due_in_days,
            next_run_on=next_run_on,
        )

    async def delete(self, connection, *, scope, template_id):
        self.deletes.append({"scope": scope, "template_id": template_id})

        return template_id in self.schedules

    async def find_many_for_templates(self, connection, *, scope, template_ids):
        self.lookups.append({"scope": scope, "template_ids": list(template_ids)})

        return {
            template_id: self.schedules[template_id]
            for template_id in template_ids
            if template_id in self.schedules
        }


class RecordingIssueService:
    """The real service's four apply-path methods, recorded."""

    def __init__(self):
        self.created: list[dict] = []
        self.projects: list[dict] = []
        self.cycles: list[dict] = []
        self.issue = make_entity(1)

    async def create(self, **kwargs):
        self.created.append(kwargs)

        return self.issue

    async def set_project(self, **kwargs):
        self.projects.append(kwargs)

        return self.issue

    async def set_cycle(self, **kwargs):
        self.cycles.append(kwargs)

        return self.issue


class RecordingLabelService:
    def __init__(self):
        self.attached: list[dict] = []

    async def attach(self, **kwargs):
        self.attached.append(kwargs)


def service(
    *,
    repository=None,
    recurrences=None,
    issues=None,
    labels=None,
    pool=None,
) -> TemplateService:
    return TemplateService(
        pool=pool if pool is not None else FakePool(),
        repository=repository if repository is not None else RecordingRepository(),
        recurrences=(
            recurrences if recurrences is not None else RecordingRecurrenceRepository()
        ),
        issues=issues if issues is not None else RecordingIssueService(),
        labels=labels if labels is not None else RecordingLabelService(),
    )


# --- validation happens before a connection ---------------------------


@pytest.mark.parametrize(
    ("bad", "expected"),
    [
        (draft(name=""), [("name", "OUT_OF_RANGE")]),
        (draft(name="x" * 101), [("name", "OUT_OF_RANGE")]),
        (draft(title="x" * 501), [("title", "TOO_LONG")]),
        (draft(description="x" * 16385), [("description", "TOO_LONG")]),
        (draft(priority=5), [("priority", "OUT_OF_RANGE")]),
        (draft(priority=-1), [("priority", "OUT_OF_RANGE")]),
        (draft(estimate=-1), [("estimate", "OUT_OF_RANGE")]),
        (draft(cycle_id=CYCLE_ID), [("cycleId", "TEAM_REQUIRED")]),
    ],
    ids=[
        "empty name",
        "long name",
        "long title",
        "long description",
        "priority above range",
        "priority below range",
        "negative estimate",
        "cycle with no team",
    ],
)
async def test_a_bad_draft_is_refused_without_acquiring_a_connection(bad, expected):
    """`ExplodingPool` fails the test if anything acquires.

    A request that cannot be satisfied by any state of the database must not
    cost a connection -- the property `IssueService._validate_create` has, and
    the reason the cycle-without-team rule is checked here rather than left to
    `issue_templates_cycle_requires_team`.
    """
    pool = ExplodingPool()

    with pytest.raises(ValidationError) as raised:
        await service(pool=pool).create(scope=SCOPE, draft=bad)

    assert [(issue.field, issue.code) for issue in raised.value.issues] == expected
    assert pool.acquire_count == 0


async def test_every_violation_is_reported_in_a_deterministic_order():
    """A form with four bad fields reports four, in the input type's order,
    so a client can render them all at once and rely on the sequence."""
    with pytest.raises(ValidationError) as raised:
        await service(pool=ExplodingPool()).create(
            scope=SCOPE,
            draft=draft(
                name="",
                title="x" * 501,
                priority=9,
                estimate=-2,
            ),
        )

    assert [issue.field for issue in raised.value.issues] == [
        "name",
        "title",
        "priority",
        "estimate",
    ]


async def test_duplicate_labels_are_deduplicated_rather_than_refused():
    """A set spelled carelessly is not a mistake worth an error.

    Left alone, `issue_template_labels_pkey` would turn it into a unique
    violation with no field attached -- which the client could not act on and
    which this layer would have no honest way to translate.
    """
    repository = RecordingRepository()

    await service(repository=repository).create(
        scope=SCOPE,
        draft=draft(label_ids=(LABEL_TWO, LABEL_ONE, LABEL_TWO)),
    )

    assert repository.drafts[0].label_ids == (LABEL_ONE, LABEL_TWO)


def test_a_template_may_not_carry_more_labels_than_an_issue_may_wear():
    """The two caps are pinned equal rather than left to agree by habit.

    A template that applied more labels than `LabelService` accepts would be
    one whose apply always fails part way through -- with the issue already
    filed, since applying is not one transaction.
    """
    assert LABELS_PER_TEMPLATE_MAX == LABELS_PER_ISSUE_MAX


# --- which refusals are the client's to fix ---------------------------


class RefusingRepository:
    """A repository whose write violates one named foreign key."""

    def __init__(self, constraint: str):
        self._constraint = constraint

    async def create(self, connection, *, scope, draft):
        error = asyncpg.ForeignKeyViolationError("refused")
        error.constraint_name = self._constraint

        raise error


@pytest.mark.parametrize(
    ("constraint", "field", "code"),
    [
        ("issue_templates_team_fk", "teamId", "NOT_FOUND"),
        ("issue_templates_assignee_fk", "assigneeId", "NOT_A_MEMBER"),
        ("issue_templates_project_fk", "projectId", "NOT_FOUND"),
        ("issue_templates_cycle_fk", "cycleId", "NOT_FOUND"),
        ("issue_template_labels_label_fk", "labelIds", "NOT_FOUND"),
    ],
)
async def test_each_composite_key_becomes_the_field_error_it_is_about(
    constraint, field, code
):
    """Five keys, five halves of a form, one message each.

    None of them distinguishes "belongs to another workspace" from "does not
    exist", and that is the point: a template is the one place a client can
    park an id and come back to it, so an error that confirmed the id was real
    would be a cross-tenant existence oracle with a long shelf life.
    """
    subject = service(repository=RefusingRepository(constraint))

    with pytest.raises(ValidationError) as raised:
        await subject.create(scope=SCOPE, draft=draft(team_id=TEAM_ID))

    assert [(issue.field, issue.code) for issue in raised.value.issues] == [
        (field, code)
    ]


async def test_a_constraint_nobody_predicted_stays_an_error():
    """CLAUDE.md's rule, asserted rather than assumed.

    `issue_templates_workspace_fk` takes its value from the authorized scope
    rather than from the request, so violating it is a defect here -- not
    something a client could correct by sending a different field.
    """
    subject = service(repository=RefusingRepository("issue_templates_workspace_fk"))

    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await subject.create(scope=SCOPE, draft=draft())


# --- applying re-checks rather than trusting --------------------------


async def test_applying_reads_the_template_under_the_callers_own_scope():
    """The tenancy of the whole apply path in one assertion.

    The scope handed to the read is the one the resolver AUTHORIZED, never a
    workspace taken from the stored row -- so a template id from another
    tenant resolves to nothing and no defaults are ever in hand to re-check.
    """
    repository = RecordingRepository()

    await service(repository=repository).apply(
        scope=SCOPE,
        template_id=TEMPLATE_ID,
        team_id=TEAM_ID,
        actor_id=VIEWER_ID,
    )

    assert repository.gets == [{"scope": SCOPE, "template_id": TEMPLATE_ID}]


async def test_a_template_this_workspace_does_not_have_is_not_found():
    class Missing(RecordingRepository):
        async def get(self, connection, *, scope, template_id):
            return None

    with pytest.raises(ValidationError) as raised:
        await service(repository=Missing()).apply(
            scope=SCOPE,
            template_id=TEMPLATE_ID,
            team_id=TEAM_ID,
        )

    assert [(issue.field, issue.code) for issue in raised.value.issues] == [
        ("templateId", "NOT_FOUND")
    ]


async def test_a_team_scoped_template_cannot_be_filed_into_another_team():
    """Refused here rather than left to `issues_cycle_fk` to refuse obscurely.

    Both values are already in hand, so it is a property of the arguments
    alone -- and the failure it prevents is a template's cycle silently
    landing on an issue in a team that cycle does not belong to.
    """
    repository = RecordingRepository(template(team_id=TEAM_ID))

    with pytest.raises(ValidationError) as raised:
        await service(repository=repository).apply(
            scope=SCOPE,
            template_id=TEMPLATE_ID,
            team_id=OTHER_TEAM_ID,
        )

    assert [(issue.field, issue.code) for issue in raised.value.issues] == [
        ("teamId", "TEAM_MISMATCH")
    ]


async def test_a_workspace_template_may_be_filed_into_any_team():
    """The other side of the same rule, so it is not merely a ban.

    A shared "Bug report" is the case the nullable `team_id` exists for.
    """
    issues = RecordingIssueService()

    await service(repository=RecordingRepository(), issues=issues).apply(
        scope=SCOPE,
        template_id=TEMPLATE_ID,
        team_id=OTHER_TEAM_ID,
    )

    assert issues.created[0]["team_id"] == OTHER_TEAM_ID


async def test_every_stored_reference_goes_through_the_service_that_owns_it():
    """The re-check, spelled as which code the values reach.

    Nothing here writes an assignee, a project, a cycle or a label directly.
    Each is handed to the service whose composite foreign key checks it
    against the workspace of the issue being created -- which is what makes
    this path correct without depending on the save path having been correct.
    """
    entity = template(
        team_id=TEAM_ID,
        assignee_id=ASSIGNEE_ID,
        project_id=PROJECT_ID,
        cycle_id=CYCLE_ID,
        label_ids=(LABEL_ONE, LABEL_TWO),
    )
    issues = RecordingIssueService()
    labels = RecordingLabelService()

    await service(
        repository=RecordingRepository(entity),
        issues=issues,
        labels=labels,
    ).apply(
        scope=SCOPE,
        template_id=TEMPLATE_ID,
        team_id=TEAM_ID,
        actor_id=VIEWER_ID,
    )

    assert issues.created[0]["assignee_id"] == ASSIGNEE_ID
    assert issues.created[0]["creator_id"] == VIEWER_ID
    assert issues.projects[0]["project_id"] == PROJECT_ID
    # A template carries a project and never a milestone: a milestone is a
    # position INSIDE a plan, which a shape filed months later cannot claim.
    assert issues.projects[0]["milestone_id"] is None
    assert issues.cycles[0]["cycle_id"] == CYCLE_ID
    assert [call["label_id"] for call in labels.attached] == [LABEL_ONE, LABEL_TWO]

    # Every one of them under the scope the resolver authorized.
    assert {call["scope"] for call in issues.created} == {SCOPE}


async def test_a_template_with_no_project_or_cycle_makes_no_extra_calls():
    """The ordinary template is one write, not four.

    Applying is not atomic -- see `TemplateService.apply` -- so each step that
    does not have to run is one fewer way for a partial apply to happen.
    """
    issues = RecordingIssueService()
    labels = RecordingLabelService()

    await service(issues=issues, labels=labels).apply(
        scope=SCOPE,
        template_id=TEMPLATE_ID,
        team_id=TEAM_ID,
    )

    assert issues.projects == []
    assert issues.cycles == []
    assert labels.attached == []


async def test_a_title_override_wins_over_the_templates_own():
    """ "Deploy checklist: 4.2" filed from a "Deploy checklist" template, which
    is the ordinary reason to override anything."""
    issues = RecordingIssueService()

    await service(issues=issues).apply(
        scope=SCOPE,
        template_id=TEMPLATE_ID,
        team_id=TEAM_ID,
        title="Deploy checklist: 4.2",
    )

    assert issues.created[0]["title"] == "Deploy checklist: 4.2"


async def test_a_template_with_no_title_reaches_the_issues_own_title_rule():
    """An empty string, not a second copy of "Title is required" here.

    One answer to "what is a valid title" is what keeps the template path and
    `issueCreate` from drifting into two.
    """
    issues = RecordingIssueService()

    await service(
        repository=RecordingRepository(template(title=None)), issues=issues
    ).apply(
        scope=SCOPE,
        template_id=TEMPLATE_ID,
        team_id=TEAM_ID,
    )

    assert issues.created[0]["title"] == ""


async def test_a_template_with_no_priority_files_at_the_columns_default():
    """`issues.priority` is NOT NULL with a default of 0, so "no opinion" is
    a request for that default rather than for a null the column cannot
    hold."""
    issues = RecordingIssueService()

    await service(
        repository=RecordingRepository(template(priority=None)),
        issues=issues,
    ).apply(scope=SCOPE, template_id=TEMPLATE_ID, team_id=TEAM_ID)

    assert issues.created[0]["priority"] == 0


# --- listing ----------------------------------------------------------


async def test_listing_passes_the_cap_and_the_team_through():
    """A ceiling rather than a page size. See TEMPLATES_MAX for when that
    stops being the right shape."""
    repository = RecordingRepository()

    await service(repository=repository).list(scope=SCOPE, team_id=TEAM_ID)

    assert repository.lists == [
        {"scope": SCOPE, "team_id": TEAM_ID, "limit": TEMPLATES_MAX}
    ]


# --- who may read and write a template --------------------------------


TEMPLATES = """
query Templates($slug: String!) {
  issueTemplates(workspaceSlug: $slug) { id name }
}
"""

TEMPLATE = """
query Template($slug: String!, $id: UUID!) {
  issueTemplate(workspaceSlug: $slug, id: $id) { id name }
}
"""

CREATE = """
mutation Create($input: IssueTemplateCreateInput!) {
  issueTemplateCreate(input: $input) { template { id } errors { field code } }
}
"""

UPDATE = """
mutation Update($input: IssueTemplateUpdateInput!) {
  issueTemplateUpdate(input: $input) { template { id } errors { field code } }
}
"""

DELETE = """
mutation Delete($input: IssueTemplateDeleteInput!) {
  issueTemplateDelete(input: $input) { id errors { field code } }
}
"""

APPLY = """
mutation Apply($input: IssueCreateFromTemplateInput!) {
  issueCreateFromTemplate(input: $input) { issue { id } errors { field code } }
}
"""

FIELDS = {"name": "Bug report"}

DOCUMENTS = [
    (TEMPLATES, {"slug": "acme"}),
    (TEMPLATE, {"slug": "acme", "id": str(TEMPLATE_ID)}),
    (CREATE, {"input": {"workspaceSlug": "acme", "template": FIELDS}}),
    (
        UPDATE,
        {
            "input": {
                "workspaceSlug": "acme",
                "id": str(TEMPLATE_ID),
                "template": FIELDS,
            }
        },
    ),
    (DELETE, {"input": {"workspaceSlug": "acme", "id": str(TEMPLATE_ID)}}),
    (
        APPLY,
        {
            "input": {
                "workspaceSlug": "acme",
                "templateId": str(TEMPLATE_ID),
                "teamId": str(TEAM_ID),
            }
        },
    ),
]
DOCUMENT_IDS = ["list", "get", "create", "update", "delete", "apply"]


class RefusingMembershipService:
    def __init__(self):
        self.calls: list[dict] = []

    async def authorized_scope_for_slug(self, *, slug, user_id):
        self.calls.append({"slug": slug, "user_id": user_id})

        raise WorkspaceAccessDeniedError()


def viewer_context(viewer_id, **services):
    context = graphql_context(**services)

    async def viewer():
        return None if viewer_id is None else SimpleNamespace(id=viewer_id)

    context.viewer = viewer

    return context


@pytest.mark.parametrize(("document", "variables"), DOCUMENTS, ids=DOCUMENT_IDS)
async def test_an_anonymous_caller_is_refused_before_any_lookup(document, variables):
    """Fails closed and fails first, on all six fields.

    The membership service is wired and asserted UNTOUCHED: a resolver that
    refused after resolving a workspace would give the same response having
    already performed a protected lookup for a request with no identity.
    """
    memberships = RefusingMembershipService()

    result = await schema.execute(
        document,
        variable_values=variables,
        context_value=viewer_context(None, membership_service=memberships),
    )

    assert result.errors is not None
    assert result.errors[0].formatted["message"] == UNAUTHENTICATED_MESSAGE
    assert memberships.calls == [], "no lookup may happen for an anonymous caller"


@pytest.mark.parametrize(("document", "variables"), DOCUMENTS, ids=DOCUMENT_IDS)
async def test_a_workspace_the_viewer_does_not_belong_to_is_not_found(
    document, variables
):
    """The template service is an `UnusedService`, so reaching it at all fails
    with a sentence naming it -- which is what proves the refusal happens
    before any template is read, rather than after a query returning
    nothing."""
    memberships = RefusingMembershipService()

    result = await schema.execute(
        document,
        variable_values=variables,
        context_value=viewer_context(VIEWER_ID, membership_service=memberships),
    )

    assert result.errors is not None
    assert result.errors[0].formatted["message"] == WORKSPACE_NOT_FOUND_MESSAGE
    assert result.errors[0].formatted["extensions"] == {"code": "NOT_FOUND"}
    assert memberships.calls == [{"slug": "acme", "user_id": VIEWER_ID}]


def test_no_template_field_accepts_a_workspace_id():
    """A slug selects WHAT is asked about; it never selects who is asking.

    An `workspaceId` argument would be a tenant the client chose, checked
    against nothing -- which is the one thing every field in this feature is
    built to prevent.
    """
    sdl = strawberry.printer.print_schema(schema)

    fields = [
        line
        for line in sdl.splitlines()
        if "issueTemplate" in line or "issueCreateFromTemplate" in line
    ]

    # Six mutations and two queries. Asserted rather than assumed, so this
    # test cannot pass on a schema that lost the feature entirely -- and the
    # count moved when migration 029 added the two recurrence mutations, which
    # is the assertion doing its job rather than an inconvenience.
    assert len(fields) == 8, fields
    assert not any("workspaceId" in line for line in fields)
