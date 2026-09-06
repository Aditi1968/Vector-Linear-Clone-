import strawberry
from strawberry.types import Info

from app.graphql.inputs.slack import SlackDisconnectInput
from app.graphql.queries.slack import slack_admin_scope
from app.graphql.types.slack import SlackDisconnectPayload, SlackIntegrationType


@strawberry.type
class SlackMutation:
    """Slack writes, composed into the root Mutation by app/graphql/schema.py.

    One mutation. Connecting is not here and cannot be: an OAuth grant is a
    browser redirect to Slack and back, which is a REST flow by nature (see
    app/rest/slack.py). Disconnecting is an ordinary authorized write with no
    third party in the loop, so it belongs on the product API like every other
    write.

    `slack_admin_scope` is imported from the query module rather than
    duplicated. The authorization for reading the integration and for removing
    it is the same question, and it should have exactly one answer.
    """

    @strawberry.mutation
    async def slack_disconnect(
        self,
        info: Info,
        input: SlackDisconnectInput,
    ) -> SlackDisconnectPayload:
        """Remove this workspace's Slack installation.

        Idempotent: disconnecting a workspace that was not connected succeeds
        and reports DISCONNECTED. Two admins pressing the same button is an
        ordinary race, and an error on the second would describe the first
        admin's action rather than anything the second did wrong.

        The token is deleted here and NOT revoked at Slack; SlackService
        .disconnect says exactly what that leaves live and why revocation
        belongs with a retryable job rather than in this request.
        """
        scope = await slack_admin_scope(info, input.workspace_slug)

        view = await info.context.slack_service.disconnect(scope=scope)

        return SlackDisconnectPayload(
            integration=SlackIntegrationType.from_view(view),
        )
