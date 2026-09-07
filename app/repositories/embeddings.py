from uuid import UUID

import asyncpg

from app.domain.issues import IssueEntity
from app.domain.semantic_search import DuplicateSuggestion, EmbeddingSource
from app.domain.tenancy import WorkspaceScope
from app.repositories.issues import ISSUE_COLUMNS, issue_from_row


class EmbeddingRepository:
    """SQL over `issue_embeddings`, and the hybrid reads that join it.

    Every statement here is anchored on `issue_embeddings`, which is the table
    this repository owns -- the two search statements return issues, but they
    return them the way `IssueRepository.get_by_identifier` returns an issue by
    joining `teams`: the join reaches a second table to answer a question about
    the first. The lexical arm of the fusion is the one exception, and it is a
    copy of `IssueRepository.search`'s predicate rather than a call to it,
    because reciprocal rank fusion needs both orderings inside ONE statement --
    fusing two Python lists would mean fetching a hundred entities per arm to
    keep twenty.

    THE TENANT PREDICATE IS THE POINT OF THIS FILE. A vector index -- and, for
    a workspace whose rows the planner reads sequentially, a vector scan --
    ranks by distance and knows nothing about tenancy. So `workspace_id = $1`
    is ANDed into every FROM in every statement below, including inside both
    arms of the fusion and inside the final join that fetches the rows: another
    tenant's issue is never read into this process, so there is no top-k for a
    post-filter to be forgotten about, no count for it to inflate, and no
    ordering for it to perturb. A filter applied after a `LIMIT` would be all
    three of those at once -- see migrations/025_semantic_search.sql, which
    declines an HNSW index for exactly this reason.

    Connections are passed in and never acquired. `asyncpg.Record` does not
    leave this class.
    """

    async def upsert(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        issue_id: UUID,
        model: str,
        source_digest: str,
        embedding: str,
        # `embedding` is a pgvector text literal from
        # app.services.embeddings.vector_literal, cast with `::vector` below.
    ) -> bool:
        """Store one issue's embedding, if the issue still reads that way.

        Returns whether a row was written. False means the write was declined,
        and there are three ways to earn that, all of them correct:

          * the issue is not in this workspace, or does not exist;
          * it has been archived since the sweep read it;
          * ITS TEXT CHANGED WHILE THE MODEL WAS RUNNING. This is the one worth
            spelling out. `source_digest` is the digest the caller embedded,
            and the WHERE clause requires the row to still carry it -- so a
            title edited between the read and this write leaves the old vector
            unwritten rather than stored under a digest that says it is
            current. The issue stays in the regeneration queue and is picked up
            with its new text. Without this predicate the race would produce
            exactly the failure migration 025's digest column exists to make
            impossible: a stale vector indistinguishable from a fresh one.

        `source_digest` is written from `issues.embedding_source_digest` rather
        than from the parameter, even though the WHERE clause has just proved
        them equal. The stored value is then the server's own, so a future
        widening of that predicate cannot let a caller's claim about the text
        become the record of what was embedded.
        """
        written = await connection.fetchval(
            """
            INSERT INTO issue_embeddings (
                workspace_id, issue_id, model, source_digest, embedding
            )
            SELECT
                issues.workspace_id,
                issues.id,
                $3,
                issues.embedding_source_digest,
                $5::vector
            FROM issues
            WHERE issues.workspace_id = $1
                AND issues.id = $2
                AND issues.archived_at IS NULL
                AND issues.embedding_source_digest = $4
            ON CONFLICT ON CONSTRAINT issue_embeddings_pkey DO UPDATE
            SET model = EXCLUDED.model,
                source_digest = EXCLUDED.source_digest,
                embedding = EXCLUDED.embedding,
                updated_at = now()
            RETURNING issue_id
            """,
            scope.workspace_id,
            issue_id,
            model,
            source_digest,
            embedding,
        )

        return written is not None

    async def list_stale(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        model: str,
        limit: int,
    ) -> list[EmbeddingSource]:
        """This workspace's live issues with no current embedding under `model`.

        THE REGENERATION QUEUE, and it is a query rather than a table. An issue
        needs embedding when nothing in `issue_embeddings` matches it on all
        four of (workspace, issue, model, digest) -- so the whole of "is this
        stale?" is the LEFT JOIN's ON clause, and "which are stale?" is
        `IS NULL` over it. Nothing marks a row dirty, so nothing can forget to:
        a title edited by a bulk import, a webhook or a hand-run UPDATE changes
        `issues.embedding_source_digest` in the same statement that changed the
        title, and the row appears here on the next sweep.

        `model` in the join condition is what makes changing embedders free.
        Every stored vector under the old name stops matching at once, the
        whole workspace queues itself, and no migration or backfill script is
        involved.

        Newest first, matching `issues_workspace_live_created_at_id_idx` from
        migration 006, so the walk is an index scan rather than a sort of the
        workspace. Newest is also the right product answer: a freshly filed
        issue is the one somebody is about to search for a duplicate of.

        The ceiling, stated: this is an anti-join, so once most issues are
        embedded the scan walks the index past every current row to find
        `limit` uncovered ones. Bounded work per sweep, unbounded per row
        skipped. It stops mattering only if the sweep becomes incremental --
        a `updated_at > $n` watermark -- and that is worth doing on the day a
        workspace's steady-state sweep shows up in a profile, not before.
        """
        rows = await connection.fetch(
            """
            SELECT
                issues.id,
                issues.embedding_source_digest,
                issues.title,
                issues.description
            FROM issues
            LEFT JOIN issue_embeddings
                ON issue_embeddings.workspace_id = issues.workspace_id
                AND issue_embeddings.issue_id = issues.id
                AND issue_embeddings.model = $2
                AND issue_embeddings.source_digest
                    = issues.embedding_source_digest
            WHERE issues.workspace_id = $1
                AND issues.archived_at IS NULL
                AND issue_embeddings.issue_id IS NULL
            ORDER BY issues.created_at DESC, issues.id DESC
            LIMIT $3
            """,
            scope.workspace_id,
            model,
            limit,
        )

        return [
            EmbeddingSource(
                issue_id=row["id"],
                source_digest=row["embedding_source_digest"],
                title=row["title"],
                description=row["description"],
            )
            for row in rows
        ]

    async def search_hybrid(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        query: str,
        embedding: str,
        model: str,
        depth: int,
        rrf_k: int,
        limit: int,
    ) -> list[IssueEntity]:
        """One ranked list from two retrievers, fused by reciprocal rank.

        Two CTEs and a FULL OUTER JOIN, in one statement. `lexical` is
        `IssueRepository.search`'s predicate and ordering exactly -- the same
        `websearch_to_tsquery('english', ...)` over the same generated column,
        so migration 011's GIN index serves it here as it does there.
        `semantic` orders this workspace's fresh embeddings by cosine distance.
        Each numbers its own results with `row_number()`, which is the only
        output of either arm the fusion reads.

        WHY BOTH ARMS CARRY THE TENANT EQUALITY SEPARATELY, rather than
        filtering the fused list: because each arm has its own LIMIT, and a
        LIMIT applied before a tenant filter is a cross-tenant defect even when
        the filter afterwards is correct. A workspace holding a hundred issues
        inside an installation holding a million would get its hundred
        candidates chosen from the million, discover that ninety-nine of them
        belong to somebody else, and answer with one result -- while the
        response time, the result count and the ordering all varied with what
        OTHER tenants had written. `workspace_id = $1` is inside both arms and
        again in the final join, so the candidate lists are drawn from this
        workspace to begin with.

        `source_digest = issues.embedding_source_digest` is in the semantic
        arm's WHERE clause, so a stale embedding contributes nothing: the issue
        is still findable lexically, at its lexical rank, and is simply absent
        from the vector ordering until the next refresh. That is the honest
        behaviour -- an issue ranked by a vector of text it no longer contains
        would be ranked by a lie.

        `archived_at IS NULL` in both arms, for the reason every read in this
        schema carries it.

        The fusion itself: `1 / (k + rank)` per arm, summed, with `coalesce` to
        zero for an arm that did not retrieve the document at all. See RRF_K in
        app/domain/semantic_search.py for why rank and not score.

        `ORDER BY score DESC, id DESC`. The tie-break is not decoration --
        reciprocal ranks are rationals over a small set of integers, so exact
        ties are routine (any two documents found by one arm only, at the same
        rank, from different arms) and without a unique tie-break the same
        query returns the same rows in a different order each time.

        The final join goes back to `issues` and re-states `workspace_id = $1`.
        Redundant given both arms, and kept: it makes the fetch an index seek
        on `issues_workspace_id_key` rather than a lookup by id alone, and it
        means the statement has no path to a row outside the scope even if a
        future edit loosens one of the CTEs.
        """
        rows = await connection.fetch(
            f"""
            WITH lexical AS (
                SELECT
                    issues.id AS issue_id,
                    row_number() OVER (
                        ORDER BY
                            ts_rank(
                                issues.search_vector,
                                websearch_to_tsquery('english', $2)
                            ) DESC,
                            issues.id DESC
                    ) AS rank
                FROM issues
                WHERE issues.workspace_id = $1
                    AND issues.archived_at IS NULL
                    AND issues.search_vector
                        @@ websearch_to_tsquery('english', $2)
                ORDER BY
                    ts_rank(
                        issues.search_vector,
                        websearch_to_tsquery('english', $2)
                    ) DESC,
                    issues.id DESC
                LIMIT $5
            ),
            semantic AS (
                SELECT
                    issue_embeddings.issue_id AS issue_id,
                    row_number() OVER (
                        ORDER BY
                            issue_embeddings.embedding <=> $3::vector,
                            issue_embeddings.issue_id DESC
                    ) AS rank
                FROM issue_embeddings
                JOIN issues
                    ON issues.workspace_id = issue_embeddings.workspace_id
                    AND issues.id = issue_embeddings.issue_id
                WHERE issue_embeddings.workspace_id = $1
                    AND issue_embeddings.model = $4
                    AND issue_embeddings.source_digest
                        = issues.embedding_source_digest
                    AND issues.archived_at IS NULL
                ORDER BY
                    issue_embeddings.embedding <=> $3::vector,
                    issue_embeddings.issue_id DESC
                LIMIT $5
            ),
            fused AS (
                SELECT
                    coalesce(lexical.issue_id, semantic.issue_id) AS issue_id,
                    coalesce(1.0 / ($6 + lexical.rank), 0.0)
                        + coalesce(1.0 / ($6 + semantic.rank), 0.0) AS score
                FROM lexical
                FULL OUTER JOIN semantic
                    ON semantic.issue_id = lexical.issue_id
            )
            SELECT
{ISSUE_COLUMNS}
            FROM fused
            JOIN issues
                ON issues.workspace_id = $1
                AND issues.id = fused.issue_id
            ORDER BY fused.score DESC, issues.id DESC
            LIMIT $7
            """,
            scope.workspace_id,
            query,
            embedding,
            model,
            depth,
            rrf_k,
            limit,
        )

        return [issue_from_row(row) for row in rows]

    async def search_similar(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        embedding: str,
        model: str,
        exclude_issue_id: UUID | None,
        min_similarity: float,
        limit: int,
    ) -> list[DuplicateSuggestion]:
        """This workspace's issues closest to one embedding, with the score.

        The duplicate-suggestion read. Vector only and no lexical arm,
        deliberately: a person about to file "the build keeps dying" is shown
        candidates they would not have found by searching, and the lexical
        matches are the ones they would have. Fusing here would fill the list
        with issues sharing a common word and push the paraphrase off it.

        `1 - (embedding <=> $2)` is the cosine similarity, computed and
        returned rather than left for the caller to reconstruct from a
        distance. It is a similarity and never a certainty; see
        `DuplicateSuggestion` for what that means for whatever renders it.

        `min_similarity` is applied in the WHERE clause and not after the
        LIMIT, so `limit` bounds the SUGGESTIONS rather than the candidates: a
        workspace with two plausible duplicates gets two, not two plus
        eighteen unrelated issues that happened to be the next-nearest.

        `IS DISTINCT FROM $4` rather than `<> $4`, so a NULL exclusion excludes
        nothing instead of making the predicate NULL and matching no row at
        all. The parameter is the issue being EDITED, which must not be
        suggested as its own duplicate; it is NULL when the caller is creating
        an issue that does not exist yet.

        Tenant-scoped in the WHERE clause and again in the join, and the
        staleness equality is here for the same reason it is in the hybrid
        read: suggesting a duplicate on the strength of text the issue no
        longer contains is worse than suggesting nothing.
        """
        rows = await connection.fetch(
            f"""
            SELECT
{ISSUE_COLUMNS},
                1 - (issue_embeddings.embedding <=> $2::vector) AS similarity
            FROM issue_embeddings
            JOIN issues
                ON issues.workspace_id = issue_embeddings.workspace_id
                AND issues.id = issue_embeddings.issue_id
            WHERE issue_embeddings.workspace_id = $1
                AND issue_embeddings.model = $3
                AND issue_embeddings.source_digest
                    = issues.embedding_source_digest
                AND issues.archived_at IS NULL
                AND issues.id IS DISTINCT FROM $4
                AND 1 - (issue_embeddings.embedding <=> $2::vector) >= $5
            ORDER BY
                issue_embeddings.embedding <=> $2::vector,
                issues.id DESC
            LIMIT $6
            """,
            scope.workspace_id,
            embedding,
            model,
            exclude_issue_id,
            min_similarity,
            limit,
        )

        return [
            DuplicateSuggestion(
                issue=issue_from_row(row),
                # float() rather than the value as bound: the expression is
                # `numeric - double precision`, which asyncpg hands back as a
                # Python float already -- but the entity boundary is where a
                # driver's choice of numeric type stops being this
                # application's problem, and Decimal reaching a Strawberry
                # `Float` field is a serialisation error rather than a
                # conversion.
                similarity=float(row["similarity"]),
            )
            for row in rows
        ]
