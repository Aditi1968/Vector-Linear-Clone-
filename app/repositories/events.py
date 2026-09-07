"""SQL access for `domain_events`, the notification pipeline's outbox.

Two halves with two shapes.

The WRITE half -- `record_*` -- takes the CALLER'S connection, because an
event that is not written in the same transaction as the change it describes
is a lie waiting to happen in both directions: the change rolls back and Slack
announces it anyway, or the change commits and the announcement is lost because
something failed between the two. Passing the connection is how the layering
rule -- services own transactions, repositories receive connections -- delivers
atomicity here. The same argument `ActivityRepository` makes for issue history.

The CLAIM/FINISH half -- `claim_next`, `mark_delivered`, `mark_not_delivered`
-- also takes a connection, and is called by the delivery service inside three
SHORT transactions of its own rather than one long one. That is not a style
choice: the Slack call happens between the claim and the finish, and holding a
pool connection across a network call is how a settings page takes the rest of
the product down with it. `SlackService.sync_channels` states the same rule.

Every statement here derives what a message says from columns rather than from
anything a caller passed -- the identifier from `teams.key` and `issues.number`,
the route from `workspaces.slug`. There is deliberately no parameter through
which a caller could supply a path, because a path is what a message links to.
"""

from datetime import timedelta
from uuid import UUID

import asyncpg

from app.domain.events import DeliveryState, DomainEventEntity, DomainEventKind
from app.domain.tenancy import WorkspaceScope


# The columns an event is claimed back as, shared by the one statement that
# returns them. Interpolated from a module-level literal; every value below
# arrives as $n.
#
# `slack_attempts AS attempts` is the one rename, and it is a deliberate one
# rather than shorthand. The COLUMN is prefixed because migration 027 keeps one
# adapter's delivery state on the event row and the prefix is what says whose;
# the ENTITY has no such ambiguity, because an entity handed to the Slack
# delivery path is only ever about that delivery. Aliasing here is what lets
# both names be right at once.
_CLAIMED_COLUMNS = """
                e.workspace_id,
                e.kind,
                e.dedupe_key,
                e.subject,
                e.summary,
                e.path,
                e.slack_attempts AS attempts
"""

# An event about one issue, as a template with one hole.
#
# The identifier ("ENG-142") and the route are composed HERE, from the team's
# key, the issue's number and the workspace's slug, rather than passed in.
# Three things follow and all three are the reason for the shape:
#
#   * a caller cannot supply the link a message points at;
#   * the row is written from the issue as it stands inside the caller's
#     transaction, so a title captured here is the title the change left
#     behind rather than whichever one is current when Slack is eventually
#     reachable;
#   * an issue in another workspace matches no row at all, because
#     `i.workspace_id = $1` is an equality in the WHERE clause and not a check
#     applied afterwards.
#
# `left(i.title, 300)` rather than a validation. `domain_events_summary_length`
# bounds the column, and a workspace whose issue titles are longer than a Slack
# message usefully carries should get a short message rather than a delivery
# that fails on a CHECK -- the same judgement `_channels_from_response` makes
# about a channel name.
#
# ON CONFLICT DO NOTHING, and not a bare INSERT that lets the key violation
# fly. The primary key is what makes a repeat unstorable; this clause is how a
# writer USES that key from inside somebody else's transaction. In PostgreSQL a
# constraint violation aborts the whole transaction, so "catch it and carry on"
# is not available -- a second report of a merge would take down the delivery
# that reported it. `SubscriberRepository.subscribe` absorbs a duplicate for
# exactly this reason.
_ISSUE_EVENT_TEMPLATE = """
    INSERT INTO domain_events (
        workspace_id, kind, dedupe_key, issue_id, subject, summary, path
    )
    SELECT
        i.workspace_id,
        $2::TEXT,
        $3::TEXT,
        i.id,
        t.key || '-' || i.number,
        left(i.title, 300),
        '/' || w.slug || '/issues/' || i.id
    FROM issues AS i
    JOIN teams AS t
        ON t.workspace_id = i.workspace_id AND t.id = i.team_id
    JOIN workspaces AS w
        ON w.id = i.workspace_id
    {completed}
    WHERE i.workspace_id = $1 AND i.id = $4
    ON CONFLICT DO NOTHING
"""

# The extra join that makes "completed" a fact the STATEMENT establishes.
#
# `record_changes` knows an issue's workflow state moved; it does not know
# whether the state it moved to is a completed one, and `IssueSnapshot` carries
# ids rather than types. The alternatives were a second round trip to look the
# type up, or widening the snapshot -- which is a field added to a type six
# services construct. This join costs neither: the same statement that would
# write the row is the one that decides there is no row to write, exactly as
# `NotificationRepository.notify_about_issue` derives its recipients.
_COMPLETED_JOIN = """
    JOIN workflow_states AS s
        ON s.workspace_id = i.workspace_id
        AND s.id = i.workflow_state_id
        AND s.type = 'completed'
"""

_ISSUE_EVENT_STATEMENTS = {
    False: _ISSUE_EVENT_TEMPLATE.format(completed=""),
    True: _ISSUE_EVENT_TEMPLATE.format(completed=_COMPLETED_JOIN),
}

# An event about one project, as a template with one hole.
#
# `summary` is a parameter here where the issue statement derives it, and the
# asymmetry is deliberate: what a project update is ABOUT is the text the
# author wrote and the health they reported, neither of which is a column on
# `projects` at the moment this runs. The subject and the route are still
# derived, so the thing a caller cannot supply is still the link.
_PROJECT_EVENT_TEMPLATE = """
    INSERT INTO domain_events (
        workspace_id, kind, dedupe_key, subject, summary, path
    )
    SELECT
        p.workspace_id,
        $2::TEXT,
        $3::TEXT,
        left(p.name, 200),
        left($4::TEXT, 300),
        '/' || w.slug || '/projects/' || p.id
    FROM projects AS p
    JOIN workspaces AS w
        ON w.id = p.workspace_id
    WHERE p.workspace_id = $1 AND p.id = $5
        {health}
    ON CONFLICT DO NOTHING
"""

# What makes a health change an event only when the health actually MOVED.
#
# Compared against the STORED value, which means this statement has to run
# before `ProjectService.post_update` stamps the new one -- and it does. An
# update that reports the same health a project already had is a report and not
# a change, and a channel that announced "still on track" every week is the
# noise migration 018 keeps its vocabulary short to avoid.
_HEALTH_MOVED_PREDICATE = "AND p.health IS DISTINCT FROM $6::TEXT"

_PROJECT_EVENT_STATEMENTS = {
    False: _PROJECT_EVENT_TEMPLATE.format(health=""),
    True: _PROJECT_EVENT_TEMPLATE.format(health=_HEALTH_MOVED_PREDICATE),
}


class EventRepository:
    """SQL access for `domain_events`.

    The repository receives a connection from the service layer. It never
    acquires connections, never touches the pool, and never owns a transaction.
    `asyncpg.Record` never escapes this class.

    Every write is scoped to one workspace by an equality in the statement, and
    every read of a claimed row carries the workspace it belongs to back out --
    so the delivery service looks up a channel under the id the DATABASE
    matched rather than one anything else supplied.
    """

    async def record_about_issue(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        kind: DomainEventKind,
        dedupe_key: str,
        issue_id: UUID,
        only_when_completed: bool = False,
    ) -> None:
        """Emit one event about one issue, on the caller's connection.

        Call this INSIDE the transaction that performs the change, never after
        it. The whole guarantee is that the two commit together.

        Nothing is returned, and no error is raised for an issue that matched
        nothing. Both are ordinary: an issue in another workspace produces no
        row, and so does a state move to a state that is not a completed one.
        A caller mid-write has nothing to do with either answer, and handing
        one back would invite a resolver to report it -- which is how an
        internal record becomes part of a mutation's contract.

        `only_when_completed` selects the variant that additionally requires
        the issue's current workflow state to be a completed one. It is a
        statement selector and not a rule the caller applies, which is what
        keeps "what counts as completed" in one place instead of in every
        service that moves an issue.
        """
        await connection.execute(
            _ISSUE_EVENT_STATEMENTS[only_when_completed],
            scope.workspace_id,
            kind.value,
            dedupe_key,
            issue_id,
        )

    async def record_pull_request_merged(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        repository_id: int,
        number: int,
        title: str,
    ) -> None:
        """Emit one event per Vector issue a merged pull request is about.

        One row per LINKED ISSUE rather than one per pull request, and the
        difference is what the message can say. A Vector channel wants "ENG-142
        is done, here is the issue"; a bare pull-request link is what GitHub's
        own Slack app already posts, and posting it again is two integrations
        announcing one merge. A merge that names no Vector issue therefore
        produces no row -- there is no Vector route to link to, and inventing
        one would be a message whose link goes nowhere useful.

        `SELECT DISTINCT` because `github_pull_request_issues` carries one row
        per SOURCE: a pull request whose title and whose branch both name
        ENG-142 is two link rows and one merge.

        The dedupe key is the repository, the number and the issue, which is
        what makes this idempotent against the provider rather than against the
        transport. GitHub retries a delivery it did not see a 2xx for -- and
        `github_deliveries` already stops that one -- but it ALSO sends the
        whole `pull_request` object on every later edit of an already-merged
        pull request, each under a delivery id this server has never seen. Those
        are the duplicates this key refuses, and there is no other place they
        could be caught.

        The links are read under the workspace that owns the repository the
        delivery came from, so an identifier naming another tenant's issue
        resolves to no link row and therefore to no event. That is migration
        017's guarantee being relied on rather than re-implemented.
        """
        await connection.execute(
            """
            INSERT INTO domain_events (
                workspace_id, kind, dedupe_key, issue_id, subject, summary, path
            )
            SELECT DISTINCT
                link.workspace_id,
                $2::TEXT,
                $3::TEXT || '/' || link.issue_id::TEXT,
                link.issue_id,
                t.key || '-' || i.number,
                left($4::TEXT, 300),
                '/' || w.slug || '/issues/' || i.id
            FROM github_pull_request_issues AS link
            JOIN issues AS i
                ON i.workspace_id = link.workspace_id AND i.id = link.issue_id
            JOIN teams AS t
                ON t.workspace_id = i.workspace_id AND t.id = i.team_id
            JOIN workspaces AS w
                ON w.id = i.workspace_id
            WHERE link.workspace_id = $1
                AND link.repository_id = $5
                AND link.number = $6
            ON CONFLICT DO NOTHING
            """,
            scope.workspace_id,
            DomainEventKind.PULL_REQUEST_MERGED.value,
            f"{repository_id}/{number}",
            title,
            repository_id,
            number,
        )

    async def record_about_project(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        kind: DomainEventKind,
        dedupe_key: str,
        project_id: UUID,
        summary: str,
        only_when_health_moves_to: str | None = None,
    ) -> None:
        """Emit one event about one project, on the caller's connection.

        `only_when_health_moves_to` selects the variant that writes a row only
        if the project's STORED health differs from the value given -- so it
        has to be called before the new health is stamped. `ProjectService`
        does both writes in one transaction and this sits between them.

        Silent about whether it wrote anything, for the reason
        `record_about_issue` gives.
        """
        moved = only_when_health_moves_to is not None
        statement = _PROJECT_EVENT_STATEMENTS[moved]

        if moved:
            await connection.execute(
                statement,
                scope.workspace_id,
                kind.value,
                dedupe_key,
                summary,
                project_id,
                only_when_health_moves_to,
            )
        else:
            await connection.execute(
                statement,
                scope.workspace_id,
                kind.value,
                dedupe_key,
                summary,
                project_id,
            )

    async def claim_next(
        self,
        connection: asyncpg.Connection,
        *,
        retry_delay: timedelta,
        backoff_steps: int,
    ) -> DomainEventEntity | None:
        """Take the next due event, or None. Commit before doing any work.

        The claim IS the backoff. One statement increments the attempt counter
        and pushes `slack_next_attempt_at` forward together, so an event that
        fails -- or whose process dies mid-attempt -- is not due again until
        the delay has passed. A retry cannot hot-loop even if the loop above
        this has no sleep in it at all, and cannot hot-loop harder because a
        second process joined.

        `FOR UPDATE SKIP LOCKED` is what lets two processes drain the same
        backlog without either waiting on the other or both taking the same
        row. It is not what prevents a double post -- the UPDATE is: whichever
        transaction commits first has moved `slack_next_attempt_at` into the
        future, and the other re-evaluates its predicate against the updated
        row and matches nothing.

        Ordered by when the event became due rather than by when it happened,
        because a retried event has already had its turn and a fresh one has
        not. `LIMIT 1` and not a batch: the delivery that follows is a network
        call per row, and a batch claimed at once is a batch whose last row
        waits out every call before it -- with its claim already spent if the
        process dies in between.

        The workspace comes back with the row. Everything the delivery then
        reads -- the preference, the channel, the token -- is looked up under
        THAT id, which the database matched, rather than under one a caller
        supplied.
        """
        row = await connection.fetchrow(
            f"""
            WITH due AS (
                SELECT workspace_id, kind, dedupe_key
                FROM domain_events
                WHERE slack_state = 'pending'
                    AND slack_next_attempt_at <= now()
                ORDER BY slack_next_attempt_at
                LIMIT 1
                FOR UPDATE SKIP LOCKED
            )
            UPDATE domain_events AS e
            SET slack_attempts = e.slack_attempts + 1,
                slack_next_attempt_at = now()
                    + $1::interval * LEAST(e.slack_attempts + 1, $2::int)
            FROM due
            WHERE e.workspace_id = due.workspace_id
                AND e.kind = due.kind
                AND e.dedupe_key = due.dedupe_key
            RETURNING
{_CLAIMED_COLUMNS}
            """,
            retry_delay,
            backoff_steps,
        )

        if row is None:
            return None

        return self._to_entity(row)

    async def mark_delivered(
        self,
        connection: asyncpg.Connection,
        *,
        event: DomainEventEntity,
    ) -> None:
        """Record that Slack acknowledged this event.

        Reachable from exactly one place: after `post_message` returned without
        raising, which happens only for a Slack response that said `ok: true`.
        `domain_events_delivered_has_an_instant` is the floor under that -- the
        state cannot be written without the timestamp, and cannot be written
        alongside a failure reason -- so a service that caught an exception and
        called this would be refused by the database rather than trusted.
        """
        await connection.execute(
            """
            UPDATE domain_events
            SET slack_state = 'delivered',
                slack_delivered_at = now(),
                slack_failure = NULL
            WHERE workspace_id = $1 AND kind = $2 AND dedupe_key = $3
            """,
            event.workspace_id,
            event.kind.value,
            event.dedupe_key,
        )

    async def mark_not_delivered(
        self,
        connection: asyncpg.Connection,
        *,
        event: DomainEventEntity,
        state: DeliveryState,
        failure: str,
    ) -> None:
        """Close this event without a delivery, saying why.

        One method for `failed` and `skipped` because the write is identical
        and only the word differs; two would be two statements to keep in step
        about a column neither of them is really about. Which word to use is a
        product decision and lives in the service -- see `SlackNotifier` on why
        a disabled preference is not an error.

        `slack_delivered_at` is cleared rather than left, so a row can never
        carry a delivery instant it did not earn. Today nothing could have set
        one, and `domain_events_delivered_has_an_instant` would refuse the row
        if it had.
        """
        await connection.execute(
            """
            UPDATE domain_events
            SET slack_state = $4,
                slack_failure = $5,
                slack_delivered_at = NULL
            WHERE workspace_id = $1 AND kind = $2 AND dedupe_key = $3
            """,
            event.workspace_id,
            event.kind.value,
            event.dedupe_key,
            state.value,
            failure,
        )

    @staticmethod
    def _to_entity(row: asyncpg.Record) -> DomainEventEntity:
        return DomainEventEntity(
            workspace_id=row["workspace_id"],
            kind=DomainEventKind(row["kind"]),
            dedupe_key=row["dedupe_key"],
            subject=row["subject"],
            summary=row["summary"],
            path=row["path"],
            attempts=row["attempts"],
        )
