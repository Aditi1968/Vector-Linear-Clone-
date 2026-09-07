from datetime import datetime
from enum import Enum
from uuid import UUID

import strawberry

from app.domain.releases import (
    ENVIRONMENT_KINDS,
    RELEASE_STATUSES,
    EnvironmentEntity,
    ReleaseEntity,
    ReleasePage,
)
from app.graphql.types.errors import ValidationErrorType
from app.graphql.types.pagination import PageInfo


@strawberry.enum(name="EnvironmentKind")
class EnvironmentKindType(Enum):
    """What a deploy target IS, as an enum rather than a String.

    The same argument `InitiativeStatusType` makes: a closed vocabulary that a
    client picks from belongs in the schema, so an unknown value is a document
    the server rejects before a resolver runs rather than a field error after
    one has.

    The members' *values* are the strings the database stores, and the members'
    *names* are what appears in SDL.
    """

    DEVELOPMENT = "development"
    STAGING = "staging"
    PRODUCTION = "production"
    CUSTOM = "custom"


@strawberry.enum(name="ReleaseStatus")
class ReleaseStatusType(Enum):
    """Where a release is in its lifecycle.

    Deliberately a separate enum from `EnvironmentKindType` and from every
    other status in this schema: they share no member and would only move
    together by accident.

    ROLLED_BACK rather than a `rolledBack: Boolean` beside DEPLOYED, because
    the two are one fact -- a rolled-back release is not deployed -- and a
    boolean beside a status is a pair a client can read as both.
    """

    PENDING = "pending"
    DEPLOYED = "deployed"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"


# Checked at import time rather than left to a test, for the reason
# `InitiativeStatusType` states: a disagreement here is a payload that cannot
# be built, raised from whichever resolver reads the drifted row first, in
# production, as a masked internal error. Failing at import turns that into a
# process that will not start. An `if`/`raise` and not an `assert`, because
# `python -O` discards asserts.
if tuple(member.value for member in EnvironmentKindType) != ENVIRONMENT_KINDS:
    raise RuntimeError(
        "EnvironmentKindType and app.domain.releases.ENVIRONMENT_KINDS disagree; "
        "they are both statements of the environments_kind_check constraint in "
        "migrations/024_releases.sql and have to be changed together"
    )

if tuple(member.value for member in ReleaseStatusType) != RELEASE_STATUSES:
    raise RuntimeError(
        "ReleaseStatusType and app.domain.releases.RELEASE_STATUSES disagree; "
        "they are both statements of the releases_status_check constraint in "
        "migrations/024_releases.sql and have to be changed together"
    )


@strawberry.type(name="Environment")
class EnvironmentType:
    """One deploy target, as a client reads it.

    `kind` is published beside `name` rather than derived from it, for the
    reason migrations/024_releases.sql gives: a workspace may run "Prod EU" and
    "Prod US", and a client rendering a production warning has to recognise
    both.
    """

    id: UUID
    name: str
    kind: EnvironmentKindType
    created_at: datetime

    @classmethod
    def from_entity(cls, entity: EnvironmentEntity) -> "EnvironmentType":
        return cls(
            id=entity.id,
            name=entity.name,
            # By value: the entity carries what the column holds, and the enum
            # is keyed on exactly those strings. A row holding a kind the enum
            # does not know raises ValueError here rather than being rendered
            # as something plausible.
            kind=EnvironmentKindType(entity.kind),
            created_at=entity.created_at,
        )


@strawberry.type(name="Release")
class ReleaseType:
    id: UUID
    name: str

    environment_id: UUID

    # GitHub's numeric id for the repository this release shipped. An id and
    # not a `Repository`, following `Initiative.projectIds`: a field named
    # `repositoryId` says what it is instead of pretending a resolver is
    # coming, and the repository's `full_name` already reaches clients through
    # `githubIntegration`.
    repository_id: strawberry.ID

    commit_sha: str
    previous_commit_sha: str | None

    status: ReleaseStatusType

    # The frozen release note, exactly as it was rendered when the release was
    # cut.
    #
    # A stored value and not a resolver, which is the whole design: a note
    # computed on read would answer differently once a webhook edited a title
    # or retracted a link, so the document that went out and the document the
    # API returns would drift. See the header of migrations/024_releases.sql.
    notes: str

    # Null until it reaches the environment. Nullable in the SDL rather than
    # defaulted to `createdAt`, because "cut" and "deployed" are different
    # facts and a client that could not tell them apart would report a release
    # sitting in a queue as live.
    deployed_at: datetime | None

    created_at: datetime
    updated_at: datetime

    # What this release shipped, as ids and numbers.
    #
    # Ids and not `[Issue!]!`, following the argument `Initiative.projectIds`
    # makes at length: a field returning ids says so, and when a resolver
    # arrives it is added beside this rather than changing this field's type.
    # It also keeps the fan-out honest -- `releases(first: 100) { issues { ...
    # } }` would be a hundred issue reads that app/graphql/limits.py prices as
    # one.
    issue_ids: list[UUID]

    # Pull-request numbers within `repositoryId`, which is what "#84" means.
    # Enough to build a GitHub link, and the titles are already quoted in
    # `notes`.
    pull_request_numbers: list[int]

    @classmethod
    def from_entity(cls, entity: ReleaseEntity) -> "ReleaseType":
        return cls(
            id=entity.id,
            name=entity.name,
            environment_id=entity.environment_id,
            # GitHub's ids are BIGINT and routinely exceed what a GraphQL Int
            # (a signed 32-bit value) may carry, so this crosses the wire as an
            # ID -- the same choice app/graphql/types/github.py makes for the
            # same column. A client that parses it as a number is doing
            # something this schema does not promise.
            repository_id=strawberry.ID(str(entity.repository_id)),
            commit_sha=entity.commit_sha,
            previous_commit_sha=entity.previous_commit_sha,
            status=ReleaseStatusType(entity.status),
            notes=entity.notes,
            deployed_at=entity.deployed_at,
            created_at=entity.created_at,
            updated_at=entity.updated_at,
            issue_ids=list(entity.issue_ids),
            pull_request_numbers=list(entity.pull_request_numbers),
        )


@strawberry.type
class ReleaseConnection:
    nodes: list[ReleaseType]
    page_info: PageInfo

    @classmethod
    def from_domain(cls, page: ReleasePage) -> "ReleaseConnection":
        return cls(
            nodes=[ReleaseType.from_entity(entity) for entity in page.nodes],
            page_info=PageInfo(
                has_next_page=page.has_next_page,
                end_cursor=page.end_cursor,
            ),
        )


# One payload type per *shape*, reused across the mutations that share it,
# rather than one per mutation -- the convention app/graphql/types/project.py
# argues for at length.
@strawberry.type
class ReleasePayload:
    release: ReleaseType | None
    errors: list[ValidationErrorType]


@strawberry.type
class ReleaseDeletePayload:
    # The id rather than the release. Returning the deleted row would invite a
    # client to render something that no longer exists; the id is what a cache
    # needs in order to evict it.
    deleted_release_id: UUID | None
    errors: list[ValidationErrorType]


@strawberry.type
class EnvironmentPayload:
    environment: EnvironmentType | None
    errors: list[ValidationErrorType]
