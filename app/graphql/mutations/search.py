import strawberry
from graphql import GraphQLError
from strawberry.types import Info

from app.domain.errors import ValidationError
from app.graphql.scope import authorized_scope


# One sweep's default size. Small enough that the call returns promptly with a
# real model behind it, large enough that draining a new workspace is a handful
# of calls rather than hundreds.
DEFAULT_LIMIT = 100


@strawberry.type
class SearchMutation:
    """The one write the search feature owns: filling in missing embeddings.

    WHY THIS IS A MUTATION AT ALL. Nothing else in this system writes
    `issue_embeddings`: `IssueService.create` deliberately does not, for the
    reasons `SearchService.refresh_embeddings` gives -- model latency does not
    belong on the path of filing an issue, and an issue edited three times in
    its first minute would be embedded four times to keep the last. So the
    embeddings arrive from a background pass.

    THERE IS NOW A WORKER THAT RUNS THAT PASS BY ITSELF -- `EmbeddingWorker`,
    started from the application's lifespan when `EMBEDDING_WORKER_ENABLED` is
    set, draining the queue migration 028 added. This field is kept anyway and
    is not made redundant by it: it is the way to force a sweep NOW for one
    workspace -- after a bulk import, or on a deployment that has deliberately
    left the worker off -- and it is the only path that exists at all when no
    process has the flag. The two cannot fight, because both write through
    `EmbeddingRepository.upsert` and both decide what is stale from the same
    anti-join; the worst case of running both is one wasted vector.

    What has changed is what a client should show when this returns zero.
    `embeddingIndexingState` is the field that says whether zero meant "nothing
    to do" or "no model here".

    It is idempotent and self-limiting: it embeds only issues whose stored
    vector does not match their current text, so calling it twice in a row does
    the work once and reports zero the second time. A caller loops until it
    reports zero.

    Any MEMBER may call it, which is a deliberate widening of what an admin
    endpoint would be. It writes no product data, discloses nothing about the
    workspace -- the return value is a count of the caller's own workspace's
    unembedded issues -- and refusing it to members would mean semantic search
    silently not working for every workspace whose admin never visited a
    settings page. The cost it can impose is bounded by REFRESH_MAX per call.
    """

    @strawberry.mutation
    async def embeddings_refresh(
        self,
        info: Info,
        workspace_slug: str,
        limit: int = DEFAULT_LIMIT,
    ) -> int:
        """Embed up to `limit` of this workspace's issues that need it.

        Returns how many embeddings were WRITTEN, which is not always how many
        issues were read: an issue edited between the read and the write
        declines its own row and stays queued, so a sweep can honestly report
        fewer than it attempted. See `EmbeddingRepository.upsert`.

        Zero when this deployment has no embedder wired, and zero when there
        was nothing to do. Those are deliberately the same answer: both mean
        "no embeddings were written", and a client that could tell them apart
        would be reading the server's configuration off a mutation result.

        Authorized before anything is read or written, through the same
        `authorized_scope` every workspace-scoped field uses. The scope is a
        `workspace_id` equality in both the queue read and the write, so a
        sweep can neither see nor embed another tenant's issues.
        """
        scope = await authorized_scope(info, workspace_slug)

        try:
            # Annotated rather than returned inline, for the reason
            # `app.graphql.scope.authorized_scope` gives about its own scope:
            # the context attribute is untyped here, so returning it directly
            # would satisfy any return type at all.
            written: int = await info.context.search_service.refresh_embeddings(
                scope=scope,
                limit=limit,
            )
        except ValidationError as exc:
            # Only expected input validation is translated. Anything else --
            # an asyncpg failure, a bug in an embedder -- propagates and is
            # masked by app/graphql/schema.py rather than being reported to the
            # client as bad input. `from None` keeps the domain exception out
            # of the response.
            raise GraphQLError(
                "Invalid embedding refresh arguments",
                extensions={
                    "code": "BAD_USER_INPUT",
                    "issues": [
                        {
                            "field": issue.field,
                            "code": issue.code,
                            "message": issue.message,
                        }
                        for issue in exc.issues
                    ],
                },
            ) from None

        return written
