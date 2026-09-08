from datetime import date
from uuid import UUID

import strawberry

from app.domain.issues import (
    UNSET,
    DueWindow,
    IssueFilter,
    IssueOrder,
    IssueOrderField,
    IssuePatch,
    OrderDirection,
    Unset,
)
from app.domain.teams import WorkflowStateCategory

# Imported for its side effect, exactly as `WorkflowStateCategoryType` below
# is and for the same reason: `types/issue.py` is where `strawberry.enum`
# annotates DueWindow with its GraphQL definition, and a field referencing the
# bare class before that has run makes Strawberry mint a SECOND definition for
# the same name -- at which point the schema refuses to build.
from app.graphql.types.issue import DueWindowType  # noqa: F401

# Imported for its side effect, not for the name. `types/team.py` is where
# `strawberry.enum` annotates WorkflowStateCategory with its GraphQL
# definition, and a field referencing the bare class before that has run
# makes Strawberry mint a SECOND definition for the same name -- at which
# point the schema refuses to build. Whether that module happens to be
# imported first is a question about import order, which is not a thing to
# leave to luck.
from app.graphql.types.team import WorkflowStateCategoryType  # noqa: F401


@strawberry.input
class IssueCreateInput:
    """The fields a client may set when filing an issue.

    Every optional field defaults to a plain `None` rather than to UNSET,
    and the difference from IssueUpdateInput below is not an inconsistency.
    On a create there is no prior value for "leave it alone" to preserve, so
    "not supplied" and "supplied as null" describe the same resulting row --
    one sentinel would be distinguishing two things that cannot differ.

    There is deliberately no `workflowStateId`. A new issue goes wherever
    new work goes on its team -- the team's `unstarted` state, resolved
    server-side -- and filing straight into some other state is a move,
    which `IssueUpdateInput` already expresses. Accepting one here would be
    a second path into the same column, and the only one of the two that
    would have to answer what `completedAt` should be for an issue born
    complete.

    There is deliberately no `creatorId` and no `completedAt` either.
    Authorship is taken from whoever authenticated the request, because a
    client able to name the creator could forge it; `completedAt` is derived
    from the workflow state, and a client able to set it could put the two
    into a disagreement.
    """

    # Every workspace-scoped mutation names its tenant, and every mutation
    # that takes an `input` names it HERE rather than beside the input. One
    # place per operation, so a client never has to remember which mutations
    # spell it as an argument; the two that take no input at all
    # (`issueArchive`, `cycleDelete`) carry it as a field argument, because
    # inventing a one-field input object for them would be worse.
    #
    # A slug and not a workspace id, deliberately. CLAUDE.md forbids trusting
    # a workspace id from the frontend: the slug is a public string that
    # selects WHAT is being asked about, and `app.graphql.scope` decides
    # whether the session behind the request may act there.
    workspace_slug: str

    # Required, and never resolved server-side. A workspace has many teams and
    # every issue belongs to exactly one, so a server that picked a default
    # would be choosing where another tenant's work lands by a rule invisible
    # at the call site -- and the choice would be permanent, since the number
    # in `ENG-42` comes off the team's own counter. The client that knows
    # which team it means is the one that has to say so.
    #
    # A team from another workspace is refused by `issues_team_fk` against the
    # authorized workspace, inside the insert, rather than by a check here.
    team_id: UUID

    title: str
    description: str | None = None
    priority: int = 0
    assignee_id: UUID | None = None
    estimate: int | None = None
    due_date: date | None = None


@strawberry.input
class IssueUpdateInput:
    """A patch: every field is optional, and omission means "do not touch".

    `strawberry.UNSET` is the default on every field so that the three
    states a partial update needs stay distinct -- set to a value, set to
    null, and not mentioned. With a plain `None` default the last two
    collapse and there is no request that clears an assignee.

    Every field is declared nullable, including the three -- `title`,
    `priority`, `workflowStateId` -- whose columns are NOT NULL and for which
    "cleared" has no meaning. That is not the schema mis-describing them; it
    is the only shape GraphQL offers. An input field is required exactly when
    it is non-null and carries no default, so declaring them `String!` and
    `Int!` makes them mandatory on every patch -- which is the opposite of a
    patch -- and giving them a default makes an omission silently overwrite
    the stored value with it. Nullable-and-omittable is the only remaining
    option, and the explicit null it lets through is refused by
    `IssueService._validate_patch` as a field error rather than reaching a
    NOT NULL violation the client cannot read.

    The other four are nullable because clearing them is an ordinary edit.

    `workspaceSlug` is the one required field, and it is not part of the
    patch: it says WHICH workspace's issue is being edited, not what to
    change about it, so `to_patch` below does not carry it and an update that
    sets nothing else is still empty.
    """

    workspace_slug: str

    title: str | None = strawberry.UNSET
    description: str | None = strawberry.UNSET
    priority: int | None = strawberry.UNSET
    workflow_state_id: UUID | None = strawberry.UNSET
    assignee_id: UUID | None = strawberry.UNSET
    estimate: int | None = strawberry.UNSET
    due_date: date | None = strawberry.UNSET

    def to_patch(self) -> IssuePatch:
        """Translate Strawberry's sentinel onto the domain's own.

        Two sentinels rather than one, on purpose. `strawberry.UNSET` is a
        transport detail -- it exists because GraphQL distinguishes an
        absent argument from a null one -- and letting it travel inwards
        would put a Strawberry import in the domain, which CLAUDE.md rules
        out and which would make the same distinction untestable without a
        GraphQL request to carry it.
        """
        return IssuePatch(
            title=_patched(self.title),
            description=_patched(self.description),
            priority=_patched(self.priority),
            workflow_state_id=_patched(self.workflow_state_id),
            assignee_id=_patched(self.assignee_id),
            estimate=_patched(self.estimate),
            due_date=_patched(self.due_date),
        )


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


@strawberry.input(
    description=(
        "What narrows an issue list. Every field is optional and every one "
        "of them narrows: nothing here can widen a list beyond the "
        "workspace the request was authorized for, so an id belonging to "
        "another workspace selects nothing."
    )
)
class IssueFilterInput:
    """The filter, with omitted and explicitly-null meaning different things.

    Three fields -- `assigneeId`, `projectId`, `cycleId` -- name columns that
    are genuinely nullable, and for those an explicit `null` is a filter and
    not the absence of one: `assigneeId: null` is the unassigned issues,
    `cycleId: null` is the backlog. Omitting the field is what means "I am
    not filtering on this".

    That distinction has to be spelled somehow, because a nullable argument
    already spends `null` on "no filter" -- and this is the spelling GraphQL
    itself offers, the same one `IssueUpdateInput` above uses to tell
    "clear the assignee" from "leave the assignee alone". The alternative,
    a second boolean field beside each id, is two fields that can contradict
    each other.

    The catch is worth naming: a client that passes a nullable variable
    straight through -- `assigneeId: $maybeViewer` with nothing signed in --
    asks for the unassigned issues rather than for all of them. Send the
    field only when filtering.

    The other five name columns that are NOT NULL on every row, so "has
    none" is not a set to ask for; an explicit null there reads as no filter,
    exactly as it does everywhere else in this schema.
    """

    team_id: UUID | None = strawberry.UNSET
    assignee_id: UUID | None = strawberry.UNSET
    workflow_state_id: UUID | None = strawberry.UNSET
    state_category: WorkflowStateCategory | None = strawberry.UNSET
    label_id: UUID | None = strawberry.UNSET
    priority: int | None = strawberry.UNSET
    project_id: UUID | None = strawberry.UNSET
    cycle_id: UUID | None = strawberry.UNSET

    # The due-date filters. `due` is RELATIVE and the server resolves it, which
    # is the point: a client sending its own idea of today would give two
    # colleagues in two timezones two different lists from one screen. `NO_DUE
    # DATE` lives in that enum rather than as a null here, so there is one
    # spelling of the undated set instead of two that could be sent together.
    #
    # `dueAfter` and `dueBefore` are absolute and inclusive at both ends -- a
    # sprint window, a month, a range dragged on a calendar. All three narrow
    # and all three may be combined; a range that excludes itself selects
    # nothing rather than erroring, exactly as an id from another workspace
    # does.
    due: DueWindow | None = strawberry.UNSET
    due_after: date | None = strawberry.UNSET
    due_before: date | None = strawberry.UNSET

    def to_filter(self) -> IssueFilter:
        """Translate Strawberry's sentinel onto the domain's own.

        Two sentinels rather than one, for the reason `to_patch` above gives:
        `strawberry.UNSET` is a transport detail and letting it travel
        inwards would put a Strawberry import in the domain.
        """
        return IssueFilter(
            team_id=_present(self.team_id),
            assignee_id=_nullable(self.assignee_id),
            workflow_state_id=_present(self.workflow_state_id),
            state_category=_present(self.state_category),
            label_id=_present(self.label_id),
            priority=_present(self.priority),
            project_id=_nullable(self.project_id),
            cycle_id=_nullable(self.cycle_id),
            # `_present` on all three, so an explicit null reads as "no filter"
            # rather than as a request for the empty set. None of these columns
            # has a "has none" a null could mean: the undated issues are
            # `due: NO_DUE_DATE`, and a null bound on a range is not a range.
            due_window=_present(self.due),
            due_after=_present(self.due_after),
            due_before=_present(self.due_before),
        )


@strawberry.input(description="How an issue list is sorted.")
class IssueOrderInput:
    """A field and a direction, both defaulted to the list's historic order.

    Both are non-null with defaults, so a client may send `{field: PRIORITY}`
    and get the direction it did not name. Every ordering is completed by the
    issue's id as a tie-break -- see `IssueOrderField` -- which is what keeps
    it total, and a total order is what makes the cursor walk neither skip
    nor repeat rows.
    """

    field: IssueOrderField = IssueOrderField.CREATED_AT
    direction: OrderDirection = OrderDirection.DESC

    def to_order(self) -> IssueOrder:
        return IssueOrder(field=self.field, direction=self.direction)


def _present[T](value: T | None) -> T | Unset:
    """UNSET for both an absent field and an explicit null.

    For the filters whose column is NOT NULL, where "has none" selects
    nothing any state of the database could produce. A null there is a client
    saying nothing rather than a client asking for the empty set.
    """
    if value is strawberry.UNSET or value is None:
        return UNSET

    return value


def _nullable[T](value: T | None) -> T | None | Unset:
    """UNSET only for an absent field; an explicit null survives as None.

    For the three filters whose column is nullable, where None is the request
    for the rows that hold nothing -- unassigned, in no project, in no cycle.
    """
    if value is strawberry.UNSET:
        return UNSET

    return value
