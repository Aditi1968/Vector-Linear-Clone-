from uuid import UUID

import asyncpg

from app.domain.templates import IssueTemplateDraft, IssueTemplateEntity
from app.domain.tenancy import WorkspaceScope


# Every column an IssueTemplateEntity is built from, as one expression list
# shared by the two statements that return one -- and, through them, by the
# create and update paths, which read their result back rather than growing a
# third and fourth copy of this list in a RETURNING clause.
#
# `label_ids` is a correlated subquery rather than a join, so a template with
# no labels still produces a row -- a LEFT JOIN with a GROUP BY would do the
# same and would put the aggregate in the way of the ORDER BY that `list`
# needs. It is the alternative to a second round trip per template, which on a
# menu of twenty templates is the N+1 this shape exists to avoid.
#
# The subquery pins the label rows to the template's OWN workspace_id rather
# than to the parameter. That is already guaranteed by
# issue_template_labels_template_fk, and it is written anyway: a read that
# depends on a constraint holding, without saying so, is a read that silently
# starts crossing tenants if the constraint is ever relaxed.
#
# `coalesce(..., '{}')` because array_agg over no rows is NULL, and the entity
# holds a tuple. Ordered by label_id so two reads of one template answer in the
# same order -- the join table has no position column, so any order is as good
# as any other and only stability matters.
#
# Interpolated with an f-string, which is a rule about VALUES rather than about
# text: this is a module-level literal no input can influence, and the
# alternative is four copies of the same fourteen lines that drift the day a
# column is added to one of them. Every actual value below arrives as $n.
_TEMPLATE_COLUMNS = """
                template.id,
                template.team_id,
                template.name,
                template.title,
                template.description,
                template.priority,
                template.estimate,
                template.assignee_id,
                template.project_id,
                template.cycle_id,
                template.created_at,
                template.updated_at,
                (
                    SELECT coalesce(
                        array_agg(link.label_id ORDER BY link.label_id),
                        '{}'::uuid[]
                    )
                    FROM issue_template_labels AS link
                    WHERE link.workspace_id = template.workspace_id
                        AND link.template_id = template.id
                ) AS label_ids
"""


class TemplateRepository:
    """SQL access for `issue_templates` and its label join.

    One repository over two tables, because a template's labels are not a
    resource of their own: there is no operation that adds a label to a
    template without saving the template, and the join row is meaningless
    without the row it hangs from. That is the judgement `LabelService` makes
    about `labels` and `issue_labels`, made one layer down because here the two
    tables are written by one statement pair inside one transaction.

    The repository receives a connection from the service layer. It never
    acquires connections, never touches the pool, and never owns a
    transaction. `asyncpg.Record` never escapes this class.

    Every statement is scoped to one workspace and the scope arrives as a
    required keyword argument. For the writes it is the single column every
    composite foreign key on both tables reads, so a template naming another
    tenant's team, member, project, cycle or label has no workspace_id that
    satisfies both parents -- which is the guarantee the apply path is built
    on. Nothing here checks any of those ids first, deliberately: a SELECT
    ahead of the write would be a second, weaker copy of five rules.
    """

    async def create(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        draft: IssueTemplateDraft,
    ) -> IssueTemplateEntity:
        """Write one template and its labels, on the caller's connection.

        Three statements inside the caller's transaction: the row, its labels,
        and a read-back. The insert returns only the id, and the entity comes
        from `get`, because the labels do not exist yet when the INSERT's
        RETURNING is evaluated -- a RETURNING that computed `label_ids` would
        always answer empty. One extra statement on a path that runs once per
        template save, in exchange for one place that knows how an entity is
        built out of a row.

        Every foreign key violation propagates as itself. Deciding which of
        the five is the client's to fix is a question about the CLIENT's
        request, so it belongs in the service that has the request in hand --
        the split `IssueService._EXPECTED_FOREIGN_KEYS` makes.
        """
        template_id: UUID = await connection.fetchval(
            """
            INSERT INTO issue_templates (
                workspace_id,
                team_id,
                name,
                title,
                description,
                priority,
                estimate,
                assignee_id,
                project_id,
                cycle_id
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
            RETURNING id
            """,
            scope.workspace_id,
            draft.team_id,
            draft.name,
            draft.title,
            draft.description,
            draft.priority,
            draft.estimate,
            draft.assignee_id,
            draft.project_id,
            draft.cycle_id,
        )

        await self.replace_labels(
            connection,
            scope=scope,
            template_id=template_id,
            label_ids=draft.label_ids,
        )

        created = await self.get(connection, scope=scope, template_id=template_id)

        # The row was written by this transaction and is read back inside it,
        # so it cannot be absent. Asserting rather than returning
        # `IssueTemplateEntity | None` keeps the create path's type honest for
        # every caller instead of pushing an impossible None onto all of them.
        assert created is not None, "a template just written must be readable"

        return created

    async def update(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        template_id: UUID,
        draft: IssueTemplateDraft,
    ) -> IssueTemplateEntity | None:
        """Replace one template wholesale; None if this workspace has no such
        id.

        Every column is written, including the ones the draft left as None.
        That is what makes a save a REPLACE rather than a patch, and it is the
        reason there is no UNSET machinery in `IssueTemplateDraft`: clearing a
        default is sending null, which is the same operation as setting one.

        The workspace is part of the predicate, not a check on the row
        afterwards, so a template belonging to another tenant matches nothing
        and answers None -- exactly as an id that exists nowhere does.

        `updated_at` is written explicitly. The trigger that will eventually
        maintain the column does not exist yet, and when it does, setting the
        same value here is harmless.
        """
        updated = await connection.fetchval(
            """
            UPDATE issue_templates
            SET team_id = $3,
                name = $4,
                title = $5,
                description = $6,
                priority = $7,
                estimate = $8,
                assignee_id = $9,
                project_id = $10,
                cycle_id = $11,
                updated_at = now()
            WHERE workspace_id = $1 AND id = $2
            RETURNING id
            """,
            scope.workspace_id,
            template_id,
            draft.team_id,
            draft.name,
            draft.title,
            draft.description,
            draft.priority,
            draft.estimate,
            draft.assignee_id,
            draft.project_id,
            draft.cycle_id,
        )

        if updated is None:
            return None

        await self.replace_labels(
            connection,
            scope=scope,
            template_id=template_id,
            label_ids=draft.label_ids,
        )

        return await self.get(connection, scope=scope, template_id=template_id)

    async def replace_labels(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        template_id: UUID,
        label_ids: tuple[UUID, ...],
    ) -> None:
        """Make this template's labels exactly `label_ids`.

        Delete-then-insert rather than a difference, because the set is at most
        a handful of rows and computing which ones moved would be more code
        than rewriting all of them -- and both statements are inside the
        caller's transaction, so no reader sees the gap between them.

        The DELETE is scoped to the workspace as well as the template. The
        template id alone would be enough given `issue_template_labels_pkey`,
        and writing the tenant anyway is what makes this statement correct on
        its own terms rather than correct because of a key declared elsewhere.

        `executemany` rather than an unnested array, because the failing row of
        a multi-row insert is not identifiable from the error -- and the one
        error this can raise, `issue_template_labels_label_fk`, is a client
        naming a label that is not in this workspace.
        """
        await connection.execute(
            """
            DELETE FROM issue_template_labels
            WHERE workspace_id = $1 AND template_id = $2
            """,
            scope.workspace_id,
            template_id,
        )

        if not label_ids:
            return

        await connection.executemany(
            """
            INSERT INTO issue_template_labels (workspace_id, template_id, label_id)
            VALUES ($1, $2, $3)
            """,
            [(scope.workspace_id, template_id, label_id) for label_id in label_ids],
        )

    async def delete(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        template_id: UUID,
    ) -> bool:
        """Discard one template and its labels; True if a row went.

        The label rows go first, in the caller's transaction. They have to:
        `issue_template_labels_template_fk` is RESTRICT, so the template
        deletion is refused while any of them stand -- which is the constraint
        working as 009 describes for project_teams, as a guard on this ordering
        rather than an obstacle to it.

        The SCHEDULE goes first for the same reason and is NOT deleted here:
        `issue_recurrences_template_fk` is RESTRICT too, so the caller removes
        it through `RecurrenceRepository.delete` in this same transaction
        before calling this. Doing it here instead would mean this class held
        SQL against a table it does not own -- and would put "deleting a
        template silently stops a schedule" in a statement nobody reading the
        recurrence feature would find.

        A template in another workspace deletes nothing and answers False,
        indistinguishable from an id that exists nowhere.
        """
        await connection.execute(
            """
            DELETE FROM issue_template_labels
            WHERE workspace_id = $1 AND template_id = $2
            """,
            scope.workspace_id,
            template_id,
        )

        deleted = await connection.fetchval(
            """
            DELETE FROM issue_templates
            WHERE workspace_id = $1 AND id = $2
            RETURNING id
            """,
            scope.workspace_id,
            template_id,
        )

        return deleted is not None

    async def get(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        template_id: UUID,
    ) -> IssueTemplateEntity | None:
        """One template from this workspace, or nothing.

        "Not in this workspace" and "does not exist" are the same answer, and
        that equivalence is what the apply path's tenancy rests on: a template
        id guessed or copied from another tenant resolves to nothing here, so
        nothing downstream ever holds another workspace's defaults to
        re-validate in the first place.
        """
        row = await connection.fetchrow(
            f"""
            SELECT
{_TEMPLATE_COLUMNS}
            FROM issue_templates AS template
            WHERE template.workspace_id = $1 AND template.id = $2
            """,
            scope.workspace_id,
            template_id,
        )

        if row is None:
            return None

        return self._to_entity(row)

    async def list_for_team(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        team_id: UUID | None,
        limit: int,
    ) -> list[IssueTemplateEntity]:
        """The templates one team may file from, in menu order.

        That set is the workspace's shared templates PLUS the team's own, which
        is why the predicate is an OR rather than an equality. `team_id` of
        None asks for the shared ones alone -- the "no team chosen yet" state
        of a create form -- and `$2::uuid` being NULL makes the second half of
        the OR false for every row, so one statement serves both without a
        second text.

        No cursor. See `TemplateService.list` for why a bounded cap is the
        right shape for a configuration list and what would have to change if
        that stopped being true.

        A team id from another workspace is not refused and is not reported: it
        selects no team-scoped rows, so the answer is the workspace's shared
        templates -- the same answer a real team with no templates of its own
        gets. Reporting otherwise would confirm that another tenant's team
        exists.
        """
        rows = await connection.fetch(
            f"""
            SELECT
{_TEMPLATE_COLUMNS}
            FROM issue_templates AS template
            WHERE template.workspace_id = $1
                AND (template.team_id IS NULL OR template.team_id = $2::uuid)
            ORDER BY template.name, template.id
            LIMIT $3
            """,
            scope.workspace_id,
            team_id,
            limit,
        )

        return [self._to_entity(row) for row in rows]

    @staticmethod
    def _to_entity(row: asyncpg.Record) -> IssueTemplateEntity:
        return IssueTemplateEntity(
            id=row["id"],
            team_id=row["team_id"],
            name=row["name"],
            title=row["title"],
            description=row["description"],
            priority=row["priority"],
            estimate=row["estimate"],
            assignee_id=row["assignee_id"],
            project_id=row["project_id"],
            cycle_id=row["cycle_id"],
            label_ids=tuple(row["label_ids"]),
            # Always None here, and filled in by `TemplateService` from
            # `RecurrenceRepository`. A schedule lives in its own table, so its
            # SQL belongs to the repository that owns that table -- the split
            # `ProjectService` makes when it reaches for `IssueRepository`, and
            # `SavedViewService` for `FavoriteRepository`.
            #
            # A LEFT JOIN into `_TEMPLATE_COLUMNS` would save a round trip and
            # was declined for that reason plus one more: `list_for_team`'s
            # shape is a plain ORDER BY over one table, and the day somebody
            # needs a second schedule column it would be added here, to a
            # statement about templates, by whoever was editing recurrences.
            recurrence=None,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
