-- Indexes for an issue list that is finally filtered and sorted in the
-- database instead of in the browser.
--
-- Nothing here changes a table, a column or a constraint. `issues` already
-- holds every value the new `issues(filter:, orderBy:)` reads; what it did not
-- hold was a way to READ them cheaply in any order but one. Four indexes, and
-- the argument for each is below it.
--
-- The filters mostly needed nothing. `teamId` has 002's
-- issues_workspace_team_idx, `workflowStateId` has 005's
-- issues_workflow_state_idx, `cycleId` has 008's issues_workspace_team_cycle_idx,
-- `projectId` has 009's issues_workspace_project_milestone_idx, and `labelId`
-- probes issue_labels_pkey from 007. That is not luck: every one of those
-- indexes exists because PostgreSQL indexes the REFERENCED side of a foreign
-- key and not the referencing side, so the shape a referential check needs --
-- (workspace_id, <the id>) -- is the same shape a tenant-scoped filter needs.
-- The two that were genuinely unserved are below.
--
-- What is deliberately NOT here is the combinatorial thing: one index per
-- (filter, ordering) pair is thirty-two indexes on the hottest-written table
-- in the product, paid on every insert and every edit, to save a sort of at
-- most a few hundred rows. A selective filter narrows to a set the planner
-- sorts in memory; a wide list needs the ordering index. Those are the two
-- cases, and four indexes cover them.
--
-- Apply this ONLY through `python -m scripts.apply_migration
-- migrations/015_issue_list_indexes.sql`. The runner wraps the file in one
-- transaction, takes an advisory lock and records a checksum. A hand-run gets
-- none of that and executes statement-at-a-time in autocommit, which here
-- means a subset of the indexes existing with nothing recording that the file
-- only half-ran -- and a subset is indistinguishable from the whole until the
-- one query that needed the missing one goes to a sequential scan in
-- production.
--
-- DEPENDS ON 001 for `priority` and `due_date`, 002 for `workspace_id`, and
-- 006 for `archived_at` and `updated_at`. Applying this before any of them
-- fails on a column that does not exist and -- inside the single transaction
-- the runner wraps the file in -- leaves nothing behind. That failure IS the
-- dependency check: the ledger records what has been applied, not what depends
-- on what.


-- "My Issues", which is the filtered list a person opens most days.
--
-- 006 created issues_workspace_assignee_idx and argued, in writing, against
-- widening it: "a keyset walk within one assignee is a query nothing issues
-- yet, and two more key columns is write amplification on every insert and
-- every reassignment, paid now against a page that does not exist." That was
-- correct then and is wrong now -- the page exists, and it is
-- `issues(filter: {assigneeId: ...})` with the default ordering. The narrow
-- index answers "which issues are Ana's" and then leaves the server to sort
-- them; this one answers "the next fifty of Ana's issues, newest first" by
-- walking the index and stopping.
--
-- Partial on `archived_at IS NULL`, copying 006's live-issue index and its
-- argument with it: archived is where issues END UP, so in the steady state
-- most rows in this table are ones the query must skip, and an unpartitioned
-- index would make the cost of one page grow with everything the workspace has
-- ever filed. Entries leave this index when an issue is archived.
--
-- It also serves `assigneeId: null` -- the unassigned queue. A btree indexes
-- NULLs, so `assignee_id IS NULL` is a range within this index rather than a
-- predicate applied after a scan, and the untriaged list is exactly as cheap
-- as one person's.
--
-- 006's issues_workspace_assignee_idx is NOT dropped. It is the prefix
-- issues_assignee_fk's `ON DELETE SET NULL` looks up when an account is
-- removed, and that check has to reach archived issues too -- which this
-- partial index deliberately omits.
CREATE INDEX issues_workspace_live_assignee_created_at_id_idx
    ON issues (workspace_id, assignee_id, created_at DESC, id DESC)
    WHERE archived_at IS NULL;


-- The three orderings the list can now be sorted by that no index answered.
--
-- Each is `(workspace_id, <key>, id)` with the tie-break in the key, because
-- the ordering the application emits is `<key> <dir>, id <dir>` -- the id is
-- what makes it total, and an index without it can order the page but cannot
-- settle a tie, so the server would sort anyway.
--
-- ONE index per key and not two, although the API offers both directions. A
-- btree scans either way, so an index declared descending serves the ascending
-- query read backwards -- but only if the whole key reverses, which is why the
-- id carries the same direction as the sort column rather than an independent
-- one. `(priority DESC, id DESC)` read backwards is `(priority ASC, id ASC)`;
-- `(priority DESC, id ASC)` read backwards is nothing the query asks for.
--
-- The same reasoning is what settles where the NULLs go, and it is the reason
-- the application never writes a NULLS clause. PostgreSQL's defaults are ASC
-- NULLS LAST and DESC NULLS FIRST, which are exact mirrors -- so one index
-- serves both directions. Spelling `DESC NULLS LAST` in the query would read
-- better and would need a second index to serve at all.

-- Sorted by when it last changed, which is what a "recently updated" list is.
-- `updated_at` is stamped by every write in IssueRepository, so this index
-- churns with edits rather than with inserts; that is the cost of the view,
-- and it is the cheapest of the three because the rows it moves are the rows
-- being written anyway.
CREATE INDEX issues_workspace_live_updated_at_id_idx
    ON issues (workspace_id, updated_at DESC, id DESC)
    WHERE archived_at IS NULL;

-- Sorted by urgency, over `NULLIF(priority, 0)` and not over `priority`.
--
-- An expression index, which needs a defence because it is the only one in
-- this schema. 0 in that column means "no priority", not "the lowest one" -- 1
-- is Urgent and 4 is Low -- so the raw column sorts the untriaged either above
-- Urgent or below Low, and neither is an order anybody asked for. The
-- application orders by the expression; an index over the bare column could
-- not serve it, and the planner matches an expression index only against the
-- identical expression, so the two are written to be textually the same.
--
-- The expression is NULL for every untriaged issue, so those sort last
-- ascending, which is the point.
CREATE INDEX issues_workspace_live_priority_id_idx
    ON issues (workspace_id, NULLIF(priority, 0) DESC, id DESC)
    WHERE archived_at IS NULL;

-- Sorted by what is due, which is the list a person plans a week from.
-- `due_date` is NULL for most issues and that is a real state rather than
-- missing data, so the undated sort last ascending -- soonest first, then the
-- ones with no date -- under the default this index is built to match.
CREATE INDEX issues_workspace_live_due_date_id_idx
    ON issues (workspace_id, due_date, id)
    WHERE archived_at IS NULL;


-- There is deliberately no index for `stateCategory` and none for a
-- workspace-wide `workflowStateId`.
--
-- A category is a property of `workflow_states`, not of `issues`, so the
-- filter is a semi-join against a table holding a handful of rows per team.
-- PostgreSQL hashes that table and probes it while walking whichever ordering
-- index the query named, stopping at the page size -- and the categories a
-- person filters by are not selective (most of a board is 'unstarted' or
-- 'started'), so the walk stops early. Materialising the category onto
-- `issues` to index it would be a copy of a column that changes when a state
-- is reclassified, and the copy is the one that would be wrong.
--
-- A workflow state alone, without a team, is the case 005's
-- issues_workflow_state_idx cannot serve, since `team_id` is its second
-- column. It stays unserved on purpose: a state belongs to exactly one team,
-- so any client that has a state id has the team it came from, and sending
-- both makes the existing index answer. An index to cover a client that
-- withholds a fact it holds is one this table pays for on every write.
