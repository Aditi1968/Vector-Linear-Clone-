from uuid import UUID

import asyncpg

from app.domain.slack import (
    SlackChannelEntity,
    SlackInstallationEntity,
    SlackNotificationPreference,
)


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
    """SQL access for the five tables one workspace's Slack connection owns.

    `slack_installations` and `slack_event_deliveries` from migration 014, and
    `slack_channels`, `slack_notification_settings` and
    `slack_notification_preferences` from 018.

    The repository receives a connection from the service layer. It never
    acquires connections, never touches the pool, and never owns a
    transaction. `asyncpg.Record` never escapes this class.

    One repository rather than five, because they are one subject and mostly
    one transaction: an event is deduplicated in order to be routed through an
    installation, a sync writes channels and refreshes a settings row in the
    same breath, and a disconnect has to remove all five in dependency order.
    Splitting them would mean a service holding several repositories to answer
    one request, and would put the delete ORDER -- which is the only thing
    standing between a disconnect and a RESTRICT violation -- in a service
    rather than beside the statements it constrains.

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

    # --- channels, settings and preferences (migration 018) -------------

    async def replace_channels(
        self,
        connection: asyncpg.Connection,
        *,
        workspace_id: UUID,
        channels: list[SlackChannelEntity],
    ) -> None:
        """Record what `conversations.list` just reported, in three statements.

        Called inside the service's transaction, and it has to be: the three
        statements are one act, and a sync that upserted the listing and then
        failed before marking the rest would leave channels the workspace can
        no longer reach still offered in the picker.

        Statement one is a bulk upsert through `unnest`, not an `executemany`.
        One round trip for a workspace with eight hundred channels rather than
        eight hundred, and -- more importantly -- one statement, so there is no
        interleaving for the other two to be inconsistent with. The arrays are
        bound as parameters like any other value; nothing is joined into the
        text, so a channel named `'); DROP` is a name.

        Statement two is why this is `replace_channels` and not
        `upsert_channels`, and why it UPDATEs rather than DELETEs. A channel
        drops out of the listing when it is deleted, made private, or the bot
        loses sight of it -- and one of those channels may be the one an admin
        chose as their default, which
        `slack_notification_settings_default_channel_fk` protects with
        RESTRICT. A delete would abort the whole sync; marking keeps the row,
        keeps the name a settings screen needs in order to explain itself, and
        keeps the choice intact so it works again if the bot is re-invited.

        Statement three is the rename refresh. `slack_notification_settings`
        holds the channel name denormalised so a disappeared channel still has
        one to show, and this is the single place that duplicate is brought
        back into step. `IS DISTINCT FROM` rather than `<>` so the comparison
        is right for a NULL name -- a workspace with no default channel -- for
        which the join produces no row anyway, and which must not be written.
        """
        await connection.execute(
            """
            INSERT INTO slack_channels (
                workspace_id,
                channel_id,
                name,
                is_private,
                is_archived,
                is_member,
                is_accessible
            )
            SELECT $1, listed.channel_id, listed.name, listed.is_private,
                   listed.is_archived, listed.is_member, TRUE
            FROM unnest(
                $2::text[], $3::text[], $4::boolean[], $5::boolean[], $6::boolean[]
            ) AS listed(channel_id, name, is_private, is_archived, is_member)
            ON CONFLICT (workspace_id, channel_id) DO UPDATE SET
                name = EXCLUDED.name,
                is_private = EXCLUDED.is_private,
                is_archived = EXCLUDED.is_archived,
                is_member = EXCLUDED.is_member,
                is_accessible = TRUE,
                synced_at = now()
            """,
            workspace_id,
            [channel.channel_id for channel in channels],
            [channel.name for channel in channels],
            [channel.is_private for channel in channels],
            [channel.is_archived for channel in channels],
            [channel.is_member for channel in channels],
        )

        await connection.execute(
            """
            UPDATE slack_channels
            SET is_accessible = FALSE,
                synced_at = now()
            WHERE workspace_id = $1
              AND is_accessible
              AND channel_id <> ALL($2::text[])
            """,
            workspace_id,
            [channel.channel_id for channel in channels],
        )

        await connection.execute(
            """
            UPDATE slack_notification_settings AS settings
            SET default_channel_name = channel.name,
                updated_at = now()
            FROM slack_channels AS channel
            WHERE settings.workspace_id = $1
              AND channel.workspace_id = settings.workspace_id
              AND channel.channel_id = settings.default_channel_id
              AND channel.name IS DISTINCT FROM settings.default_channel_name
            """,
            workspace_id,
        )

    async def list_channels(
        self,
        connection: asyncpg.Connection,
        *,
        workspace_id: UUID,
    ) -> list[SlackChannelEntity]:
        """Every channel this workspace has ever been told about.

        Inaccessible ones included, deliberately. They are what lets a settings
        screen say "#engineering is no longer reachable" instead of quietly
        dropping the row an admin's saved choice points at; the caller filters
        for a picker, and the ordering puts the usable ones first so it does
        not have to sort.

        Ordered in SQL rather than in Python because the order is part of the
        answer, and an ordering decided by whichever caller renders it is an
        ordering that differs between two screens showing one list.

        No LIMIT and no cursor. This is bounded by the workspace's Slack, not
        by anything a client sends, and a picker that paged would be a picker
        you cannot type into. If a workspace ever has enough channels for this
        to hurt, the fix is a server-side name filter, not a page.
        """
        rows = await connection.fetch(
            """
            SELECT
                channel_id,
                name,
                is_private,
                is_archived,
                is_member,
                is_accessible
            FROM slack_channels
            WHERE workspace_id = $1
            ORDER BY is_accessible DESC, is_archived, name
            """,
            workspace_id,
        )

        return [self._to_channel(row) for row in rows]

    async def find_channel(
        self,
        connection: asyncpg.Connection,
        *,
        workspace_id: UUID,
        channel_id: str,
    ) -> SlackChannelEntity | None:
        """One channel of THIS workspace, or nothing.

        The workspace id comes from an AuthorizedWorkspaceScope and the channel
        id from the client, and the equality is on both -- so a channel id
        belonging to another tenant answers exactly as one that does not exist.
        That is the read behind "you may not choose that channel", and it has
        to be indistinguishable from a miss: an answer that told the two apart
        would confirm, to anyone who can reach a settings page, that a
        particular Slack channel is connected to this deployment.
        """
        row = await connection.fetchrow(
            """
            SELECT
                channel_id,
                name,
                is_private,
                is_archived,
                is_member,
                is_accessible
            FROM slack_channels
            WHERE workspace_id = $1 AND channel_id = $2
            """,
            workspace_id,
            channel_id,
        )

        if row is None:
            return None

        return self._to_channel(row)

    async def find_default_channel(
        self,
        connection: asyncpg.Connection,
        *,
        workspace_id: UUID,
    ) -> tuple[str, str] | None:
        """(channel id, channel name) this workspace posts to, or nothing.

        A tuple rather than an entity, for the reason `find_token_reference`
        gives about its pair: the two values are meaningless apart -- an id
        with no name is an opaque string on a settings screen -- and a type
        whose only job is to carry two strings between two frames is a type
        nobody gains from reading.

        Absent covers both "no settings row" and "a row with no channel". They
        are the same state to every caller: this workspace has nowhere to post.
        `slack_notification_settings_channel_pair` in 018 is what guarantees
        the third case -- a name without an id -- cannot be stored, so the
        NOT NULL test here needs to name only one column.
        """
        row = await connection.fetchrow(
            """
            SELECT default_channel_id, default_channel_name
            FROM slack_notification_settings
            WHERE workspace_id = $1 AND default_channel_id IS NOT NULL
            """,
            workspace_id,
        )

        if row is None:
            return None

        return row["default_channel_id"], row["default_channel_name"]

    async def set_default_channel(
        self,
        connection: asyncpg.Connection,
        *,
        workspace_id: UUID,
        channel_id: str,
        channel_name: str,
    ) -> None:
        """Record where this workspace's notifications go.

        ON CONFLICT rather than a SELECT that chooses between INSERT and
        UPDATE, for the reason `upsert` above gives: two admins on the settings
        screen at once is an ordinary race, and a read-then-write loses it by
        raising a unique violation at whichever of them was second.

        The name is written from the row the service just read out of
        `slack_channels`, never from an argument the client supplied -- so the
        denormalised copy starts out agreeing with the cache rather than
        agreeing with whoever sent the mutation. The composite foreign key
        would refuse a channel from another tenant even if it did not, but a
        name is not part of that key and nothing in the schema could catch a
        client-supplied one.
        """
        await connection.execute(
            """
            INSERT INTO slack_notification_settings (
                workspace_id,
                default_channel_id,
                default_channel_name
            )
            VALUES ($1, $2, $3)
            ON CONFLICT (workspace_id) DO UPDATE SET
                default_channel_id = EXCLUDED.default_channel_id,
                default_channel_name = EXCLUDED.default_channel_name,
                updated_at = now()
            """,
            workspace_id,
            channel_id,
            channel_name,
        )

    async def list_preferences(
        self,
        connection: asyncpg.Connection,
        *,
        workspace_id: UUID,
    ) -> list[SlackNotificationPreference]:
        """The preference rows this workspace has actually written.

        Only the rows. The service merges them with the full vocabulary, so
        that a repository never has to hold an opinion about what an unwritten
        preference means -- which is a product decision, and one a future
        release may change without a migration.
        """
        rows = await connection.fetch(
            """
            SELECT event, enabled
            FROM slack_notification_preferences
            WHERE workspace_id = $1
            """,
            workspace_id,
        )

        return [
            SlackNotificationPreference(event=row["event"], enabled=row["enabled"])
            for row in rows
        ]

    async def set_preference(
        self,
        connection: asyncpg.Connection,
        *,
        workspace_id: UUID,
        event: str,
        enabled: bool,
    ) -> None:
        """Turn one event on or off for one workspace.

        An upsert rather than an insert-or-delete pair. Deleting the row to
        mean "off" would make "never configured" and "deliberately disabled"
        the same absence, and they are not the same: the first is a default a
        later release may change, the second is a decision it must not.

        `event` reaches the statement as a parameter and is checked by
        `slack_notification_preferences_event_known`, so a value outside the
        vocabulary is refused by the database as well as by the GraphQL enum
        that produced it. Two gates for one rule, because the enum only guards
        the transport this feature has today.
        """
        await connection.execute(
            """
            INSERT INTO slack_notification_preferences (
                workspace_id, event, enabled
            )
            VALUES ($1, $2, $3)
            ON CONFLICT (workspace_id, event) DO UPDATE SET
                enabled = EXCLUDED.enabled,
                updated_at = now()
            """,
            workspace_id,
            event,
            enabled,
        )

    async def delete_dependents(
        self,
        connection: asyncpg.Connection,
        *,
        workspace_id: UUID,
    ) -> None:
        """Remove everything 018 hangs off this workspace's installation.

        In dependency order, and the order is the whole method. Every foreign
        key in 018 is RESTRICT, so `delete` above fails outright while any of
        these rows survive -- which is the intended behaviour, not an obstacle:
        it means a disconnect that forgot one of these tables is a loud error
        rather than an orphaned row nothing will ever read again.

        Settings first, because it is the only table that points at
        `slack_channels`. Preferences next; they point at nothing but the
        installation. Channels last.

        Called inside the caller's transaction. On its own it would be three
        deletes that can half-happen, and half-happening here means a
        disconnect that leaves the channel cache of a workspace that is no
        longer connected.
        """
        for statement in (
            "DELETE FROM slack_notification_settings WHERE workspace_id = $1",
            "DELETE FROM slack_notification_preferences WHERE workspace_id = $1",
            "DELETE FROM slack_channels WHERE workspace_id = $1",
        ):
            await connection.execute(statement, workspace_id)

    @staticmethod
    def _to_channel(row: asyncpg.Record) -> SlackChannelEntity:
        return SlackChannelEntity(
            channel_id=row["channel_id"],
            name=row["name"],
            is_private=row["is_private"],
            is_archived=row["is_archived"],
            is_member=row["is_member"],
            is_accessible=row["is_accessible"],
        )

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
