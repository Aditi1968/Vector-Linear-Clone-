from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

import asyncpg

from app.domain.cycles import CycleEntity
from app.domain.errors import ValidationError, ValidationIssue
from app.domain.tenancy import WorkspaceScope
from app.repositories.cycles import CycleRepository


NUMBER_MIN = 1
NUMBER_MAX = 9999

NAME_MAX_LENGTH = 200

# The constraint names migrations/008_cycles.sql declares, named here because
# a violation is only expected input for the constraint it was expected from.
# `except asyncpg.UniqueViolationError` on its own would translate ANY unique
# violation this statement can raise -- today and after the next migration
# adds one -- into "that cycle number is taken", which is how a schema change
# turns into a wrong error message rather than a loud one.
CYCLES_TEAM_NUMBER_KEY = "cycles_team_number_key"
CYCLES_TEAM_FK = "cycles_team_fk"
CYCLES_NO_OVERLAP = "cycles_no_overlap"


class CycleService:
    """Business rules for cycles.

    Validation lives here rather than in the GraphQL layer so that REST,
    workers and internal jobs all go through the same rules. The service
    also owns connection acquisition and transaction boundaries.

    The workspace is threaded through as an argument on every method rather
    than held on the instance, for the reason IssueService gives: an
    instance attribute becomes an ambient current workspace the moment
    anything caches, shares or reuses a service.

    Holding a scope is not permission to act in it. Nothing in this class
    checks that the caller belongs to the workspace it named, or that it may
    write to the team it named; that check does not exist yet, and when it
    does it will not live here.

    ## What is refused where

    Three rules about a cycle are enforced by PostgreSQL and not by any
    statement in this file: that the team a cycle names belongs to the
    workspace the cycle claims, that a team's cycle numbers are unique, and
    that a team's cycles do not overlap in time. Every one of them would be
    racy as a SELECT first -- the team could be deleted, the number taken,
    and the range claimed, between the look and the write -- so the write is
    attempted and the server's refusal is translated here into the same
    structured error a pre-check would have produced. The translation is
    narrowed to one named constraint each: anything else propagates as the
    unexpected failure it is.

    The overlap rule is the one where this matters most, because a
    pre-check for it looks convincing and is not: two concurrent creates
    both read a table without the other's row, both find the range free, and
    both insert. `cycles_no_overlap` is a GiST exclusion constraint, so the
    decision happens inside the index write and the second caller fails
    however the two interleave. Nothing in this file may re-implement it.

    ## Field names in errors

    `ValidationIssue.field` names the GraphQL input field ("teamId",
    "startsAt"), not the Python argument. The field is a contract with a
    client, whose job is to put the message next to the input the user
    typed; every other name is one the client would have to translate.
    """

    def __init__(
        self,
        pool: asyncpg.Pool,
        repository: CycleRepository,
    ):
        self._pool = pool
        self._repository = repository

    async def create(
        self,
        *,
        scope: WorkspaceScope,
        team_id: UUID,
        number: int,
        name: str | None,
        starts_at: datetime,
        ends_at: datetime,
    ) -> CycleEntity:
        """Open one cycle in this workspace, for this team."""
        self._validate_fields(
            number=number,
            name=name,
            starts_at=starts_at,
            ends_at=ends_at,
        )

        async with self._pool.acquire() as connection:
            # The service owns the transaction boundary: later this block
            # will also carry the audit / sync / outbox writes.
            async with connection.transaction():
                try:
                    return await self._repository.create(
                        connection,
                        scope=scope,
                        team_id=team_id,
                        number=number,
                        name=name,
                        starts_at=starts_at,
                        ends_at=ends_at,
                    )
                except asyncpg.UniqueViolationError as error:
                    raise self._duplicate_number(error, number) from None
                except asyncpg.ExclusionViolationError as error:
                    raise self._overlapping_dates(error) from None
                except asyncpg.ForeignKeyViolationError as error:
                    raise self._unknown_team(error) from None

    async def get_by_id(
        self,
        *,
        scope: WorkspaceScope,
        cycle_id: UUID,
    ) -> CycleEntity | None:
        """One cycle from this workspace, or nothing.

        "Not in this workspace" and "does not exist" are the same answer on
        purpose; the repository explains why the distinction must not be
        observable.
        """
        async with self._pool.acquire() as connection:
            return await self._repository.get_by_id(
                connection,
                scope=scope,
                cycle_id=cycle_id,
            )

    async def get_many(
        self,
        *,
        scope: WorkspaceScope,
        cycle_ids: Sequence[UUID],
    ) -> list[CycleEntity]:
        """Those of these cycles that exist in this workspace.

        The batched form of get_by_id, and it withholds exactly as much: an
        id from another tenant is absent from the result, indistinguishable
        from an id that exists nowhere.

        An empty request is answered without a connection. The alternative
        is a round trip to ask the server about no rows, on a path
        (`Issue.cycle` over a page of issues in no cycle) where that is the
        common case rather than the odd one.
        """
        if not cycle_ids:
            return []

        async with self._pool.acquire() as connection:
            return await self._repository.find_many_by_ids(
                connection,
                scope=scope,
                cycle_ids=cycle_ids,
            )

    async def list_for_team(
        self,
        *,
        scope: WorkspaceScope,
        team_id: UUID,
    ) -> list[CycleEntity]:
        """One team's cycles, lowest number first.

        A team that has none and a team that is not in this workspace both
        answer with an empty list. That is the same refusal to confirm
        existence the single-cycle lookup makes: a client sweeping team ids
        must not be able to tell a real team in another tenant from an id
        that names nothing.

        A single SELECT needs no explicit write transaction, so this
        acquires a connection without opening one.
        """
        async with self._pool.acquire() as connection:
            return await self._repository.list_for_team(
                connection,
                scope=scope,
                team_id=team_id,
            )

    async def update(
        self,
        *,
        scope: WorkspaceScope,
        cycle_id: UUID,
        number: int,
        name: str | None,
        starts_at: datetime,
        ends_at: datetime,
    ) -> CycleEntity:
        """Rewrite a cycle's editable fields, whole.

        Every field is required: this replaces the cycle's editable state
        rather than patching part of it. A partial update would have to
        distinguish "leave the name alone" from "clear the name", which
        neither GraphQL nullability nor a Python default expresses without a
        third sentinel value -- and the read-modify-write that avoids the
        sentinel is a lost update waiting for two clients to edit at once.

        A cycle that is not in this workspace raises the same NOT_FOUND as
        one that does not exist. Raised after the statement rather than
        before it, because a SELECT first would answer a question the UPDATE
        answers anyway, one statement earlier and with no lock.
        """
        self._validate_fields(
            number=number,
            name=name,
            starts_at=starts_at,
            ends_at=ends_at,
        )

        async with self._pool.acquire() as connection:
            async with connection.transaction():
                try:
                    entity = await self._repository.update(
                        connection,
                        scope=scope,
                        cycle_id=cycle_id,
                        number=number,
                        name=name,
                        starts_at=starts_at,
                        ends_at=ends_at,
                    )
                except asyncpg.UniqueViolationError as error:
                    raise self._duplicate_number(error, number) from None
                except asyncpg.ExclusionViolationError as error:
                    raise self._overlapping_dates(error) from None

            # Outside the transaction: an UPDATE that matched nothing changed
            # nothing, so there is no work to roll back and no reason to hold
            # the transaction open while deciding what its emptiness meant.
            if entity is None:
                raise self._not_found()

            return entity

    async def delete(
        self,
        *,
        scope: WorkspaceScope,
        cycle_id: UUID,
    ) -> UUID:
        """Delete one cycle, returning the id that was deleted.

        The id comes back so the caller can report what it removed without
        echoing what it was asked to remove: on this path they are the same
        value, and a payload that echoed its input would go on looking
        correct if the delete ever stopped being.

        Issues in the cycle survive it and lose only their membership; the
        repository explains where that happens and why it is one statement.
        """
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                deleted = await self._repository.delete(
                    connection,
                    scope=scope,
                    cycle_id=cycle_id,
                )

            if not deleted:
                raise self._not_found()

            return cycle_id

    @staticmethod
    def _not_found() -> ValidationError:
        """The one answer for absent, and for belonging to another tenant.

        Deliberately not two codes. A client that could tell "no such cycle"
        from "not yours" could enumerate other workspaces' cycles by id.
        """
        return ValidationError(
            [
                ValidationIssue(
                    field="id",
                    code="NOT_FOUND",
                    message="Cycle not found",
                )
            ]
        )

    @staticmethod
    def _duplicate_number(
        error: asyncpg.UniqueViolationError,
        number: int,
    ) -> Exception:
        """Translate a per-team numbering collision, or hand back the original.

        Returned rather than raised so the call site reads
        `raise ... from None`: this is the one place a server error becomes
        a field error, and it should be visible as a raise where it happens.
        """
        if error.constraint_name != CYCLES_TEAM_NUMBER_KEY:
            # Annotated rather than returned inline: asyncpg ships no types,
            # so the parameter is Any and would silently satisfy any return
            # type this function grew later. The same note stands on the two
            # translations below.
            unexpected: Exception = error

            return unexpected

        return ValidationError(
            [
                ValidationIssue(
                    field="number",
                    code="DUPLICATE",
                    message=f"This team already has a cycle numbered {number}",
                )
            ]
        )

    @staticmethod
    def _overlapping_dates(error: asyncpg.ExclusionViolationError) -> Exception:
        """Translate a clash with another of the team's cycles, or hand back
        the original.

        Reported on `startsAt` because that is the field a client moves to
        resolve it, and because a two-field error would put the same message
        twice under one input. The message does not name the cycle in the
        way -- that would be a second query on a failure path, and it would
        report a row the caller may have no other route to.

        Narrowed to the one constraint, like every other translation here:
        an exclusion violation from a constraint added later is not this
        error, and must not be described to a client as one.
        """
        if error.constraint_name != CYCLES_NO_OVERLAP:
            unexpected: Exception = error

            return unexpected

        return ValidationError(
            [
                ValidationIssue(
                    field="startsAt",
                    code="OVERLAPPING",
                    message="This team already has a cycle covering those dates",
                )
            ]
        )

    @staticmethod
    def _unknown_team(error: asyncpg.ForeignKeyViolationError) -> Exception:
        """Translate a team that is not this workspace's, or hand back the original.

        The message says the team was not found rather than that it belongs
        to someone else, and the two cases are genuinely indistinguishable
        from here: `cycles_team_fk` refuses a team id that names nothing and
        a team id that names another tenant's team identically, which is the
        property that stops a client sweeping ids to map a stranger's teams.
        """
        if error.constraint_name != CYCLES_TEAM_FK:
            unexpected: Exception = error

            return unexpected

        return ValidationError(
            [
                ValidationIssue(
                    field="teamId",
                    code="NOT_FOUND",
                    message="Team not found",
                )
            ]
        )

    @staticmethod
    def _validate_fields(
        *,
        number: int,
        name: str | None,
        starts_at: datetime,
        ends_at: datetime,
    ) -> None:
        """Collect every violation, then raise once.

        Field order is deterministic (number, name, startsAt, endsAt) so
        that clients can rely on it. The codes and messages are a public
        contract.

        Shared by create and update because a cycle's fields do not become
        more or less valid once it exists; a rule enforced on one path only
        is a rule a client can get around by creating and then editing.
        """
        issues: list[ValidationIssue] = []

        if number < NUMBER_MIN or number > NUMBER_MAX:
            issues.append(
                ValidationIssue(
                    field="number",
                    code="OUT_OF_RANGE",
                    message=f"Number must be between {NUMBER_MIN} and {NUMBER_MAX}",
                )
            )

        # The name is validated as supplied -- never trimmed or rewritten.
        # An absent name is the ordinary case (a cycle is "Cycle 7" until
        # someone titles it); an empty one is a client sending a field it
        # should have omitted, and silently storing it would make two
        # spellings of "unnamed" that every reader has to handle.
        if name is not None:
            if not name:
                issues.append(
                    ValidationIssue(
                        field="name",
                        code="REQUIRED",
                        message="Name must not be empty when provided",
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

        # Both bounds are stored as TIMESTAMPTZ, which is an instant. A
        # datetime with no offset does not name one: it names a wall-clock
        # reading whose meaning depends on a timezone nobody sent, and
        # asyncpg would encode it as UTC rather than refuse it -- so a
        # client in Sydney would silently open its cycle eleven hours late.
        # GraphQL's DateTime parses `2026-01-05T09:00:00` happily, so this
        # is the only layer that can say no.
        aware = [
            field
            for field, value in (("startsAt", starts_at), ("endsAt", ends_at))
            if value.utcoffset() is None
        ]

        issues.extend(
            ValidationIssue(
                field=field,
                code="INVALID",
                message=f"{field} must include a timezone offset",
            )
            for field in aware
        )

        # Only comparable once both are instants; comparing an aware
        # datetime with a naive one raises TypeError, which would turn a
        # reportable input error into a masked server error.
        if not aware and ends_at <= starts_at:
            issues.append(
                ValidationIssue(
                    field="endsAt",
                    code="OUT_OF_RANGE",
                    message="endsAt must be after startsAt",
                )
            )

        if issues:
            raise ValidationError(issues)
