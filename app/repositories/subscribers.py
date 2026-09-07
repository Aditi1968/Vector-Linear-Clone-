from uuid import UUID

import asyncpg

from app.domain.subscribers import SubscriberEntity
from app.domain.tenancy import WorkspaceScope


class SubscriberRepository:
    """SQL access for `issue_subscribers`.

    The repository receives a connection from the service layer. It never
    acquires connections, never touches the pool, and never owns a
    transaction. `asyncpg.Record` never escapes this class.

    Every statement is scoped to one workspace and the scope arrives as a
    required keyword argument. That is not a filter applied to rows already
    fetched: it is an equality in the WHERE clause, or -- for the insert -- the
    single column both composite foreign keys read, so a row pairing one
    tenant's issue with another's member has no workspace_id that satisfies
    both parents.
    """

    async def subscribe(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        issue_id: UUID,
        user_id: UUID,
    ) -> bool:
        """Start watching; True if this call is what started it.

        `ON CONFLICT DO NOTHING` here, unlike `IssueLabelRepository.attach`
        which argues against it, and the difference is what the caller is
        asking. Attaching a label twice is a mistake worth reporting -- the
        person clicked a label that was already on the issue. Subscribing twice
        is not: the auto-subscribe path fires on every comment, so the second
        comment on an issue would otherwise have to catch and discard a unique
        violation inside somebody else's transaction. In PostgreSQL a
        constraint violation aborts the whole transaction, so "catch it and
        carry on" is not available to a caller that is mid-write -- which is
        exactly the caller this method has.

        The conflict target is named rather than left bare, so a violation of
        either FOREIGN key still raises. A bare `DO NOTHING` swallows only
        unique and exclusion violations, so this is belt and braces -- but the
        named target also says which duplicate is expected, and a future second
        constraint on this table would not be silently absorbed.

        The return value distinguishes "subscribed" from "already subscribed",
        which is what lets a mutation answer honestly without a second
        statement to look first. `RETURNING` produces no row when the conflict
        clause suppressed the insert.

        Neither the issue nor the user is checked against the workspace before
        the insert, and neither should be: both foreign keys are composite
        against this row's single workspace_id, so the server refuses an issue
        belonging to another tenant, and a user who is not a member of this
        one, as part of this statement. A SELECT first would be a second,
        weaker copy of both rules -- weaker because it can be raced, and weaker
        because it would then be two places that have to agree.
        """
        inserted = await connection.fetchval(
            """
            INSERT INTO issue_subscribers (workspace_id, issue_id, user_id)
            VALUES ($1, $2, $3)
            ON CONFLICT ON CONSTRAINT issue_subscribers_pkey DO NOTHING
            RETURNING user_id
            """,
            scope.workspace_id,
            issue_id,
            user_id,
        )

        return inserted is not None

    async def unsubscribe(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        issue_id: UUID,
        user_id: UUID,
    ) -> bool:
        """Stop watching; True if a row went.

        All three ids are matched under this workspace's key, so a request
        naming another tenant's issue deletes nothing and answers False --
        indistinguishable from an issue this user was never watching. Any
        other answer would tell a caller holding a guessed id that the issue is
        real and simply not theirs.
        """
        deleted = await connection.fetchval(
            """
            DELETE FROM issue_subscribers
            WHERE workspace_id = $1 AND issue_id = $2 AND user_id = $3
            RETURNING user_id
            """,
            scope.workspace_id,
            issue_id,
            user_id,
        )

        return deleted is not None

    async def list_for_issue(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        issue_id: UUID,
        limit: int,
    ) -> list[SubscriberEntity]:
        """Everyone watching one issue, oldest subscription first.

        No cursor and no keyset, unlike every other list in this layer. The
        set is bounded by the workspace's membership -- an issue cannot have
        more watchers than the workspace has members -- so this is a
        configuration-sized read rather than a product list that grows without
        limit, and `limit` is a ceiling on the response rather than a page
        size. See `ActivityService.list_subscribers` for the cap and when it
        would have to become a real page.

        Ordered by (created_at, id) rather than by created_at alone: two
        auto-subscribes inside one transaction share a timestamp to the
        microsecond, and an order that is not total makes the same list come
        back in two different orders on two reads.

        An issue in another workspace produces an empty list, which is what an
        unwatched issue produces. The equivalence is the isolation property.
        """
        rows = await connection.fetch(
            """
            SELECT user_id, created_at
            FROM issue_subscribers
            WHERE workspace_id = $1 AND issue_id = $2
            ORDER BY created_at, user_id
            LIMIT $3
            """,
            scope.workspace_id,
            issue_id,
            limit,
        )

        return [
            SubscriberEntity(user_id=row["user_id"], created_at=row["created_at"])
            for row in rows
        ]

    async def is_subscribed(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        issue_id: UUID,
        user_id: UUID,
    ) -> bool:
        """Whether this user watches this issue, in this workspace.

        For the `Issue.viewerIsSubscribed` field, which needs one boolean and
        not a list the client then searches. Served by
        `issue_subscribers_pkey` in full -- all three columns are equalities
        against its key, in its order.
        """
        watching: bool = await connection.fetchval(
            """
            SELECT EXISTS (
                SELECT 1
                FROM issue_subscribers
                WHERE workspace_id = $1 AND issue_id = $2 AND user_id = $3
            )
            """,
            scope.workspace_id,
            issue_id,
            user_id,
        )

        return watching
