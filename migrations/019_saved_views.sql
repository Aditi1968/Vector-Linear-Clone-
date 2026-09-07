-- Saved views: a named, reusable issue query, and the per-user shortcuts that
-- point at one.
--
-- The shape this file exists to make impossible is a saved view that is its
-- own filter language. A view stores the SAME filter `issues(filter:)` already
-- takes -- app/domain/issues.py's IssueFilter, and nothing else -- so "save
-- this list" and "show me that list again" are one predicate builder rather
-- than two that have to keep agreeing. A second, stored-only filter dialect
-- would be a second place for a tenancy predicate to be forgotten, and the
-- forgotten one is always the one nobody re-read.
--
-- That decision is what makes the `filter` column JSONB rather than a wide row
-- of nullable columns. The filter has eight optional fields today and three of
-- them are TRI-state -- "assigned to Ana", "assigned to nobody", "not
-- filtering on assignee" -- which a nullable column cannot spell without a
-- second boolean beside it that can contradict it. Eight columns and three
-- booleans would also mean a migration every time the issue filter grows a
-- field, in a table whose contents no query here ever looks inside.
--
-- The cost of JSONB is stated plainly, because it is the one real risk in this
-- file: a stored document is *attacker-authored*. Any user who can create a
-- view chooses those bytes, and a later reader that trusted them would be
-- building SQL predicates out of client input that has been sitting in a
-- table long enough to look like server state. So nothing downstream reads
-- this column as JSON. `app.domain.saved_views.decode_filter` parses it back
-- into an `IssueFilter` -- known keys only, one parser per field, an unknown
-- key or a mistyped value refused outright -- and every predicate is then
-- built from typed values ANDed onto the caller's own workspace, exactly as a
-- filter arriving live on the wire is. The CHECK below is the schema's half of
-- that: the column is an object, so the decoder never has to wonder whether it
-- was handed an array, a number or a bare `null`.
--
-- What is deliberately NOT in JSONB is everything a query might one day want
-- to read: the ordering, the layout, the grouping and the visibility are
-- columns with CHECK constraints, because those are vocabularies the server
-- decides and the client selects from. A layout of 'kanbanish' is a typo in a
-- writer, and a typo that reaches this table is a view that renders as
-- nothing.
--
-- `favorites` is the second table, and it is per-user AND per-workspace: the
-- same person in two workspaces has two independent lists, and the pair
-- (workspace_id, user_id) is a foreign key onto `workspace_members` rather
-- than onto `users`, so a favourite cannot outlive the membership that made it
-- meaningful. Three nullable target columns rather than one (kind, target_id)
-- pair, and that is the whole tenancy argument again: a polymorphic pair can
-- carry no foreign key at all, so "favourite another workspace's project"
-- would be a row PostgreSQL happily stores and a resolver has to remember to
-- refuse. Three typed columns each get a composite key through this row's own
-- workspace_id, and the pairing is then not a row that exists.
--
-- Apply this ONLY through `python -m scripts.apply_migration
-- migrations/019_saved_views.sql`. The runner wraps the file in one
-- transaction, takes an advisory lock and records a checksum; a hand-run gets
-- none of that and executes statement-at-a-time in autocommit, which for this
-- file means a failure part way through leaves `favorites` referencing a
-- `saved_views` that has no unique key for it to point at.
--
-- DEPENDS ON 002 (workspaces, teams), 004 (workspace_members) and 009
-- (projects). Every one of those is referenced by a constraint below, so
-- applying this file before any of them fails on that constraint and -- inside
-- the single transaction the runner wraps the file in -- leaves nothing
-- behind. That failure IS the dependency check: the ledger records what has
-- been applied and not what depends on what, so the schema itself is what has
-- to refuse an out-of-order apply.


CREATE TABLE saved_views (
    id UUID PRIMARY KEY DEFAULT uuidv7(),

    workspace_id UUID NOT NULL,

    -- The team this view is about, or NULL for a view over the whole
    -- workspace.
    --
    -- Nullable and staying nullable: "all my issues across every team" is an
    -- ordinary view and not a missing team. This is a LABEL on the view rather
    -- than a filter applied to it -- the team a saved view narrows to lives in
    -- the `filter` document with the other seven predicates, so that one
    -- filter builder serves the saved and the live case alike. What this
    -- column buys is the sidebar: "which views belong on this team's page" is
    -- an equality on an indexed column rather than a containment operator over
    -- every stored document in the workspace.
    team_id UUID,

    name TEXT NOT NULL,

    -- The IssueFilter, as the object `app.domain.saved_views.encode_filter`
    -- produces: one key per field the author actually filtered on, absent
    -- for the fields they did not, and JSON null for the three whose column
    -- is nullable and whose author asked for the rows holding nothing.
    --
    -- No DEFAULT. A view that filters on nothing stores `{}`, which the
    -- decoder reads as the wide filter; a default would let an INSERT that
    -- forgot the column silently mean the same thing, and "shows every issue
    -- in the workspace" is not a value to arrive at by omission.
    filter JSONB NOT NULL,

    -- The ordering, as the two halves `app.domain.issues.IssueOrder` carries.
    --
    -- Two TEXT columns rather than one 'created_at:desc' string, because the
    -- CHECKs below can then say which fields and which directions exist,
    -- separately -- and a widening of one is a constraint swap that does not
    -- have to reason about the other. The vocabularies are IssueOrderField and
    -- OrderDirection, and the application's copies of them are pinned to these
    -- CHECKs by tests/test_saved_views.py rather than left to agree by habit.
    order_field TEXT NOT NULL,
    order_direction TEXT NOT NULL,

    layout TEXT NOT NULL,

    -- How rows are gathered on screen, or NULL for a flat list.
    --
    -- The server stores and constrains this vocabulary; it does not perform
    -- the grouping. Grouping is a rendering of a page the client already
    -- holds, and a server that grouped would have to page each group
    -- separately -- a different feature with a different cursor. What this
    -- column is for is that the choice survives a reload, which is the whole
    -- point of saving a view.
    --
    -- No 'label' among the values, deliberately. An issue wears many labels,
    -- so grouping by one puts the same issue in several groups, and every
    -- count drawn from those groups sums to more than the list holds. If it is
    -- ever wanted it needs that decision made out loud, not smuggled in as a
    -- seventh string.
    grouping TEXT,
    subgrouping TEXT,

    visibility TEXT NOT NULL,

    -- Who made this view. NOT NULL, because a personal view with no owner is
    -- one nobody can ever see again -- `visibility = 'personal'` is read as
    -- `created_by = <viewer>`, and a NULL there matches no viewer at all.
    created_by UUID NOT NULL,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- RESTRICT on both sides, matching teams_workspace_fk. A workspace with
    -- live views is not something to delete by accident, and relocating a
    -- workspace's id is not something to do silently to its views.
    CONSTRAINT saved_views_workspace_fk
        FOREIGN KEY (workspace_id)
        REFERENCES workspaces (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,

    -- The team must be in THIS view's workspace.
    --
    -- Composite through the row's own workspace_id, onto the pair
    -- teams_workspace_id_key marks [FK TARGET] in 002. `REFERENCES teams (id)`
    -- is the obvious spelling and it is the bug: it checks that the team is a
    -- real team somewhere and says nothing about where, so any team id from
    -- any tenant would label any workspace's view -- and the sidebar of
    -- workspace A would answer for a team in workspace B.
    --
    -- MATCH SIMPLE (the default) skips the check entirely for a row with any
    -- NULL among its referencing columns. That is the wanted behaviour and it
    -- is only safe because `workspace_id` is NOT NULL: the sole column that
    -- can be NULL is team_id, so the exemption is precisely "this view is not
    -- about one team" and cannot be widened by a NULL arriving in the other
    -- half.
    CONSTRAINT saved_views_team_fk
        FOREIGN KEY (workspace_id, team_id)
        REFERENCES teams (workspace_id, id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,

    -- The author must be a member of THIS view's workspace.
    --
    -- The same argument projects_lead_fk makes in 009, and it matters more
    -- here: `created_by` is not decoration, it is half of the read predicate
    -- for a personal view. A creator id from another tenant would be a view
    -- whose owner is nobody in this workspace -- invisible to every member and
    -- deletable by none of them.
    --
    -- ON DELETE RESTRICT, so removing someone from a workspace while they
    -- still own views is refused rather than silently orphaning them. The
    -- caller decides whether those views are deleted or handed on; a DELETE
    -- against one table must not quietly rewrite rows in another and report
    -- `DELETE 1`.
    CONSTRAINT saved_views_creator_fk
        FOREIGN KEY (workspace_id, created_by)
        REFERENCES workspace_members (workspace_id, user_id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,

    -- Bounded here as well as in the service, because an unbounded TEXT column
    -- inside an index is a way to make that index enormous by sending a large
    -- string. 200 matches the name limit projects and milestones already use.
    CONSTRAINT saved_views_name_length
        CHECK (char_length(name) BETWEEN 1 AND 200),

    -- The filter is an OBJECT, so the decoder never has to consider an array,
    -- a number, a string or a bare JSON null. Those are not filters anyone can
    -- author through the API; they are what a hand-written UPDATE or a future
    -- second writer would produce, and this is where that is caught -- before
    -- the row exists, rather than on the read that would have to fail loudly
    -- in front of a user.
    CONSTRAINT saved_views_filter_is_object
        CHECK (jsonb_typeof(filter) = 'object'),

    -- TEXT with a CHECK rather than an enum type, on all five vocabularies
    -- below, for the reason 009 gives at length: the runner wraps each file in
    -- one transaction and PostgreSQL refuses to *use* a label added by `ALTER
    -- TYPE ... ADD VALUE` in the transaction that added it, so widening an
    -- enum cannot be an ordinary migration. A CHECK widens with DROP
    -- CONSTRAINT + ADD CONSTRAINT in one file.
    CONSTRAINT saved_views_order_field_check
        CHECK (order_field IN ('priority', 'created_at', 'updated_at', 'due_date')),

    CONSTRAINT saved_views_order_direction_check
        CHECK (order_direction IN ('asc', 'desc')),

    CONSTRAINT saved_views_layout_check
        CHECK (layout IN ('list', 'board')),

    CONSTRAINT saved_views_grouping_check
        CHECK (
            grouping IS NULL
            OR grouping IN (
                'workflow_state', 'assignee', 'priority', 'project', 'cycle', 'team'
            )
        ),

    CONSTRAINT saved_views_subgrouping_check
        CHECK (
            subgrouping IS NULL
            OR subgrouping IN (
                'workflow_state', 'assignee', 'priority', 'project', 'cycle', 'team'
            )
        ),

    -- A second level of grouping needs a first one, and must differ from it.
    --
    -- Without this a row can say "no grouping, subgrouped by assignee", which
    -- is not a rendering any client can produce -- so it would be stored,
    -- returned, and silently dropped on the way to the screen. The `<>` half
    -- refuses "grouped by assignee, subgrouped by assignee", whose second
    -- level is one group per first-level group.
    CONSTRAINT saved_views_subgrouping_requires_grouping
        CHECK (
            subgrouping IS NULL
            OR (grouping IS NOT NULL AND subgrouping <> grouping)
        ),

    -- Two visibilities and no third. 'personal' means the creator alone;
    -- 'shared' means every member of the workspace. There is deliberately no
    -- 'team' between them: a per-team visibility would need a membership model
    -- for teams, which this schema does not have, and inventing one as a
    -- string here would be a permission nothing checks.
    CONSTRAINT saved_views_visibility_check
        CHECK (visibility IN ('personal', 'shared')),

    -- [FK TARGET] Redundant beside the primary key on id only if you do not
    -- look at what points here. A foreign key may reference a UNIQUE column
    -- SET and nothing else, and `(workspace_id, id)` is the pair
    -- favorites_saved_view_fk references. Without it that composite key cannot
    -- be declared at all, and the schema falls back to a single-column key
    -- that permits exactly the cross-workspace favourite this file refuses.
    CONSTRAINT saved_views_workspace_id_key UNIQUE (workspace_id, id)
);


-- One person's shortcuts, in one workspace, in the order they arranged them.
CREATE TABLE favorites (
    id UUID PRIMARY KEY DEFAULT uuidv7(),

    -- One workspace_id for the whole row, and that is the point: all four
    -- foreign keys below read this same column, so the member and the thing
    -- they favourited are checked against the SAME tenant rather than against
    -- two tenants that happen to be spelled separately.
    workspace_id UUID NOT NULL,
    user_id UUID NOT NULL,

    -- Exactly one of these is set; see favorites_one_target below.
    --
    -- Three typed columns and not a (kind TEXT, target_id UUID) pair. The pair
    -- is the tidier-looking shape and it cannot carry a foreign key -- there
    -- is no table for target_id to reference -- so every cross-tenant refusal
    -- in this table would become a lookup a resolver has to remember. Three
    -- columns cost three constraints and three indexes, and buy a schema where
    -- "favourite another workspace's project" is not a row.
    --
    -- Initiatives are deliberately absent. They do not exist yet, and a fourth
    -- column referencing a table no migration has created is a constraint that
    -- cannot be declared; when they land, the migration that creates them adds
    -- the column, its foreign key, its unique key, its index, and widens
    -- favorites_one_target.
    team_id UUID,
    project_id UUID,
    saved_view_id UUID,

    -- Where this sits in the person's own list, chosen by them.
    --
    -- Deliberately NOT unique per user. Uniqueness sounds tidier and turns
    -- every reorder into a multi-statement shuffle that a non-deferrable
    -- constraint rejects halfway through -- moving a favourite up means
    -- vacating the position it is moving into first. Ties are broken by `id`
    -- wherever favourites are ordered, which makes the order total without
    -- making it fragile. This is the same argument project_milestones.position
    -- makes in 009.
    position INTEGER NOT NULL,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Per-user AND per-workspace, in one constraint.
    --
    -- Onto workspace_members rather than users, and that is the difference
    -- between "this row belongs to a person" and "this row belongs to a person
    -- who is in this workspace". A `REFERENCES users (id)` would accept a
    -- favourite owned by someone with no membership here at all -- an
    -- ex-member's sidebar, still holding pointers into a workspace they were
    -- removed from.
    --
    -- ON DELETE RESTRICT for the reason every other key in this schema is:
    -- removing a member must not silently delete rows in a table the DELETE
    -- did not name. MembershipService is where that ordering gets written when
    -- member removal grows a cleanup path.
    CONSTRAINT favorites_member_fk
        FOREIGN KEY (workspace_id, user_id)
        REFERENCES workspace_members (workspace_id, user_id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,

    -- The three targets, each checked against this row's own workspace. A pair
    -- of single-column foreign keys would accept "workspace A's favourite,
    -- workspace B's project", and nothing in either constraint would notice.
    --
    -- MATCH SIMPLE skips each of these for a row whose target column is NULL,
    -- which is precisely the two targets this row is not, and is only safe
    -- because workspace_id is NOT NULL.
    CONSTRAINT favorites_team_fk
        FOREIGN KEY (workspace_id, team_id)
        REFERENCES teams (workspace_id, id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,

    CONSTRAINT favorites_project_fk
        FOREIGN KEY (workspace_id, project_id)
        REFERENCES projects (workspace_id, id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,

    -- Tenancy only. That a personal view belongs to somebody ELSE is a rule
    -- this key cannot state -- it would have to read `saved_views.visibility`
    -- and compare it with this row's user_id, which is a JOIN and not a
    -- constraint. SavedViewRepository.add_favorite writes the row through a
    -- `WHERE EXISTS (... visibility = 'shared' OR created_by = $user)` guard
    -- in the same statement, so there is no window between the check and the
    -- insert; this key is what stops the workspace being wrong even then.
    CONSTRAINT favorites_saved_view_fk
        FOREIGN KEY (workspace_id, saved_view_id)
        REFERENCES saved_views (workspace_id, id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,

    CONSTRAINT favorites_position_check
        CHECK (position >= 0),

    -- A favourite points at ONE thing. Not zero -- a row favouriting nothing
    -- is a position in a list with nothing at it -- and not two, which would
    -- be one row that has to render twice and be un-favourited twice.
    CONSTRAINT favorites_one_target
        CHECK (num_nonnulls(team_id, project_id, saved_view_id) = 1),

    -- One row per person per thing, said three times.
    --
    -- NULLs are DISTINCT in a unique constraint by default, which is exactly
    -- what makes three constraints work where one cannot: every row
    -- favouriting a project holds team_id NULL, and those NULLs do not collide
    -- with each other, so favorites_team_key constrains only the rows that
    -- actually name a team. A single UNIQUE over all three columns would
    -- instead compare (NULL, project, NULL) tuples and, being NULLS DISTINCT,
    -- constrain nothing at all.
    CONSTRAINT favorites_team_key UNIQUE (workspace_id, user_id, team_id),
    CONSTRAINT favorites_project_key UNIQUE (workspace_id, user_id, project_id),
    CONSTRAINT favorites_saved_view_key UNIQUE (workspace_id, user_id, saved_view_id)
);


-- The list, and the ordering the sidebar reads.
--
-- `workspace_id` leads because every product query does; `name` is the sort
-- key and `id` the tie-break that makes it total, which is what keeps the
-- keyset walk in SavedViewRepository.list from skipping or repeating rows when
-- two views share a name. Both are in the index, so the ordering is read
-- rather than sorted.
CREATE INDEX saved_views_workspace_name_id_idx
    ON saved_views (workspace_id, name, id);

-- The referencing side of saved_views_team_fk, and the answer to "which views
-- belong on this team's page".
--
-- PostgreSQL indexes the REFERENCED side of a foreign key and never the
-- referencing side, so without this every deletion from `teams` scans
-- `saved_views` in full to satisfy the RESTRICT above.
--
-- Not partial on `team_id IS NOT NULL`, though many views will have none, for
-- the reason 009 gives on issues_workspace_project_milestone_idx: the
-- predicate a referential-integrity check issues is generated by PostgreSQL
-- rather than written here, so whether it matches a partial index is a
-- property of the planner's implication prover rather than of this schema.
CREATE INDEX saved_views_workspace_team_idx
    ON saved_views (workspace_id, team_id);

-- The referencing side of saved_views_creator_fk, so member removal does not
-- scan this table, and the lookup behind "the personal views that are mine".
CREATE INDEX saved_views_workspace_creator_idx
    ON saved_views (workspace_id, created_by);

-- One person's favourites in their own order. (workspace_id, user_id) is also
-- the prefix favorites_member_fk's RESTRICT check looks up, so this index
-- serves the read and the constraint alike.
CREATE INDEX favorites_workspace_user_position_idx
    ON favorites (workspace_id, user_id, position, id);

-- The referencing side of the three target foreign keys. None of the UNIQUE
-- constraints above supplies one: they lead (workspace_id, user_id, ...), so
-- (workspace_id, team_id) is not a prefix of any of them, and without these
-- every deletion of a team, a project or a saved view scans `favorites` whole.
CREATE INDEX favorites_workspace_team_idx ON favorites (workspace_id, team_id);
CREATE INDEX favorites_workspace_project_idx ON favorites (workspace_id, project_id);
CREATE INDEX favorites_workspace_saved_view_idx
    ON favorites (workspace_id, saved_view_id);
