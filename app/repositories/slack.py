from uuid import UUID

import asyncpg

from app.domain.slack import SlackInstallationEntity


# The constraint names migrations/014_slack_integration.sql declares, named
# here because a violation is only expected input for the constraint it was
# expected from. A bare `except asyncpg.UniqueViolationError` would translate
# any unique violation this statement can raise -- today and after the next
# migration adds one -- into "that Slack workspace is already connected",
# which is how a schema change becomes a wrong error message rather than a
# loud one. Read by app/services/slack.py, which does the translating.
SLACK_INSTALLATIONS_TEAM_KEY = "slack_installations_team_key"
SLACK_INSTALLATIONS_CONNECTED_BY_FK = "slack_installations_connected_by_fk"


class SlackRepository:
    """SQL access for `slack_installations` and `slack_event_deliveries`.

    The repository receives a connection from the service layer. It never
    acquires connections, never touches the pool, and never owns a
    transaction. `asyncpg.Record` never escapes this class.

    Two tables in one repository because they are one subject: an event is
    deduplicated in order to be routed through an installation, and the two
    reads happen on the same request. Splitting them would mean a service
    holding two repositories to answer one webhook.

    The token columns are never in a SELECT list beside the rest of the row.
    `find` and `find_by_team` project the eight non-secret columns; the token
    reference has its own statement, `find_token_reference`, which a caller
    has to ask for by name. That is what stops a bot token riding along in
    every status query, in every row this class hands upward, and in whatever
    a future `repr` of an entity ends up printing.
    """

    async def find(
        self,
        connection: asyncpg.Connection,
        *,
        workspace_id: UUID,
    ) -> SlackInstallationEntity | None:
        """This workspace's installation, or nothing.

        Keyed on `workspace_id` alone because that is the table's primary key:
        a workspace has one installation or none. The caller supplies the id
        from an AuthorizedWorkspaceScope, never from a client field.
        """
        row = await connection.fetchrow(
            """
            SELECT
                workspace_id,
                slack_team_id,
                slack_team_name,
                bot_user_id,
                scopes,
                connected_by_user_id,
                connected_at
            FROM slack_installations
            WHERE workspace_id = $1
            """,
            workspace_id,
        )

        if row is None:
            return None

        return self._to_entity(row)

    async def find_by_team(
        self,
        connection: asyncpg.Connection,
        *,
        slack_team_id: str,
    ) -> SlackInstallationEntity | None:
        """The installation an inbound Slack event belongs to, or nothing.

        This is the only lookup in the codebase that resolves a tenant from a
        string a third party supplied, so it is worth being precise about what
        makes it safe: `slack_installations_team_key` is UNIQUE, so this
        equality matches at most one row and no `LIMIT 1` is needed; and the
        caller has already verified the request's Slack signature, so the team
        id is one Slack asserted rather than one an anonymous poster chose.
        Reversing that order -- looking up first, verifying after -- would let
        an unauthenticated caller enumerate which Slack workspaces are
        connected by timing this query.

        An absent row is the ordinary case, not an error: Slack delivers
        events for every workspace an app is installed in, including ones this
        deployment has since disconnected.
        """
        row = await connection.fetchrow(
            """
            SELECT
                workspace_id,
                slack_team_id,
                slack_team_name,
                bot_user_id,
                scopes,
                connected_by_user_id,
                connected_at
            FROM slack_installations
            WHERE slack_team_id = $1
            """,
            slack_team_id,
        )

        if row is None:
            return None

        return self._to_entity(row)

    async def find_token_reference(
        self,
        connection: asyncpg.Connection,
        *,
        workspace_id: UUID,
    ) -> tuple[str, str] | None:
        """(backend, reference) for this workspace's bot token, or nothing.

        A statement of its own rather than two more columns on `find`, so that
        reading the token is something a caller does deliberately. The pair
        comes back together because a reference means nothing without the
        store that issued it -- 'database' means the reference IS the token,
        and a later 'kms' would mean it is a key id.

        A tuple rather than a Record: `asyncpg.Record` never escapes this
        class, and a two-field entity would be a type whose only purpose is to
        carry a secret between two frames.
        """
        row = await connection.fetchrow(
            """
            SELECT bot_token_backend, bot_token_reference
            FROM slack_installations
            WHERE workspace_id = $1
            """,
            workspace_id,
        )

        if row is None:
            return None

        return row["bot_token_backend"], row["bot_token_reference"]

    async def upsert(
        self,
        connection: asyncpg.Connection,
        *,
        workspace_id: UUID,
        slack_team_id: str,
        slack_team_name: str,
        bot_user_id: str,
        scopes: list[str],
        token_backend: str,
        token_reference: str,
        connected_by_user_id: UUID,
    ) -> SlackInstallationEntity:
        """Record a completed OAuth grant, replacing any earlier one.

        ON CONFLICT rather than DELETE-then-INSERT, and rather than a SELECT
        that decides between the two. A reconnect is one act, and splitting it
        leaves a window in which the workspace has no installation at all --
        during which an inbound event routes nowhere and is acknowledged as if
        it had been handled. The statement also has to be atomic against a
        second grant landing at the same instant, which no read-then-write is.

        `connected_at` is reset to now() on the update path: this row records
        the grant that is currently in force, and the old timestamp belongs to
        a token that has just been superseded.

        Every value is a parameter. The scope list arrives as a Python list
        and asyncpg binds it to TEXT[]; nothing is joined into a string here,
        so a scope containing a comma or a quote is a value rather than a
        syntax accident.
        """
        row = await connection.fetchrow(
            """
            INSERT INTO slack_installations (
                workspace_id,
                slack_team_id,
                slack_team_name,
                bot_user_id,
                scopes,
                bot_token_backend,
                bot_token_reference,
                connected_by_user_id
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            ON CONFLICT (workspace_id) DO UPDATE SET
                slack_team_id = EXCLUDED.slack_team_id,
                slack_team_name = EXCLUDED.slack_team_name,
                bot_user_id = EXCLUDED.bot_user_id,
                scopes = EXCLUDED.scopes,
                bot_token_backend = EXCLUDED.bot_token_backend,
                bot_token_reference = EXCLUDED.bot_token_reference,
                connected_by_user_id = EXCLUDED.connected_by_user_id,
                connected_at = now()
            RETURNING
                workspace_id,
                slack_team_id,
                slack_team_name,
                bot_user_id,
                scopes,
                connected_by_user_id,
                connected_at
            """,
            workspace_id,
            slack_team_id,
            slack_team_name,
            bot_user_id,
            scopes,
            token_backend,
            token_reference,
            connected_by_user_id,
        )

        # Not None: the statement either inserts or updates, and both paths
        # RETURNING a row. Asserted by the annotation rather than at runtime --
        # asyncpg is untyped, so a None here would surface as an AttributeError
        # in `_to_entity`, which is a loud failure and the right one for a
        # condition this schema cannot produce.
        return self._to_entity(row)

    async def delete(
        self,
        connection: asyncpg.Connection,
        *,
        workspace_id: UUID,
    ) -> bool:
        """Remove this workspace's installation. True if there was one.

        The boolean is read from the command tag rather than from a SELECT
        first, so there is no window between deciding and deleting. A
        disconnect of a workspace that was not connected is not an error --
        two admins clicking the same button is an ordinary race -- so the
        caller reports the resulting state rather than a failure.

        Scoped to one workspace_id and nothing else. There is no statement in
        this class that can delete an installation the caller did not name.
        """
        # Annotated rather than compared inline: asyncpg ships no types, so the
        # command tag is Any and `Any != str` would satisfy the bool return
        # type whatever execute() actually handed back.
        tag: str = await connection.execute(
            """
            DELETE FROM slack_installations
            WHERE workspace_id = $1
            """,
            workspace_id,
        )

        return tag != "DELETE 0"

    async def record_event(
        self,
        connection: asyncpg.Connection,
        *,
        event_id: str,
    ) -> bool:
        """Claim one Slack event id. True if this caller claimed it first.

        One statement, and that is the entire deduplication guarantee rather
        than a performance note. SELECT-then-INSERT has a window exactly the
        width of the processing it guards: Slack's retry arrives while the
        first delivery is still working, both SELECTs find nothing, and both
        proceed. `ON CONFLICT DO NOTHING` moves the decision inside the index
        write, where the second caller loses however the two interleave.

        RETURNING is what makes the outcome readable. Without it the statement
        succeeds identically whether it inserted or conflicted, and the caller
        would be back to a second query to find out which -- reopening the
        race it just closed.

        The caller must open a transaction that commits BEFORE the work this
        claim guards is finished; see SlackService.claim_event for why the
        claim is committed separately.
        """
        claimed = await connection.fetchval(
            """
            INSERT INTO slack_event_deliveries (event_id)
            VALUES ($1)
            ON CONFLICT (event_id) DO NOTHING
            RETURNING event_id
            """,
            event_id,
        )

        return claimed is not None

    @staticmethod
    def _to_entity(row: asyncpg.Record) -> SlackInstallationEntity:
        return SlackInstallationEntity(
            workspace_id=row["workspace_id"],
            slack_team_id=row["slack_team_id"],
            slack_team_name=row["slack_team_name"],
            bot_user_id=row["bot_user_id"],
            # asyncpg decodes TEXT[] as a list; the entity holds a tuple so
            # that nothing downstream can append to a workspace's granted
            # scopes and have the change look like something Slack said.
            scopes=tuple(row["scopes"]),
            connected_by_user_id=row["connected_by_user_id"],
            connected_at=row["connected_at"],
        )
