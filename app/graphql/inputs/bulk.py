from datetime import date
from uuid import UUID

import strawberry

from app.domain.bulk import BULK_MAX, BulkIssuePatch
from app.domain.issues import UNSET, Unset


@strawberry.input(
    description=(
        "One change applied to many issues, or to none of them. Every field "
        "is optional and omitting one leaves that value alone on every "
        f"issue. At most {BULK_MAX} issues may be named; a longer list is "
        "refused rather than truncated."
    )
)
class IssueBulkUpdateInput:
    """A patch over a selection, with omission meaning "do not touch".

    `strawberry.UNSET` is the default on every field so that the three states a
    partial update needs stay distinct -- set to a value, set to null, and not
    mentioned. With a plain `None` default the last two collapse and there is
    no request that clears an assignee across a selection.

    There is deliberately no `title` and no `description`. Setting one title on
    many issues is not an edit anybody wants, and offering it would make the
    obvious mistake -- selecting a page and typing -- destroy work
    irreversibly.

    There is deliberately no `archive` flag either. Archiving is its own
    operation with its own authorisation story and its own irreversibility, so
    it is `issueBulkArchive` -- a different button deserves a different
    mutation.

    `projectId` and `milestoneId` must be sent together or not at all.
    `issues_milestone_fk` ties a milestone to the row's own project, so moving
    a selection into a project while leaving their milestones alone would leave
    each issue pointing at a milestone of the project it just left. Send
    `milestoneId: null` to place issues in a project with no milestone.
    """

    # A slug and not a workspace id, deliberately. CLAUDE.md forbids trusting a
    # workspace id from the frontend: the slug selects WHAT is being asked
    # about, and `app.graphql.scope` decides whether the session behind the
    # request may act there. It matters more here than anywhere else in the
    # schema, because every id below is checked against the workspace this
    # resolves to and against nothing else.
    workspace_slug: str

    # Every id here is attacker-controlled and none is trusted because another
    # one was fine. The service locks the whole list against this workspace and
    # refuses the batch -- all of it -- if any id fails to resolve. It is
    # `[UUID!]!` rather than a paginated argument because a client sends the
    # ids it selected; the bound is a refusal at the service, not a page size.
    issue_ids: list[UUID]

    workflow_state_id: UUID | None = strawberry.UNSET
    assignee_id: UUID | None = strawberry.UNSET
    priority: int | None = strawberry.UNSET
    estimate: int | None = strawberry.UNSET
    due_date: date | None = strawberry.UNSET

    project_id: UUID | None = strawberry.UNSET
    milestone_id: UUID | None = strawberry.UNSET

    cycle_id: UUID | None = strawberry.UNSET

    # Labels are lists rather than a single id, because "tag these as Bug and
    # Regression" is one act and three round trips would be three transactions
    # with two ways to end up half done.
    #
    # Adding a label an issue already wears is a no-op rather than an error:
    # nobody selecting a page has checked which rows already carry it. Removing
    # one no issue wears is a no-op for the same reason. What is NOT a no-op is
    # adding a label from an exclusive group to an issue that already has one
    # from that group -- that refuses the whole batch, because there is no
    # partial answer to it that anyone asked for.
    add_label_ids: list[UUID] = strawberry.field(default_factory=list)
    remove_label_ids: list[UUID] = strawberry.field(default_factory=list)

    def to_patch(self) -> BulkIssuePatch:
        """Translate Strawberry's sentinel onto the domain's own.

        Two sentinels rather than one, on purpose. `strawberry.UNSET` is a
        transport detail -- it exists because GraphQL distinguishes an absent
        argument from a null one -- and letting it travel inwards would put a
        Strawberry import in the domain, which CLAUDE.md rules out and which
        would make the same distinction untestable without a GraphQL request to
        carry it. `IssueUpdateInput.to_patch` says the same.
        """
        return BulkIssuePatch(
            workflow_state_id=_patched(self.workflow_state_id),
            assignee_id=_patched(self.assignee_id),
            priority=_patched(self.priority),
            estimate=_patched(self.estimate),
            due_date=_patched(self.due_date),
            project_id=_patched(self.project_id),
            milestone_id=_patched(self.milestone_id),
            cycle_id=_patched(self.cycle_id),
        )


@strawberry.input
class IssueBulkArchiveInput:
    """Which issues come off the board.

    Separate from the update input because archiving is a separate act -- see
    that type's note. An id that is already archived fails the WHOLE batch
    rather than counting as done: a client selecting a list it had already
    archived half of is working from a stale view, and telling it so is more
    useful than a success that moved nothing.

    An issue still waiting in a triage queue is refused too, and refused for
    the batch. Archiving unaccepted work would empty a team's incoming queue
    without anybody deciding anything.
    """

    workspace_slug: str
    issue_ids: list[UUID]


def _patched[T](value: T) -> T | Unset:
    """`strawberry.UNSET` becomes the domain UNSET; anything else passes.

    Compared with `is`, not with truthiness: `strawberry.UNSET` is falsy, as
    are 0, '' and None, each of which is a value a client can legitimately
    send. An `if not value` here would silently drop a priority of 0 and an
    estimate of 0 from every patch that set them.
    """
    if value is strawberry.UNSET:
        return UNSET

    return value
