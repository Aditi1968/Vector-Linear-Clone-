import re
from datetime import datetime
from typing import NoReturn
from uuid import UUID

import asyncpg

from app.domain.errors import ValidationError, ValidationIssue
from app.domain.pagination import (
    InvalidCursorError,
    KeysetCursor,
    decode_keyset_cursor,
    encode_keyset_cursor,
)
from app.domain.patch import UNSET, UnsetType
from app.domain.releases import (
    DEFAULT_RELEASE_STATUS,
    DEPLOYED,
    ENVIRONMENT_KINDS,
    RANGE_LIMIT,
    RELEASE_STATUSES,
    RELEASE_TRANSITIONS_FROM,
    EnvironmentEntity,
    ReleaseEntity,
    ReleasePage,
    render_release_notes,
)
from app.domain.tenancy import WorkspaceScope
from app.repositories.releases import ReleaseRepository


NAME_MIN_LENGTH = 1
NAME_MAX_LENGTH = 200

# Matches `environments_name_length` in migrations/024_releases.sql, and the
# same pairing app/services/projects.py describes: the database has to refuse
# an oversized name whoever writes it, and this layer has to refuse one without
# turning a CheckViolationError into a user-facing message.
ENVIRONMENT_NAME_MAX_LENGTH = 100

FIRST_MIN = 1
FIRST_MAX = 100

# The same shape `github_commits_sha_format` and `releases_commit_sha_format`
# demand, checked here so that a malformed SHA is a field error naming the
# field rather than a CheckViolationError the transport has to mask.
_SHA = re.compile(r"^[0-9a-f]{40}$")

# One end of a release's range, once it is known to have an instant.
#
# An alias rather than a class, because it is one shape used inside one module
# and a `CommitBoundary` with a non-nullable field would be a second entity
# whose only difference from the first is a `| None`. `CommitBoundary` stays
# the repository's honest shape -- `github_commits.committed_at` is nullable --
# and this is what a caller holds once `_boundary` has refused the nullable
# case on its behalf.
DatedCommit = tuple[str, datetime]


# Constraint name -> the field error it means, for the violations that are
# ordinary consequences of client input rather than defects.
#
# Keyed on the constraint name and not on the exception class, because two
# constraints on one statement raise the same class and mean entirely different
# things: on `releases` a foreign key failure is either "no such environment"
# or "no such repository", and reporting the wrong one sends a client to
# correct the wrong half of its request.
#
# What is deliberately NOT distinguished is the tenant. An environment from
# another workspace and one that does not exist both break
# `releases_environment_fk`, so both produce the same NOT_FOUND. Telling them
# apart would require a lookup this service does not perform, and performing it
# would answer a question -- "does this id exist somewhere I cannot see" --
# that no client may be allowed to ask.
_CONSTRAINT_ERRORS: dict[str, ValidationIssue] = {
    "releases_environment_fk": ValidationIssue(
        field="environmentId",
        code="NOT_FOUND",
        message="Environment not found",
    ),
    "releases_repository_fk": ValidationIssue(
        field="repositoryId",
        code="NOT_FOUND",
        message="Repository not found",
    ),
    "environments_workspace_name_key": ValidationIssue(
        field="name",
        code="ALREADY_EXISTS",
        message="An environment with that name already exists",
    ),
}

_RELEASE_NOT_FOUND = ValidationIssue(
    field="id",
    code="NOT_FOUND",
    message="Release not found",
)

_COMMIT_NOT_FOUND = ValidationIssue(
    field="commitSha",
    code="NOT_FOUND",
    # One message for three situations that must stay indistinguishable: no
    # such commit anywhere, a commit in another workspace's repository, and a
    # commit in a repository this workspace has not connected. Naming which
    # would let anyone holding a workspace ask "have you deployed this commit?"
    # for any SHA they can guess.
    message="No such commit in this repository",
)

_COMMIT_UNDATED = ValidationIssue(
    field="commitSha",
    code="UNDATED",
    message="That commit has no timestamp, so it cannot bound a release range",
)

_PREVIOUS_COMMIT_NOT_FOUND = ValidationIssue(
    field="previousCommitSha",
    code="NOT_FOUND",
    message="No such commit in this repository",
)

_PREVIOUS_COMMIT_UNDATED = ValidationIssue(
    field="previousCommitSha",
    code="UNDATED",
    message="That commit has no timestamp, so it cannot bound a release range",
)

_PREVIOUS_COMMIT_OUT_OF_ORDER = ValidationIssue(
    field="previousCommitSha",
    code="OUT_OF_ORDER",
    message="The previous commit is newer than the one being released",
)


def _raise_mapped(error: asyncpg.PostgresError) -> NoReturn:
    """Translate a named constraint violation, or re-raise it untouched.

    The re-raise is the important half. A violation this service did not
    anticipate is a defect -- a missing NOT NULL, a constraint added by a later
    migration nobody taught this mapping about -- and turning it into a field
    error would tell a client to fix its input for a problem that is not in the
    input, while hiding the defect behind a 200.
    """
    issue = _CONSTRAINT_ERRORS.get(error.constraint_name or "")

    if issue is None:
        raise error

    raise ValidationError([issue]) from None


class ReleaseService:
    """Business rules for environments, releases and release notes.

    Validation lives here rather than in the GraphQL layer so that REST,
    workers and internal jobs all go through the same rules. The service also
    owns connection acquisition and transaction boundaries.

    The workspace is threaded through as an argument on every method rather
    than held on the instance, for the reason IssueService states: an instance
    attribute becomes an ambient current workspace that the next operation
    inherits without asking.

    Holding a scope is not permission to act in it. Nothing in this class
    checks that the caller belongs to the workspace it named.

    ## Where tenancy is actually enforced

    Mostly not in this file. `releases_environment_fk`, `releases_repository_fk`
    and `release_issues_issue_fk` in migrations/024_releases.sql are composite
    over `workspace_id`, so a cross-workspace association is refused by
    PostgreSQL and this service's only job on that path is to turn the refusal
    into a message.

    The exception is the commit SHA, which carries no foreign key -- the
    migration explains why at length. It is resolved through
    `ReleaseRepository.find_commit`, which is scoped to (workspace,
    repository), so a commit belonging to another tenant is *not found* and the
    caller gets exactly the answer a nonexistent SHA produces.

    ## Why `create` is one transaction and not three calls

    A release is a document about a moment: the range is resolved, the note is
    rendered from it, and both the note and the rows it was rendered from are
    written. Split across transactions, a failure in the middle would leave a
    release whose stored note describes issues that were never linked to it --
    which is the one inconsistency the whole design exists to prevent.
    """

    def __init__(self, pool: asyncpg.Pool, repository: ReleaseRepository):
        self._pool = pool
        self._repository = repository

    # ---------------------------------------------------------- environments

    async def list_environments(
        self,
        *,
        scope: WorkspaceScope,
    ) -> list[EnvironmentEntity]:
        """This workspace's deploy targets, by name.

        A single SELECT needs no explicit write transaction, so this acquires a
        connection without opening one.
        """
        async with self._pool.acquire() as connection:
            return await self._repository.list_environments(connection, scope=scope)

    async def create_environment(
        self,
        *,
        scope: WorkspaceScope,
        name: str,
        kind: str,
    ) -> EnvironmentEntity:
        """Declare one deploy target.

        `kind` is validated here as well as by `environments_kind_check`,
        because a CheckViolationError carries the rendered constraint -- which
        is either masked (telling the client nothing) or forwarded (telling it
        about the schema). Neither is a usable answer to "which kinds may I
        send".
        """
        self._validate_environment(name=name, kind=kind)

        async with self._pool.acquire() as connection:
            # The service owns the transaction boundary: later this block will
            # also carry the audit / sync / outbox writes.
            async with connection.transaction():
                try:
                    return await self._repository.create_environment(
                        connection,
                        scope=scope,
                        name=name,
                        kind=kind,
                    )
                except asyncpg.UniqueViolationError as error:
                    _raise_mapped(error)

    # ----------------------------------------------------------------- reads

    async def get_by_id(
        self,
        *,
        scope: WorkspaceScope,
        release_id: UUID,
    ) -> ReleaseEntity | None:
        """One release from this workspace, or nothing.

        "Not in this workspace" and "does not exist" are the same answer on
        purpose; the repository explains why the distinction must not be
        observable.
        """
        async with self._pool.acquire() as connection:
            return await self._repository.get_by_id(
                connection,
                scope=scope,
                release_id=release_id,
            )

    async def list(
        self,
        *,
        scope: WorkspaceScope,
        first: int,
        after: str | None,
    ) -> ReleasePage:
        """Forward keyset page of one workspace's releases, newest first.

        The cursor is not trusted to carry a workspace and could not be if it
        did: it is Base64 over JSON, readable and writable by anyone holding
        it. The scope comes from this call, so a cursor minted in one workspace
        and replayed against another selects nothing rather than resuming
        someone else's page.
        """
        cursor = self._validate_list(first=first, after=after)

        async with self._pool.acquire() as connection:
            # One extra row tells us whether a further page exists.
            rows = await self._repository.list(
                connection,
                scope=scope,
                limit=first + 1,
                after_created_at=cursor.created_at if cursor is not None else None,
                after_id=cursor.id if cursor is not None else None,
            )

        has_next_page = len(rows) > first
        nodes = rows[:first]

        end_cursor = None

        if nodes:
            # Built from the last RETURNED node, never from the extra row.
            last = nodes[-1]
            end_cursor = encode_keyset_cursor(last.created_at, last.id)

        return ReleasePage(
            nodes=nodes,
            has_next_page=has_next_page,
            end_cursor=end_cursor,
        )

    # -------------------------------------------------------------- releases

    async def create(
        self,
        *,
        scope: WorkspaceScope,
        name: str,
        environment_id: UUID,
        repository_id: int,
        commit_sha: str,
        previous_commit_sha: str | None | UnsetType = UNSET,
    ) -> ReleaseEntity:
        """Cut a release: resolve its range, render its notes, freeze both.

        `previous_commit_sha` has three meanings and they are three different
        requests, which is why it is UNSET-defaulted rather than
        None-defaulted:

        * UNSET -- "start from wherever the last deploy left off". The service
          looks up the most recent release of this repository into this
          environment that actually reached it, and uses its commit. This is
          what a deploy pipeline sends, because it is the value a pipeline
          would otherwise have to remember on the client side.
        * None -- "no lower bound", which is the first release of a repository
          into an environment and renders the whole history it can see.
        * a SHA -- "this exact range", which is what re-cutting a note for a
          known window needs.

        The order of the work matters. Both boundaries are resolved BEFORE
        anything is written, so a bad SHA costs no row; the insert runs before
        the links, because it carries the two foreign keys a client can break;
        and the whole thing is one transaction, so a release never exists
        carrying notes whose issues were not linked.

        The release starts `pending`. It is not deployed by being created --
        `releases_deployed_at_matches_status` would refuse a row claiming
        otherwise with no instant -- and moving it on is `set_status`.
        """
        self._validate_release_fields(
            name=name,
            commit_sha=commit_sha,
            previous_commit_sha=previous_commit_sha,
        )

        async with self._pool.acquire() as connection:
            async with connection.transaction():
                _, head_at = await self._boundary(
                    connection,
                    scope=scope,
                    repository_id=repository_id,
                    sha=commit_sha,
                    missing=_COMMIT_NOT_FOUND,
                    undated=_COMMIT_UNDATED,
                )

                previous = await self._previous_boundary(
                    connection,
                    scope=scope,
                    repository_id=repository_id,
                    environment_id=environment_id,
                    requested=previous_commit_sha,
                )

                # Unpacked rather than kept as an optional pair, so that the
                # three uses below read as two values instead of three
                # `is None` branches over one.
                previous_sha, previous_at = (
                    (None, None) if previous is None else previous
                )

                if previous_at is not None and previous_at > head_at:
                    raise ValidationError([_PREVIOUS_COMMIT_OUT_OF_ORDER])

                # One row past the limit, which is the whole reason the read is
                # bounded here rather than in the repository: a read of exactly
                # RANGE_LIMIT rows cannot tell a range that holds exactly that
                # many from one that holds more, and a changelog that silently
                # omits work is worse than one that says it did.
                resolved = await self._repository.resolve_range(
                    connection,
                    scope=scope,
                    repository_id=repository_id,
                    since=previous_at,
                    until=head_at,
                    limit=RANGE_LIMIT + 1,
                )

                truncated = (
                    len(resolved.issues) > RANGE_LIMIT
                    or len(resolved.pull_requests) > RANGE_LIMIT
                )

                # Trimmed BEFORE anything is written, so the frozen link rows
                # and the frozen note describe the same set. Two trims that
                # could disagree is exactly the inconsistency this whole design
                # exists to prevent.
                shipped_issues = resolved.issues[:RANGE_LIMIT]
                shipped_pulls = resolved.pull_requests[:RANGE_LIMIT]

                notes = render_release_notes(
                    name=name,
                    commit_sha=commit_sha,
                    previous_commit_sha=previous_sha,
                    issues=shipped_issues,
                    pull_requests=shipped_pulls,
                    truncated=truncated,
                )

                try:
                    created = await self._repository.create(
                        connection,
                        scope=scope,
                        name=name,
                        environment_id=environment_id,
                        repository_id=repository_id,
                        commit_sha=commit_sha,
                        previous_commit_sha=previous_sha,
                        status=DEFAULT_RELEASE_STATUS,
                        notes=notes,
                    )
                except asyncpg.ForeignKeyViolationError as error:
                    # Only releases_environment_fk and releases_repository_fk
                    # can fire here -- they are the two foreign keys on this
                    # INSERT whose values came from a client.
                    _raise_mapped(error)

                await self._repository.add_issues(
                    connection,
                    scope=scope,
                    release_id=created.id,
                    issue_ids=[ref.issue_id for ref in shipped_issues],
                )
                await self._repository.add_pull_requests(
                    connection,
                    scope=scope,
                    release_id=created.id,
                    pull_requests=shipped_pulls,
                )

                # Re-read for the aggregates. `create` returns empty arrays by
                # construction -- nothing could reference the id yet -- so the
                # entity it hands back would report a release that shipped
                # nothing.
                entity = await self._repository.get_by_id(
                    connection,
                    scope=scope,
                    release_id=created.id,
                )

        if entity is None:
            # Unreachable: the INSERT above committed inside this transaction,
            # so a release with this id exists in this workspace. Stated rather
            # than assumed, because the alternative is returning a
            # `ReleaseEntity | None` from a method whose whole contract is that
            # it worked.
            raise ValidationError([_RELEASE_NOT_FOUND])

        return entity

    async def set_status(
        self,
        *,
        scope: WorkspaceScope,
        release_id: UUID,
        status: str,
    ) -> ReleaseEntity:
        """Move a release along its lifecycle.

        The legal moves are `app.domain.releases.RELEASE_TRANSITIONS`, and they
        are applied as part of the UPDATE rather than checked before it -- see
        `ReleaseRepository.set_status`. That leaves two reasons the statement
        can match nothing, and this method tells them apart with a second read
        inside the same transaction: no such release, or a release in a state
        this move is not legal from.

        `deployed_at` is stamped by the same statement when the target is
        `deployed`, so a release cannot be reported as deployed without an
        instant even if this code were wrong -- the database's
        `releases_deployed_at_matches_status` refuses the pair.
        """
        if status not in RELEASE_STATUSES:
            raise ValidationError(
                [
                    ValidationIssue(
                        field="status",
                        code="INVALID",
                        message=(
                            f"Status must be one of: {', '.join(RELEASE_STATUSES)}"
                        ),
                    )
                ]
            )

        async with self._pool.acquire() as connection:
            async with connection.transaction():
                entity = await self._repository.set_status(
                    connection,
                    scope=scope,
                    release_id=release_id,
                    status=status,
                    allowed_from=RELEASE_TRANSITIONS_FROM[status],
                    stamp_deployed_at=status == DEPLOYED,
                )

                if entity is not None:
                    return entity

                current = await self._repository.get_by_id(
                    connection,
                    scope=scope,
                    release_id=release_id,
                )

        if current is None:
            raise ValidationError([_RELEASE_NOT_FOUND])

        raise ValidationError(
            [
                ValidationIssue(
                    field="status",
                    code="INVALID_TRANSITION",
                    # The CURRENT status is named because the client already
                    # holds it -- it read the release to render the button --
                    # and because "why did that fail" is otherwise
                    # unanswerable without a second round trip.
                    message=(
                        f"A release that is {current.status} cannot become {status}"
                    ),
                )
            ]
        )

    async def delete(self, *, scope: WorkspaceScope, release_id: UUID) -> None:
        """Delete one release and the record of what it shipped.

        The link rows go first, in the same transaction, because both foreign
        keys onto `releases` are RESTRICT -- the migration chose that so a
        one-line delete could not discard them while reporting `DELETE 1`.

        Unlike `InitiativeService.delete` there is nothing to promote or
        preserve here: `release_issues` and `release_pull_requests` are a
        record OF this release and mean nothing without it. The issues
        themselves are untouched.

        This exists as much for the RESTRICT chain above it as for the product:
        `releases_repository_fk` refuses to let a workspace disconnect its
        GitHub integration while releases name its repositories, and this is
        the way out of that.
        """
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                await self._repository.clear_links(
                    connection,
                    scope=scope,
                    release_id=release_id,
                )

                deleted = await self._repository.delete(
                    connection,
                    scope=scope,
                    release_id=release_id,
                )

                if not deleted:
                    # Raised inside the transaction so it rolls back. Nothing
                    # above it could have matched a row -- the release is not
                    # in this workspace, so neither is anything referencing it
                    # -- but relying on that to leave the database untouched
                    # would be relying on an argument rather than on the
                    # rollback that makes it true.
                    raise ValidationError([_RELEASE_NOT_FOUND])

    # ------------------------------------------------------------ boundaries

    async def _boundary(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        repository_id: int,
        sha: str,
        missing: ValidationIssue,
        undated: ValidationIssue,
    ) -> DatedCommit:
        """Resolve one end of the range, or refuse with a field error.

        Returns a `(sha, committed_at)` pair rather than the `CommitBoundary`
        the repository hands back, and the difference is the whole reason this
        method exists: the repository's shape carries a nullable instant
        because `github_commits.committed_at` is nullable, and every caller
        downstream needs one that is not. Refusing here and returning the
        narrower type means no later frame has to re-check it, and none of them
        can forget to.

        The two issues are parameters because both ends fail the same two ways
        and must report against different fields -- `commitSha` and
        `previousCommitSha`. One method with two messages beats two methods
        that have to keep the same rules.
        """
        boundary = await self._repository.find_commit(
            connection,
            scope=scope,
            repository_id=repository_id,
            sha=sha,
        )

        if boundary is None:
            raise ValidationError([missing])

        if boundary.committed_at is None:
            raise ValidationError([undated])

        return boundary.sha, boundary.committed_at

    async def _previous_boundary(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        repository_id: int,
        environment_id: UUID,
        requested: str | None | UnsetType,
    ) -> DatedCommit | None:
        """The lower end of the range, resolved from whichever of the three
        requests the caller made. See `create` for what the three mean.

        A SHA the caller NAMED is validated strictly: unknown or undated is a
        field error, because the caller asserted something about it that turned
        out to be false.

        A SHA DERIVED from the previous release is not, and the asymmetry is
        deliberate. That value was already validated when the earlier release
        was created, so an undated or missing row here means the commit was
        removed from `github_commits` afterwards -- something only a change to
        the GitHub integration can cause, which is not the current caller's
        input and not something they could fix. The range simply opens at the
        beginning instead, which over-reports rather than failing a deploy.

        ponytail: a derived boundary that no longer resolves widens the range
        instead of refusing. The upgrade is to keep the previous release's own
        `deployed_at` as a fallback lower bound; that needs the release row
        rather than only its SHA, and is worth doing the first time anyone sees
        it happen.
        """
        if requested is None:
            return None

        if isinstance(requested, UnsetType):
            derived = await self._repository.previous_deployed_commit_sha(
                connection,
                scope=scope,
                repository_id=repository_id,
                environment_id=environment_id,
            )

            if derived is None:
                return None

            boundary = await self._repository.find_commit(
                connection,
                scope=scope,
                repository_id=repository_id,
                sha=derived,
            )

            if boundary is None or boundary.committed_at is None:
                return None

            return boundary.sha, boundary.committed_at

        return await self._boundary(
            connection,
            scope=scope,
            repository_id=repository_id,
            sha=requested,
            missing=_PREVIOUS_COMMIT_NOT_FOUND,
            undated=_PREVIOUS_COMMIT_UNDATED,
        )

    # ------------------------------------------------------------ validation

    @staticmethod
    def _validate_list(*, first: int, after: str | None) -> KeysetCursor | None:
        """Validate pagination arguments, returning the decoded cursor.

        `first` is never silently clamped, and an invalid cursor is an expected
        input error rather than a parser exception. The rules and the codes are
        IssueService's and InitiativeService's, deliberately: list endpoints
        that disagreed about the legal page size would be a contract a client
        has to learn once per feature.
        """
        issues: list[ValidationIssue] = []
        cursor: KeysetCursor | None = None

        if first < FIRST_MIN or first > FIRST_MAX:
            issues.append(
                ValidationIssue(
                    field="first",
                    code="OUT_OF_RANGE",
                    message=f"first must be between {FIRST_MIN} and {FIRST_MAX}",
                )
            )

        if after is not None:
            try:
                cursor = decode_keyset_cursor(after)
            except InvalidCursorError:
                issues.append(
                    ValidationIssue(
                        field="after",
                        code="INVALID_CURSOR",
                        message="Cursor is invalid",
                    )
                )

        if issues:
            raise ValidationError(issues)

        return cursor

    @staticmethod
    def _validate_environment(*, name: str, kind: str) -> None:
        """Collect every violation, then raise once.

        Field order is deterministic (name, kind) so that clients can rely on
        it. The codes and messages are a public contract.
        """
        issues: list[ValidationIssue] = []

        # Validated as supplied -- never trimmed or rewritten. A name of three
        # spaces is a name the caller typed, and silently turning it into a
        # REQUIRED failure would report an error about input the client never
        # sent.
        if len(name) < NAME_MIN_LENGTH:
            issues.append(
                ValidationIssue(
                    field="name",
                    code="REQUIRED",
                    message="Name is required",
                )
            )
        elif len(name) > ENVIRONMENT_NAME_MAX_LENGTH:
            issues.append(
                ValidationIssue(
                    field="name",
                    code="TOO_LONG",
                    message=(
                        f"Name must be at most {ENVIRONMENT_NAME_MAX_LENGTH} characters"
                    ),
                )
            )

        if kind not in ENVIRONMENT_KINDS:
            issues.append(
                ValidationIssue(
                    field="kind",
                    code="INVALID",
                    message=f"Kind must be one of: {', '.join(ENVIRONMENT_KINDS)}",
                )
            )

        if issues:
            raise ValidationError(issues)

    @staticmethod
    def _validate_release_fields(
        *,
        name: str,
        commit_sha: str,
        previous_commit_sha: str | None | UnsetType,
    ) -> None:
        """Everything about a release that can be judged without a database.

        The SHA format in particular: `releases_commit_sha_format` would refuse
        an abbreviation too, but as a CheckViolationError carrying the rendered
        constraint, which is either masked or leaks the schema. Neither is a
        usable answer to "what does this field want".

        The equality check restates `releases_previous_commit_differs`, and is
        the one rule here that is about the pair rather than either half: a
        range whose ends are the same commit contains nothing.
        """
        issues: list[ValidationIssue] = []

        if len(name) < NAME_MIN_LENGTH:
            issues.append(
                ValidationIssue(
                    field="name",
                    code="REQUIRED",
                    message="Name is required",
                )
            )
        elif len(name) > NAME_MAX_LENGTH:
            issues.append(
                ValidationIssue(
                    field="name",
                    code="TOO_LONG",
                    message=f"Name must be at most {NAME_MAX_LENGTH} characters",
                )
            )

        if _SHA.match(commit_sha) is None:
            issues.append(
                ValidationIssue(
                    field="commitSha",
                    code="INVALID",
                    message="Commit SHA must be 40 lowercase hexadecimal characters",
                )
            )

        if isinstance(previous_commit_sha, str):
            if _SHA.match(previous_commit_sha) is None:
                issues.append(
                    ValidationIssue(
                        field="previousCommitSha",
                        code="INVALID",
                        message=(
                            "Commit SHA must be 40 lowercase hexadecimal characters"
                        ),
                    )
                )
            elif previous_commit_sha == commit_sha:
                issues.append(
                    ValidationIssue(
                        field="previousCommitSha",
                        code="INVALID",
                        message="A release range cannot start and end at one commit",
                    )
                )

        if issues:
            raise ValidationError(issues)
