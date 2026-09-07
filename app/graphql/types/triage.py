"""Transport types for the triage queue.

Nothing here imports `IssueType`, and the entry in the queue therefore exposes
an `IssueSummary` rather than a full `Issue`. That is not a shortcut: the
summary type exists so that the graph this schema publishes stays ACYCLIC by
construction -- nothing reachable from it returns an issue -- and a triage
connection that returned `Issue` would put a second door onto the whole issue
subtree, reached from a list, with the same fan-out `Issue.children` is
deliberately bounded to avoid.

The exception is the mutation payloads, which DO carry `Issue`. A mutation
answers about one row the client is looking at, so the cost is one issue's
fields rather than a page of them, and the client needs the state and labels it
just changed.
"""

from datetime import datetime

import strawberry

from app.domain.triage import TriageIssuePage
from app.graphql.types.errors import ValidationErrorType
from app.graphql.types.issue import IssueType
from app.graphql.types.pagination import PageInfo
from app.graphql.types.relations import IssueSummaryType


# Page size for `triageIssues` when a document does not say.
#
# Twenty-five rather than the fifty `Query.issues` defaults to. A triage queue
# is worked through one item at a time -- somebody reads each one and decides
# -- so a screenful is the honest default, and `app.graphql.limits` prices this
# field at (page size) x (fields below it) whether or not the client meant to
# ask for fifty.
DEFAULT_TRIAGE_FIRST = 25


@strawberry.type(name="TriageIssue")
class TriageIssueType:
    """One issue waiting in a team's queue, and how long it has waited.

    `enteredAt` is beside the issue rather than on it, because it is a fact
    about the QUEUE ENTRY and not about the issue: the same issue can leave a
    queue and enter another one, and the column is NULL for every issue that is
    not waiting anywhere. Putting it on `Issue` would add a field that is null
    for almost every row the product ever renders.
    """

    issue: IssueSummaryType
    entered_at: datetime = strawberry.field(
        description=(
            "When this issue entered the queue. The queue is ordered by it, "
            "oldest first, so this is also the issue's position in the list."
        )
    )


@strawberry.type
class TriageIssueConnection:
    nodes: list[TriageIssueType]
    page_info: PageInfo

    @classmethod
    def from_domain(cls, page: TriageIssuePage) -> "TriageIssueConnection":
        return cls(
            nodes=[
                TriageIssueType(
                    issue=IssueSummaryType.from_entity(node.issue),
                    entered_at=node.entered_at,
                )
                for node in page.nodes
            ],
            page_info=PageInfo(
                has_next_page=page.has_next_page,
                end_cursor=page.end_cursor,
            ),
        )


@strawberry.type
class TriagePayload:
    """The answer to every triage mutation.

    One payload for all five, because all five answer the same question: here
    is the issue as it now is, or here is what was wrong with the request. Five
    identical types would be five names for one shape and every client would
    have to handle each -- the argument `LabelPayload` makes for serving two
    mutations, applied to five.

    The issue is nullable for the error case, and the errors list is empty on
    success. A payload with neither says only that nothing happened and leaves
    a client to guess why.
    """

    issue: IssueType | None
    errors: list[ValidationErrorType]
