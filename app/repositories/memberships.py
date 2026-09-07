from uuid import UUID

import asyncpg

from app.domain.memberships import WorkspaceMemberEntity, WorkspaceMembershipEntity


# The shared holdings `shared_holdings` probes for, in the order it reports
# them, named after the foreign key that used to refuse a removal over each.
#
# Here rather than inline so that the order is a stated decision and not
# whatever a dict literal happened to iterate as: this is the sequence an admin
# reads "what do I have to reassign" in, and a list that reshuffles between two
# attempts at the same removal reads as a different answer.
#
# Every name is also a column in the statement's select list. The statement is
# a complete literal and is NOT built from this tuple -- see the class docstring
# on why no SQL here is assembled from Python -- so the two are kept in step by
# `test_members_invites_db.py`, which fails on a name this tuple has and the
# statement does not.
SHARED_HOLDINGS = (
    "projects_lead_fk",
    "initiatives_owner_fk",
    "saved_views_creator_fk",
    "issue_templates_assignee_fk",
    "github_installations_connected_by_fk",
    "slack_installations_connected_by_fk",
)


class MembershipRepository:
    """SQL access for `workspace_members`.

    The repository receives a connection from the service layer. It never
    acquires connections, never touches the pool, and never owns a
    transaction. `asyncpg.Record` never escapes this class.

    Both statements below project the same six columns and spell them out
    twice rather than sharing an interpolated fragment. Every statement in
    this repository is therefore a complete literal that can be read, pasted
    into psql and reasoned about on its own, and no SQL in this codebase is
    assembled from Python strings -- a property worth more than the six
    duplicated lines, because "this one interpolation is only a constant" is
    the shape every SQL injection starts as.

    The workspace columns are aliased. `workspaces.id` is unambiguous in SQL
    but arrives in an asyncpg.Record keyed by the bare column name, so an
    unaliased `workspaces.id` beside a future `workspace_members.id` would
    collide silently in `_to_entity`.
    """

    async def find_membership(
        self,
        connection: asyncpg.Connection,
        *,
        slug: str,
        user_id: UUID,
    ) -> WorkspaceMembershipEntity | None:
        """The caller's membership of the workspace with this slug, or nothing.

        One statement, and that is the security property rather than a
        performance note. Resolving the workspace first and then checking
        membership would compute, in this process, the difference between "no
        such workspace" and "not yours" -- and once a frame knows that
        difference it can leak it: into an error type, into a log line a
        support tool later exposes, into two response times a stranger can
        tell apart. Joining instead means the absent row has one meaning and
        the server never holds the other.

        The join is inner and the filter is two equalities, both bound as
        parameters. The slug comparison is exact for the reason spelled out on
        `WorkspaceRepository.find_id_by_slug`: `workspaces_slug_format`
        confines every stored slug to lowercase, so folding case here would be
        case-insensitive addressing of tenants.

        No LIMIT 1: `workspace_members_pkey` is on (workspace_id, user_id) and
        `workspaces_slug_key` makes a slug resolve to at most one workspace,
        so a second matching row is not a thing this schema can hold. A limit
        would claim doubt about a guarantee the schema already gives.

        Keyword-only, because these two parameters are the whole authorization
        question and transposing them at a call site is the one mistake here
        that would still typecheck at every layer above.

        `removed_at IS NULL` is the third equality and the one carrying the
        most weight. Since 026 a membership that has ended is a row that is
        still there, so without this predicate a removed member would keep
        every permission they had -- and `AuthorizedWorkspaceScope`, whose
        whole claim is "this came out of `workspace_members`", would still be
        telling the truth while meaning nothing. It sits inside the same
        statement as the other two so that a former member and a stranger
        produce the same absent row, and this frame never holds the difference.
        """
        row = await connection.fetchrow(
            """
            SELECT
                workspaces.id AS workspace_id,
                workspaces.slug AS workspace_slug,
                workspaces.name AS workspace_name,
                workspace_members.user_id AS user_id,
                workspace_members.role AS role,
                workspace_members.created_at AS created_at
            FROM workspace_members
            JOIN workspaces
                ON workspaces.id = workspace_members.workspace_id
            WHERE workspaces.slug = $1
                AND workspace_members.user_id = $2
                AND workspace_members.removed_at IS NULL
            """,
            slug,
            user_id,
        )

        if row is None:
            return None

        return self._to_entity(row)

    async def list_for_user(
        self,
        connection: asyncpg.Connection,
        *,
        user_id: UUID,
        limit: int,
    ) -> list[WorkspaceMembershipEntity]:
        """Every workspace this user belongs to, by slug, bounded by `limit`.

        Ordered by slug, which is a total order needing no tiebreak because
        `workspaces_slug_key` is unique. That matters beyond determinism: slug
        is the one column here whose order is stable under an update, so the
        day this list needs a cursor, the ordering it already has is the one
        the cursor can be built on.

        `limit` is required rather than defaulted, so the bound is a decision
        the service states out loud; see MEMBERSHIP_LIST_LIMIT for what it is
        and why it exists at all.

        The user id is the only filter that a caller supplies. There is
        deliberately no workspace argument: this answers "which workspaces are
        mine", and a caller after one named workspace asks `find_membership`,
        which is the lookup that cannot distinguish absent from unauthorized.

        `removed_at IS NULL` is not optional here either. This is the workspace
        switcher, so a former member who kept it would see a tenant they can no
        longer open -- every entry in the list is a place they would be refused
        on arrival.
        """
        rows = await connection.fetch(
            """
            SELECT
                workspaces.id AS workspace_id,
                workspaces.slug AS workspace_slug,
                workspaces.name AS workspace_name,
                workspace_members.user_id AS user_id,
                workspace_members.role AS role,
                workspace_members.created_at AS created_at
            FROM workspace_members
            JOIN workspaces
                ON workspaces.id = workspace_members.workspace_id
            WHERE workspace_members.user_id = $1
                AND workspace_members.removed_at IS NULL
            ORDER BY workspaces.slug
            LIMIT $2
            """,
            user_id,
            limit,
        )

        return [self._to_entity(row) for row in rows]

    async def create(
        self,
        connection: asyncpg.Connection,
        *,
        workspace_id: UUID,
        user_id: UUID,
        role: str,
    ) -> WorkspaceMembershipEntity | None:
        """Grant a membership, or nothing if this account already holds one.

        One statement, so the grant and the description of it come from the
        same snapshot. The alternative -- INSERT, then SELECT the workspace --
        is two round trips whose second one can be answered by a workspace row
        another transaction has since renamed.

        A data-modifying CTE rather than a plain `RETURNING`, because
        `workspace_members` holds no slug and no name: the caller needs both,
        and a join is the only way to get them alongside the row just written.
        A workspace inserted earlier in the caller's own transaction is
        visible to this statement, which is what lets workspace creation and
        the owner's grant be one transaction.

        The ON CONFLICT clause exists entirely because of 026. A removed member
        keeps their row, so the primary key stays occupied after they leave,
        and a plain INSERT would make re-inviting somebody who once left
        impossible -- the grant would collide, the service would report
        "already a member" of a workspace they cannot open, and the invitation
        would roll back so the same token could be presented forever with the
        same answer. Rejoining is an ordinary thing to do, so the conflict
        revives the row instead.

        The `WHERE` on the conflict action is load-bearing. Without it, a
        second grant to a CURRENT member would quietly rewrite their role --
        which is a privilege change performed by whoever holds any invitation
        to a workspace the account is already in. With it, that conflict
        updates no row, and this method returns None.

        None therefore means exactly "already an active member", and it is the
        only thing it can mean: the INSERT and the revival both return a row,
        and every other failure still raises. Distinguishing that from a
        successful grant is the service's job, as it was when the same case
        arrived as a UniqueViolationError.

        `created_at` is reset on a revival, deliberately. It is the date the
        product shows as "joined", and carrying the original one forward would
        have it span a stretch during which this person was not in the
        workspace at all -- a membership that reads as continuous when it was
        not. What is being revived is the row, not the history of the row.
        `ON CONFLICT DO UPDATE` touches only the columns named, so this has to
        be said rather than assumed.
        """
        row = await connection.fetchrow(
            """
            WITH granted AS (
                INSERT INTO workspace_members (workspace_id, user_id, role)
                VALUES ($1, $2, $3)
                ON CONFLICT ON CONSTRAINT workspace_members_pkey DO UPDATE
                    SET role = EXCLUDED.role,
                        removed_at = NULL,
                        created_at = now()
                    WHERE workspace_members.removed_at IS NOT NULL
                RETURNING workspace_id, user_id, role, created_at
            )
            SELECT
                workspaces.id AS workspace_id,
                workspaces.slug AS workspace_slug,
                workspaces.name AS workspace_name,
                granted.user_id AS user_id,
                granted.role AS role,
                granted.created_at AS created_at
            FROM granted
            JOIN workspaces
                ON workspaces.id = granted.workspace_id
            """,
            workspace_id,
            user_id,
            role,
        )

        if row is None:
            return None

        return self._to_entity(row)

    async def list_members(
        self,
        connection: asyncpg.Connection,
        *,
        workspace_id: UUID,
        limit: int,
    ) -> list[WorkspaceMemberEntity]:
        """Everyone in one workspace, with the account behind each membership.

        The caller must already have established that the requester belongs to
        this workspace; a workspace id alone is not permission to read who is
        in it, and nothing in this statement checks. See
        `MembershipService.list_members`, the only caller, which takes an
        AuthorizedWorkspaceScope precisely so that the check cannot be skipped.

        Ordered by email, which is total without a tiebreak because
        `users_email_key` is unique -- `name` is nullable and not unique, so
        ordering by it would reshuffle the list between reads. A client that
        wants people sorted by display name sorts them.

        `limit` is required rather than defaulted, so the bound is a decision
        the service states out loud; see MEMBERSHIP_LIST_LIMIT.

        Former members are INCLUDED, and `removed_at` is projected so a caller
        can tell them apart. That is the one read of this table that does not
        filter on `removed_at IS NULL`, and it is deliberate: this list has two
        readers who need opposite halves of it. An assignee picker needs the
        people who are here, and a comment thread needs a name for whoever
        wrote each entry -- including the ones who left, which is the whole
        reason 026 keeps their row. Returning only the active ones would make
        every screen showing both fetch a second list and merge it, and the
        merge is where a missing author starts rendering as nothing at all.

        Filtering is therefore the caller's, on a field the row carries rather
        than on an absence it has to interpret. Nothing about permission is
        being delegated here: `list_members` is reached only through an
        AuthorizedWorkspaceScope, and a former member's own access is refused
        by `find_membership`, which never returns their row to begin with.
        """
        rows = await connection.fetch(
            """
            SELECT
                workspace_members.user_id AS user_id,
                users.email AS email,
                users.name AS name,
                workspace_members.role AS role,
                workspace_members.created_at AS created_at,
                workspace_members.removed_at AS removed_at
            FROM workspace_members
            JOIN users
                ON users.id = workspace_members.user_id
            WHERE workspace_members.workspace_id = $1
            ORDER BY users.email
            LIMIT $2
            """,
            workspace_id,
            limit,
        )

        return [self._to_member_entity(row) for row in rows]

    async def lock_owner_ids(
        self,
        connection: asyncpg.Connection,
        *,
        workspace_id: UUID,
        owner_role: str,
    ) -> list[UUID]:
        """The workspace's owners, locked until the caller's transaction ends.

        FOR UPDATE is the whole point of this method, and the reason "never
        leave a workspace without an owner" cannot be a count taken before the
        write. Two transactions each demoting a different one of two owners
        both read a count of two, both decide they are safe, and both commit --
        leaving nobody. Under READ COMMITTED, PostgreSQL re-evaluates
        `role = $2` after acquiring the row lock, so the second transaction
        sees the first one's demotion and finds one owner rather than two.

        The caller MUST already be inside the transaction that performs the
        write. A lock taken in a transaction of its own is released before the
        write it was meant to protect.

        `removed_at IS NULL` is in the predicate rather than applied to the
        result, and it matters for the same reason `role = $2` is: a former
        owner counted here is an owner who cannot administer anything, so a
        workspace whose only remaining owner had left would look safe to demote
        the last real one. PostgreSQL re-evaluates the whole predicate after
        the row lock under READ COMMITTED, so a concurrent removal is seen by
        this statement exactly as a concurrent demotion is.
        """
        rows = await connection.fetch(
            """
            SELECT user_id
            FROM workspace_members
            WHERE workspace_id = $1
                AND role = $2
                AND removed_at IS NULL
            FOR UPDATE
            """,
            workspace_id,
            owner_role,
        )

        return [row["user_id"] for row in rows]

    async def update_role(
        self,
        connection: asyncpg.Connection,
        *,
        workspace_id: UUID,
        user_id: UUID,
        role: str,
    ) -> WorkspaceMemberEntity | None:
        """Set one member's role, or nothing if there is no such member here.

        Scoped by workspace as well as by user: without that predicate a user
        id from another tenant would have its role rewritten there, which is a
        cross-tenant write performed by a lookup that never looked anything up.

        Returns None rather than raising when nothing matched. Whether that
        means "no such account", "not a member here" or "a member who has
        since left" is a question for the service, and the repository does not
        answer questions about what an absence means -- which is also why
        `removed_at IS NULL` belongs in this predicate and not in a check
        beforehand. A former member has no role to change; granting them one
        would produce a row that is authorized by nothing and looks promoted.
        """
        row = await connection.fetchrow(
            """
            WITH updated AS (
                UPDATE workspace_members
                SET role = $3
                WHERE workspace_id = $1
                    AND user_id = $2
                    AND removed_at IS NULL
                RETURNING user_id, role, created_at, removed_at
            )
            SELECT
                updated.user_id AS user_id,
                users.email AS email,
                users.name AS name,
                updated.role AS role,
                updated.created_at AS created_at,
                updated.removed_at AS removed_at
            FROM updated
            JOIN users
                ON users.id = updated.user_id
            """,
            workspace_id,
            user_id,
            role,
        )

        if row is None:
            return None

        return self._to_member_entity(row)

    async def mark_removed(
        self,
        connection: asyncpg.Connection,
        *,
        workspace_id: UUID,
        user_id: UUID,
    ) -> UUID | None:
        """End a membership, returning the user id it named, or nothing.

        Scoped by workspace for the reason `update_role` gives.

        The row is stamped rather than deleted, and 026 argues that at length.
        The short version: seventeen foreign keys reference this table and
        seven of them are authorship -- a comment, a document, a project update
        -- which cannot be deleted, cannot be reassigned to anybody, and sit in
        migrations that are applied and therefore immutable. Deleting the row
        is the one operation all seventeen refuse, so the membership ends by
        ceasing to be current instead of by ceasing to exist. This method's
        earlier name, `delete`, is gone along with the statement, because a
        method still called that would be describing what it no longer does.

        `removed_at IS NULL` in the predicate makes this idempotent in the only
        sense that matters: removing an already-removed member matches nothing
        and answers None, exactly as an id naming nobody does, rather than
        moving the timestamp forward and reporting a second departure.

        `now()` and not a value from the caller. The stamp is the moment the
        server committed the removal, which is a fact the database is holding
        the clock for; a timestamp passed in would be whenever the process that
        built it thought it was.
        """
        removed = await connection.fetchval(
            """
            UPDATE workspace_members
            SET removed_at = now()
            WHERE workspace_id = $1
                AND user_id = $2
                AND removed_at IS NULL
            RETURNING user_id
            """,
            workspace_id,
            user_id,
        )

        if removed is None:
            return None

        # Annotated rather than returned inline: asyncpg ships no types, so
        # `fetchval` is Any and would silently satisfy any return type.
        removed_id: UUID = removed

        return removed_id

    async def delete_personal_rows(
        self,
        connection: asyncpg.Connection,
        *,
        workspace_id: UUID,
        user_id: UUID,
    ) -> None:
        """Clear what a departing member owns alone, before the membership goes.

        The four statements below are the rows that answer to exactly one
        person: their notification feed, the issues they watch, their sidebar
        shortcuts, and the saved views only they can open. None of them is a
        record of anything the workspace did; deleting them destroys no history
        and changes nothing another member can observe.

        Since 026 the membership row itself survives a removal, so none of this
        is still required to satisfy a RESTRICT -- these deletions would all
        succeed if they never ran. They run because leaving a workspace should
        stop it filling an inbox: a former member who kept their subscriptions
        would go on being notified about issues they can no longer open. What
        changed is only the reason; the four statements are the same four.

        Shared rows are deliberately NOT touched here. A view the whole
        workspace uses is not personal property, and dropping it because its
        author left would take a working list away from everyone. It is
        `shared_holdings` below that finds those, and
        `MembershipService.remove_member` refuses the removal by name until
        somebody reassigns them -- which is the answer 009 gives for a project
        lead.

        Why this lives on the membership repository rather than on four others:
        each statement is keyed on `(workspace_id, user_id)` and none of them is
        about notifications or favourites as such -- they are the lifecycle of a
        membership, which is this class's subject. Reaching for four
        repositories would also put four more constructor arguments on
        MembershipService for a single code path.

        Order is load-bearing. `favorites_saved_view_fk` is RESTRICT like
        everything else, so a favourite pointing at one of this member's
        personal views has to go before the view does. A personal view is
        visible only to its creator, so their own favourites are the only ones
        that can reference it -- which is what makes deleting favourites first
        sufficient rather than merely likely.

        No return value. Every statement is idempotent and a member with none of
        these rows is the ordinary case, so "how many were deleted" is not a
        question any caller has.
        """
        await connection.execute(
            "DELETE FROM favorites WHERE workspace_id = $1 AND user_id = $2",
            workspace_id,
            user_id,
        )
        await connection.execute(
            "DELETE FROM issue_subscribers WHERE workspace_id = $1 AND user_id = $2",
            workspace_id,
            user_id,
        )
        await connection.execute(
            "DELETE FROM notifications WHERE workspace_id = $1 AND user_id = $2",
            workspace_id,
            user_id,
        )
        await connection.execute(
            """
            DELETE FROM saved_views
            WHERE workspace_id = $1
              AND created_by = $2
              AND visibility = 'personal'
            """,
            workspace_id,
            user_id,
        )

    async def shared_holdings(
        self,
        connection: asyncpg.Connection,
        *,
        workspace_id: UUID,
        user_id: UUID,
    ) -> list[str]:
        """Which shared things still name this member, by constraint name.

        Before 026 this question was PostgreSQL's. Removal was a DELETE, every
        foreign key onto `workspace_members` was ON DELETE RESTRICT, and a
        member who still led a project had the deletion refused by
        `projects_lead_fk` -- for free, with no application code involved.
        Since the row now survives, no RESTRICT fires on departure and that
        refusal has to be asked for. This statement is the asking.

        The names it returns are the constraints that WOULD have refused. That
        is not nostalgia: the constraint is where the policy is written down --
        009 for a project lead, 013 and 014 for the two integrations -- so the
        name is what a reader follows to the argument, and it is already what
        `MembershipService._REMOVAL_BLOCKED` is keyed on. The mapping there did
        not have to change when the mechanism underneath it did.

        Six probes, one statement, one round trip, and all six are evaluated
        rather than short-circuited. An admin about to remove somebody wants
        the whole list of what to reassign, not the first item and then another
        attempt; each EXISTS stops at its own first row, so the cost of asking
        for all of them is six index probes against tables whose migrations
        already index this exact column -- see the closing note in 026.

        `saved_views` is the one probe with a third predicate. A personal view
        answers to its creator alone and `delete_personal_rows` above takes it
        with the membership; only a shared one is a list other people open, and
        only that one is worth refusing a removal over.

        `issues` is deliberately absent. `issues.assignee_id` is nullable, so
        an assignment is vacated rather than defended -- see `unassign_issues`.
        So are the seven authorship keys, which is the entire point of 026: a
        comment somebody wrote is not a holding they can hand over.

        Returns a plain list of names. `asyncpg.Record` does not escape: the
        row is read here against a module-level tuple that fixes the order, so
        the same set of blockers always reaches a client in the same sequence.
        """
        row = await connection.fetchrow(
            """
            SELECT
                EXISTS (
                    SELECT 1 FROM projects
                    WHERE workspace_id = $1 AND lead_id = $2
                ) AS projects_lead_fk,
                EXISTS (
                    SELECT 1 FROM initiatives
                    WHERE workspace_id = $1 AND owner_id = $2
                ) AS initiatives_owner_fk,
                EXISTS (
                    SELECT 1 FROM saved_views
                    WHERE workspace_id = $1
                      AND created_by = $2
                      AND visibility = 'shared'
                ) AS saved_views_creator_fk,
                EXISTS (
                    SELECT 1 FROM issue_templates
                    WHERE workspace_id = $1 AND assignee_id = $2
                ) AS issue_templates_assignee_fk,
                EXISTS (
                    SELECT 1 FROM github_installations
                    WHERE workspace_id = $1 AND connected_by = $2
                ) AS github_installations_connected_by_fk,
                EXISTS (
                    SELECT 1 FROM slack_installations
                    WHERE workspace_id = $1 AND connected_by_user_id = $2
                ) AS slack_installations_connected_by_fk
            """,
            workspace_id,
            user_id,
        )

        return [name for name in SHARED_HOLDINGS if row[name]]

    async def unassign_issues(
        self,
        connection: asyncpg.Connection,
        *,
        workspace_id: UUID,
        user_id: UUID,
    ) -> None:
        """Hand back every issue assigned to a departing member.

        This statement is what `issues_assignee_fk` used to do. 006 declared it
        `ON DELETE SET NULL (assignee_id)` -- alone among the seventeen keys
        onto this table, and for the reason 006 gives: an assignment is a
        statement about who is doing the work now, so when the person leaves it
        is vacated rather than defended. Since 026 stopped deleting the row
        that clause never fires, and the behaviour it encoded moved here.

        Not into `delete_personal_rows`: an assignment is not personal
        property. It is work the workspace still wants doing, and what happens
        to it is that it goes back on the pile for somebody to pick up --
        which is a different verb and worth a different method.

        No return value, for the same reason that one has none. Reassigning
        nothing is the ordinary case.

        ponytail: this vacates existing assignments and does not stop new ones.
        While removal was a DELETE, `issues_assignee_fk` also guaranteed that
        an assignee was a CURRENT member; now that the row survives, the
        constraint is satisfied by somebody who left, so a client that sends a
        former member's id gets the assignment. That is a stale picker rather
        than a breach -- the id still has to belong to this workspace, the
        composite key sees to that, and the person it names cannot open the
        issue either way. Left as a ceiling because closing it properly means a
        check in each of the three services that write an assignee
        (`issues`, `bulk`, `templates`), and the honest single place for it is
        a trigger on `issues` that nobody wants on that write path. Add the
        three checks if assignment-to-a-leaver is ever observed in practice.
        """
        await connection.execute(
            """
            UPDATE issues
            SET assignee_id = NULL
            WHERE workspace_id = $1 AND assignee_id = $2
            """,
            workspace_id,
            user_id,
        )

    @staticmethod
    def _to_entity(row: asyncpg.Record) -> WorkspaceMembershipEntity:
        return WorkspaceMembershipEntity(
            workspace_id=row["workspace_id"],
            workspace_slug=row["workspace_slug"],
            workspace_name=row["workspace_name"],
            user_id=row["user_id"],
            role=row["role"],
            created_at=row["created_at"],
        )

    @staticmethod
    def _to_member_entity(row: asyncpg.Record) -> WorkspaceMemberEntity:
        return WorkspaceMemberEntity(
            user_id=row["user_id"],
            email=row["email"],
            name=row["name"],
            role=row["role"],
            created_at=row["created_at"],
            removed_at=row["removed_at"],
        )
