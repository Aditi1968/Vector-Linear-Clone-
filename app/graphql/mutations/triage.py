import strawberry
from strawberry.types import Info

from app.domain.errors import ValidationError
from app.graphql.inputs.triage import (
    TriageAcceptInput,
    TriageChangeTeamInput,
    TriageDeclineInput,
    TriageEnterInput,
    TriageMarkDuplicateInput,
)
from app.graphql.scope import authorized_scope
from app.graphql.types.errors import ValidationErrorType
from app.graphql.types.issue import IssueType
from app.graphql.types.triage import TriagePayload


def _errors(exc: ValidationError) -> list[ValidationErrorType]:
    """The domain's structured issues, as the transport reports them."""
    return [ValidationErrorType.from_domain(issue) for issue in exc.issues]


@strawberry.type
class TriageMutation:
    """The write half of triage, merged into the root Mutation.

    Every resolver here is the same three lines: resolve the workspace, call
    the service, and turn the one expected exception into a payload. Nothing
    decides anything -- which issues are in a queue, which states a team has,
    whether a pair is already related are rules the service and the schema own
    between them, and a resolver that re-checked any of them would be a second
    copy that can disagree with the first.

    WHAT IS NOT HERE. A triage screen also assigns, prioritises, labels and
    files issues into projects, and none of those is a mutation on this class:
    they are `issueUpdate`, `issueLabelAttach` / `issueLabelDetach` and
    `issueSetProject`, which already exist and already carry their own
    validation, activity and tenancy rules. A `triageAssign` would be a second
    path into `issues.assignee_id`, and the second path is the one that forgets
    to write history.

    Every mutation requires an authenticated viewer who is a member of the
    workspace named in the input, because `authorized_scope` resolves the
    viewer first and the membership second.
    """

    @strawberry.mutation
    async def triage_enter(self, info: Info, input: TriageEnterInput) -> TriagePayload:
        """Move an issue into its team's triage queue.

        Nothing enters a queue automatically yet -- `issueCreate` does not
        consult a per-team setting, because no such setting exists. This is
        both the manual "move to triage" action and the seam an integration
        will use when one lands.
        """
        # Resolved before the try, and outside it. An unauthenticated caller,
        # and one who may not see this workspace, are not things the client's
        # INPUT can be corrected to fix, so they leave as GraphQL errors rather
        # than as field errors on the payload.
        scope = await authorized_scope(info, input.workspace_slug)

        try:
            entity = await info.context.triage_service.enter(
                scope=scope,
                issue_id=input.issue_id,
                actor_id=scope.user_id,
            )
        except ValidationError as exc:
            # Only expected input validation is translated into the payload.
            # Everything else (asyncpg failures, bugs, outages) propagates
            # through GraphQL's normal error mechanism and is masked.
            return TriagePayload(issue=None, errors=_errors(exc))

        return TriagePayload(issue=IssueType.from_entity(entity, scope), errors=[])

    @strawberry.mutation
    async def triage_accept(
        self, info: Info, input: TriageAcceptInput
    ) -> TriagePayload:
        """Accept an issue out of triage, into a state somebody chose."""
        scope = await authorized_scope(info, input.workspace_slug)

        try:
            entity = await info.context.triage_service.accept(
                scope=scope,
                issue_id=input.issue_id,
                workflow_state_id=input.workflow_state_id,
                actor_id=scope.user_id,
            )
        except ValidationError as exc:
            return TriagePayload(issue=None, errors=_errors(exc))

        return TriagePayload(issue=IssueType.from_entity(entity, scope), errors=[])

    @strawberry.mutation
    async def triage_decline(
        self, info: Info, input: TriageDeclineInput
    ) -> TriagePayload:
        """Refuse an issue: out of triage, onto its team's canceled state.

        Nothing is deleted. The issue keeps its identifier and its history and
        stays readable by anyone holding a link, which is what lets somebody
        reopen it later by moving it to another state.
        """
        scope = await authorized_scope(info, input.workspace_slug)

        try:
            entity = await info.context.triage_service.decline(
                scope=scope,
                issue_id=input.issue_id,
                actor_id=scope.user_id,
            )
        except ValidationError as exc:
            return TriagePayload(issue=None, errors=_errors(exc))

        return TriagePayload(issue=IssueType.from_entity(entity, scope), errors=[])

    @strawberry.mutation
    async def triage_mark_duplicate(
        self, info: Info, input: TriageMarkDuplicateInput
    ) -> TriagePayload:
        """Decline an issue, recording which issue it repeats.

        The relation and the decline commit together. A failure to relate --
        the other issue is not this workspace's, the pair is already marked --
        leaves the issue in the queue rather than declining it for a reason the
        server could not record.

        The payload carries the DECLINED issue rather than the one it
        duplicates, because that is the row the client is looking at and the
        one whose state just moved.
        """
        scope = await authorized_scope(info, input.workspace_slug)

        try:
            entity = await info.context.triage_service.mark_duplicate(
                scope=scope,
                issue_id=input.issue_id,
                duplicate_of_id=input.duplicate_of_id,
                actor_id=scope.user_id,
            )
        except ValidationError as exc:
            return TriagePayload(issue=None, errors=_errors(exc))

        return TriagePayload(issue=IssueType.from_entity(entity, scope), errors=[])

    @strawberry.mutation
    async def triage_change_team(
        self, info: Info, input: TriageChangeTeamInput
    ) -> TriagePayload:
        """Send a queued issue to the team it should have been filed against.

        The returned issue carries a NEW `identifier` and a new `number`: the
        issue is renumbered onto the target team's counter, because
        `issues_team_number_key` is unique per team and an issue cannot arrive
        carrying somebody else's number. A client holding the old identifier
        must take the one in this payload -- which is why the payload returns
        the issue rather than a boolean.
        """
        scope = await authorized_scope(info, input.workspace_slug)

        try:
            entity = await info.context.triage_service.change_team(
                scope=scope,
                issue_id=input.issue_id,
                team_id=input.team_id,
                actor_id=scope.user_id,
            )
        except ValidationError as exc:
            return TriagePayload(issue=None, errors=_errors(exc))

        return TriagePayload(issue=IssueType.from_entity(entity, scope), errors=[])
