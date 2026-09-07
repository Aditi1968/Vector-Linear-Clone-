from uuid import UUID

import strawberry
from strawberry.types import Info

from app.domain.errors import ValidationError
from app.graphql.errors import bad_user_input
from app.graphql.scope import authorized_scope
from app.graphql.types.triage import DEFAULT_TRIAGE_FIRST, TriageIssueConnection


@strawberry.type
class TriageQuery:
    """The read half of triage, merged into the root Query by app.graphql.schema.

    A separate class rather than more fields on the issues query, for the
    reason `merge_types` is used at all: the root is a junction every feature
    hangs off, and a junction that is also one file is a file every branch
    conflicts in.
    """

    @strawberry.field
    async def triage_issues(
        self,
        info: Info,
        workspace_slug: str,
        team_id: UUID,
        first: int = DEFAULT_TRIAGE_FIRST,
        after: str | None = None,
    ) -> TriageIssueConnection:
        """One team's queue of unaccepted work, oldest first.

        `teamId` is required and there is no workspace-wide view. Triage is a
        team's inbox: two teams' queues merged into one list would be a screen
        nobody owns, and the per-team index migration 021 creates is what makes
        the read cheap in the first place.

        Ascending by arrival, unlike every other list in this product. A queue
        is worked from the front -- the thing that has waited longest is the
        thing somebody has to look at -- where an issue list is read newest
        first.

        A team from another workspace, and a team that does not exist, both
        answer with an empty page rather than an error. That equivalence is the
        isolation property: any distinguishable answer would tell a caller
        holding a guessed id that the team is real and simply not theirs.
        """
        scope = await authorized_scope(info, workspace_slug)

        try:
            page = await info.context.triage_service.queue(
                scope=scope,
                team_id=team_id,
                first=first,
                after=after,
            )
        except ValidationError as exc:
            # Only expected pagination input errors are translated. Anything
            # else (asyncpg failures, bugs) propagates as a real execution
            # error and is masked.
            raise bad_user_input("Invalid pagination arguments", exc) from None

        return TriageIssueConnection.from_domain(page)

    @strawberry.field
    async def triage_count(
        self,
        info: Info,
        workspace_slug: str,
        team_id: UUID,
    ) -> int:
        """How many issues are waiting in this team's queue.

        A field of its own rather than `totalCount` on the connection above, so
        that the sidebar badge -- which is the only thing that asks -- does not
        have to name a page it is not going to render. A document that selects
        only this runs one aggregate and no page walk.
        """
        scope = await authorized_scope(info, workspace_slug)

        return await info.context.triage_service.waiting_count(
            scope=scope,
            team_id=team_id,
        )
