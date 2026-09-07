import strawberry
from strawberry.types import Info

from app.domain.errors import ValidationError
from app.graphql.inputs.subscriber import IssueSubscriptionInput
from app.graphql.scope import authorized_scope
from app.graphql.types.errors import ValidationErrorType
from app.graphql.types.subscriber import IssueSubscriptionPayload


def _errors(exc: ValidationError) -> list[ValidationErrorType]:
    return [ValidationErrorType.from_domain(issue) for issue in exc.issues]


@strawberry.type
class SubscriberMutation:
    """Watching and unwatching, merged into the root Mutation.

    Both act on the viewer's own subscription and there is no field through
    which they could act on anybody else's. That is not enforced by a check
    here: `ActivityService.subscribe` has no `user_id` parameter at all, so the
    property holds for every future caller rather than for the two below.

    There is no mutation for subscribing SOMEBODY ELSE, and adding one would be
    a decision rather than an extension. Being made to follow an issue by
    another person is a way to fill an inbox, and the product has no
    notification preferences to opt out with yet -- so the feature would ship
    with no way to refuse it.
    """

    @strawberry.mutation
    async def issue_subscribe(
        self,
        info: Info,
        input: IssueSubscriptionInput,
    ) -> IssueSubscriptionPayload:
        """Start watching one issue.

        Idempotent: watching an issue twice succeeds and reports the same
        state, leaving the original "watching since" where it was.

        An issue in another workspace and one that does not exist answer the
        same "Issue not found", because a distinguishable answer would tell a
        caller holding a guessed id that the issue is real and simply not
        theirs.
        """
        # Resolved before the try, and outside it. An unauthenticated caller
        # and one who may not see this workspace are not things the client's
        # INPUT can be corrected to fix.
        scope = await authorized_scope(info, input.workspace_slug)

        try:
            await info.context.activity_service.subscribe(
                scope=scope,
                issue_id=input.issue_id,
            )
        except ValidationError as exc:
            # Only expected input validation reaches the payload. Everything
            # else propagates through GraphQL's error mechanism and is masked.
            return IssueSubscriptionPayload(subscribed=False, errors=_errors(exc))

        return IssueSubscriptionPayload(subscribed=True, errors=[])

    @strawberry.mutation
    async def issue_unsubscribe(
        self,
        info: Info,
        input: IssueSubscriptionInput,
    ) -> IssueSubscriptionPayload:
        """Stop watching one issue.

        Cannot fail on a missing issue, and does not try to: unwatching one
        that is not there, or is another tenant's, leaves the caller in exactly
        the state they asked for. Reporting a failure would both break the
        retry story and make an issue's existence observable.

        Note that this is not permanent by itself. Commenting on the issue
        again subscribes the same person again, which is the rule migration 020
        argues for -- commenting is asking to be part of the conversation.
        """
        scope = await authorized_scope(info, input.workspace_slug)

        await info.context.activity_service.unsubscribe(
            scope=scope,
            issue_id=input.issue_id,
        )

        return IssueSubscriptionPayload(subscribed=False, errors=[])
