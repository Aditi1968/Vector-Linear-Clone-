from datetime import timedelta
from uuid import UUID

import asyncpg

from app.domain.memberships import WorkspaceInvitationEntity


class InvitationRepository:
    """SQL access for `workspace_invitations`.

    The repository receives a connection from the service layer. It never
    acquires connections, never touches the pool, and never owns a
    transaction. `asyncpg.Record` never escapes this class.

    No statement here selects `token_hash`, and no entity carries it. The
    column is written once and afterwards only ever appears in a WHERE clause,
    which is what keeps a bearer credential's digest out of every payload,
    log line and traceback above this file. The raw token appears nowhere at
    all: the service hashes before it calls here, so a token is not a value
    any query in this file could bind even by mistake.
    """

    async def create(
        self,
        connection: asyncpg.Connection,
        *,
        workspace_id: UUID,
        email: str,
        role: str,
        token_hash: str,
        expires_in: timedelta,
    ) -> WorkspaceInvitationEntity:
        """Record an invitation against a hash of the token that redeems it.

        The lifetime is supplied rather than defaulted in SQL, because 004
        deliberately gives `expires_at` no default: how long an invitation
        lives is a policy decision made by the code issuing it, not by the
        schema.

        It arrives as an interval added to `now()` on the server rather than
        as a timestamp computed here, so that expiry is written and later
        checked against one clock. Computed in the application, an issuer
        whose clock runs fast writes an expiry the redemption path -- which
        reads the server's `now()` -- treats as sooner than intended, and a
        `workspace_invitations_expiry_after_creation` violation is the visible
        end of that same skew.

        There is no ON CONFLICT and no uniqueness over (workspace, email).
        Two live invitations to one address are ordinary -- the first expired,
        the second re-sent -- and 004 says so at length; redemption is by
        token, so a duplicate is at worst two ways in for the same person.
        """
        row = await connection.fetchrow(
            """
            INSERT INTO workspace_invitations (
                workspace_id,
                email,
                role,
                token_hash,
                expires_at
            )
            VALUES ($1, $2, $3, $4, now() + $5)
            RETURNING
                id,
                workspace_id,
                email,
                role,
                expires_at,
                accepted_at,
                created_at
            """,
            workspace_id,
            email,
            role,
            token_hash,
            expires_in,
        )

        return self._to_entity(row)

    async def list_pending(
        self,
        connection: asyncpg.Connection,
        *,
        workspace_id: UUID,
        limit: int,
    ) -> list[WorkspaceInvitationEntity]:
        """The invitations to this workspace that can still be accepted.

        "Pending" is the two conditions redemption itself checks -- not yet
        accepted, not yet expired -- spelled the same way here as in `claim`
        below, so the list a workspace's admins act on cannot show a row that
        `claim` would refuse.

        `now()` rather than a timestamp passed in from the application. Both
        this and the expiry check in `claim` then read the same clock, the
        server's, so an invitation cannot be listed as live by one process
        whose clock is behind and refused by another.

        Ordered newest first: an admin who has just sent one is looking for
        it, and `id` is a uuidv7 whose leading bits are the creation
        timestamp, so the order is total without a tiebreak.
        """
        rows = await connection.fetch(
            """
            SELECT
                id,
                workspace_id,
                email,
                role,
                expires_at,
                accepted_at,
                created_at
            FROM workspace_invitations
            WHERE workspace_id = $1
                AND accepted_at IS NULL
                AND expires_at > now()
            ORDER BY id DESC
            LIMIT $2
            """,
            workspace_id,
            limit,
        )

        return [self._to_entity(row) for row in rows]

    async def delete(
        self,
        connection: asyncpg.Connection,
        *,
        workspace_id: UUID,
        invitation_id: UUID,
    ) -> UUID | None:
        """Withdraw an invitation, or answer nothing if there is no such one.

        Scoped by workspace as well as by id. An invitation id that reaches
        this process from a client is a value, not an authorisation, and
        deleting by id alone would let anyone holding one revoke another
        tenant's invitation.

        A delete rather than a flag, because 004 gives this table no column
        for "revoked" -- and the row's only purpose is to be redeemable, so
        one that must not be redeemed has no reason to exist. The effect on a
        token already in flight is the same either way: it matches nothing.
        """
        revoked = await connection.fetchval(
            """
            DELETE FROM workspace_invitations
            WHERE workspace_id = $1 AND id = $2
            RETURNING id
            """,
            workspace_id,
            invitation_id,
        )

        if revoked is None:
            return None

        # Annotated rather than returned inline: asyncpg ships no types, so
        # `fetchval` is Any and would silently satisfy any return type.
        revoked_id: UUID = revoked

        return revoked_id

    async def claim(
        self,
        connection: asyncpg.Connection,
        *,
        token_hash: str,
    ) -> WorkspaceInvitationEntity | None:
        """Mark an invitation accepted, and hand it back -- or answer nothing.

        This is the single-use guarantee, and it is one statement because it
        cannot be two. `SELECT` then `UPDATE` leaves a window in which two
        concurrent redemptions of the same token both see `accepted_at IS
        NULL`, and both go on to insert a membership.

        The UPDATE takes a row-level exclusive lock as it runs. A second
        redemption blocks on it; when the first transaction commits,
        PostgreSQL re-evaluates the WHERE clause against the committed row,
        finds `accepted_at` no longer NULL, and updates nothing. So exactly
        one caller is ever handed a row, however many present the token at
        once -- and because the caller MUST be inside the transaction that
        inserts the membership, an insert that fails takes the acceptance down
        with it and leaves the invitation usable.

        The three ways to get nothing back -- no such token, already accepted,
        expired -- are one answer on purpose. Distinguishing them would tell
        whoever holds a string whether it was ever a real invitation, which is
        an oracle for guessing tokens; see MembershipService.accept_invitation.

        `now()` rather than an application timestamp, so expiry is decided by
        the same clock everywhere. The lookup is by hash: the raw token never
        reaches this layer.
        """
        row = await connection.fetchrow(
            """
            UPDATE workspace_invitations
            SET accepted_at = now()
            WHERE token_hash = $1
                AND accepted_at IS NULL
                AND expires_at > now()
            RETURNING
                id,
                workspace_id,
                email,
                role,
                expires_at,
                accepted_at,
                created_at
            """,
            token_hash,
        )

        if row is None:
            return None

        return self._to_entity(row)

    @staticmethod
    def _to_entity(row: asyncpg.Record) -> WorkspaceInvitationEntity:
        return WorkspaceInvitationEntity(
            id=row["id"],
            workspace_id=row["workspace_id"],
            email=row["email"],
            role=row["role"],
            expires_at=row["expires_at"],
            accepted_at=row["accepted_at"],
            created_at=row["created_at"],
        )
