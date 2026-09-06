from datetime import date
from uuid import UUID

import strawberry

from app.domain.issues import UNSET, IssuePatch, Unset


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
    """

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
