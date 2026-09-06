"""Shared pytest fixtures."""

import asyncio
import functools
import json
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

import asyncpg
import pytest

from app.domain.issues import IssueEntity
from app.domain.tenancy import WorkspaceScope
from app.graphql.context import VectorContext
from app.repositories.issues import IssueRepository
from app.repositories.teams import TeamRepository
from app.services.issues import IssueService
from app.services.teams import TeamService
from scripts.apply_migration import apply_migration


MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations"

BASE_TIME = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

# The tenant for tests that need a scope but are not themselves about
# tenancy. Deliberately NOT the bootstrap ids from migrations/002_tenancy.sql:
# a test that passed only because the value happened to be the one the
# migration seeds would be testing the constant, not the plumbing. Nothing
# here reaches a database, so these ids need to exist nowhere.
TEST_WORKSPACE_ID = UUID("00000000-0000-7000-8000-0000000000fa")
TEST_TEAM_ID = UUID("00000000-0000-7000-8000-0000000000fb")

TEST_SCOPE = WorkspaceScope(workspace_id=TEST_WORKSPACE_ID)

# The workflow state a fake row sits in, and the key its team renders under.
# Neither reaches a database either, so they only have to be well-formed:
# `teams_key_format` in 005 requires uppercase with no hyphen, and a hyphen
# here would make `identifier` ambiguous, which is the one thing the entity's
# rendering relies on.
TEST_WORKFLOW_STATE_ID = UUID("00000000-0000-7000-8000-0000000000fc")
TEST_TEAM_KEY = "ENG"

# uuidv7() in migrations/001_issues.sql is native to PostgreSQL 18; 16 and 17
# reject the migration outright, so the tag is pinned rather than floating.
POSTGRES_IMAGE = "postgres:18"
POSTGRES_USER = "vector"
POSTGRES_PASSWORD = "vector"
POSTGRES_DB = "vector_test"

CONTAINER_PORT = "5432/tcp"

# "<host ip>::<container port>" -- the empty host port is what asks Docker for
# an ephemeral one. Binding to loopback keeps the throwaway database off the
# network, and naming no fixed host port keeps a local PostgreSQL on 5432, or
# a second test run, from colliding with it.
PUBLISH_SPEC = "127.0.0.1::5432"

# Status and the whole published-port map in one round trip. `json` renders a
# missing map as `null` rather than erroring, so a container too young to have
# one is data to wait on, not a failure to report.
INSPECT_FORMAT = "{{.State.Status}}\t{{json .NetworkSettings.Ports}}"

# States a container can still gain a port binding from. From any other one --
# exited, dead, removing -- it never will, so waiting only delays the report.
LIVE_STATES = frozenset({"created", "running", "restarting", "paused"})

DOCKER_PROBE_TIMEOUT = 10.0
CONTAINER_START_TIMEOUT = 300.0
CONTAINER_READY_TIMEOUT = 60.0
CONTAINER_STOP_TIMEOUT = 60.0
PORT_PUBLISH_TIMEOUT = 30.0
PORT_POLL_INTERVAL = 0.25
LOG_TAIL_LINES = 20


class ExplodingPool:
    """Stand-in for asyncpg.Pool that fails if anything acquires a connection.

    Validation must reject bad input before a connection is ever taken, so
    any acquire during a validation test is a test failure by definition.
    """

    def __init__(self):
        self.acquire_count = 0

    def acquire(self):
        self.acquire_count += 1

        raise AssertionError("pool.acquire() must not be called for invalid input")


class _NullTransaction:
    """What `FakeConnection.transaction()` hands back: a shape, not a scope."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False


class FakeConnection:
    """Records every query issued against it and replays canned rows.

    `row` and `value` default to None so a repository's not-found path is
    the default behaviour of the fake rather than something a test has to
    arrange.

    `transaction()` is a no-op context manager rather than a recorded call.
    Services open one around every write, so a fake without it fails with an
    AttributeError from inside the service -- which reports the fixture rather
    than the behaviour. It commits nothing and rolls back nothing, because
    there is no server here to do either; a test whose subject is transaction
    boundaries needs a real one.
    """

    def __init__(self, rows=None, row=None, value=None):
        self.rows = rows if rows is not None else []
        self.row = row
        self.value = value
        self.queries: list[dict] = []

    def transaction(self):
        return _NullTransaction()

    async def execute(self, query, *args):
        self.queries.append({"query": query, "args": args})

        return "INSERT 0 1"

    async def fetch(self, query, *args):
        self.queries.append({"query": query, "args": args})

        return self.rows

    async def fetchrow(self, query, *args):
        self.queries.append({"query": query, "args": args})

        return self.row

    async def fetchval(self, query, *args):
        self.queries.append({"query": query, "args": args})

        return self.value


# Every table in `public`, dropped in one statement, whatever they are.
#
# The alternative -- each db test naming the tables it expects -- is a list
# that is correct until the next migration adds a table, and then fails in
# whichever file happens to run second, on a `DROP TABLE` that leaves a
# dangling foreign key. That failure names a table the file has never heard
# of and has nothing to do with the test reporting it, which is how a
# migration lands looking like it broke four unrelated suites.
#
# The identifiers are quoted by `format('%I')` on the server rather than
# interpolated here: they come out of the catalog, so a table whose name
# needs quoting is a name PostgreSQL is asked to render, not one this file
# guesses at. CASCADE covers dependent objects a plain multi-table drop
# would not resolve -- a view over one of them, say.
DROP_ALL_TABLES_SQL = """
SELECT 'DROP TABLE ' || string_agg(format('%I.%I', schemaname, tablename), ', ')
       || ' CASCADE'
FROM pg_tables
WHERE schemaname = 'public'
"""


async def reset_schema(connection) -> None:
    """Leave the test database with no tables at all.

    Every db test in this suite rebuilds the schema from the migrations, and
    every one of them has to start from nothing -- including nothing left
    behind by whichever file ran before it, since the container is shared for
    the whole session.
    """
    statement = await connection.fetchval(DROP_ALL_TABLES_SQL)

    # None when the database is already empty: string_agg over no rows is
    # NULL, so there is nothing to drop and nothing to run.
    if statement is not None:
        await connection.execute(statement)


async def apply_all_migrations(connection) -> list[str]:
    """Apply every file in `migrations/`, in filename order, via the runner.

    Globbed rather than listed. A suite that creates an issue needs whatever
    the schema currently demands of an insert, and that grows: 005 added two
    NOT NULL columns to `issues` and a `workflow_states` table the insert now
    resolves against. A fixture naming versions by hand goes stale the day
    the next migration lands, and it goes stale as a confusing failure in a
    file about something else.

    Use this wherever the subject is the *application* against the schema.
    A suite whose subject is one migration should keep applying the prefix it
    is about -- `tests/test_migration_002_db.py` asserts what 002 built, and
    running 005 underneath it would change the answer.
    """
    applied = []

    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        async with connection.transaction():
            await apply_migration(connection, path, migrations_dir=MIGRATIONS_DIR)

        applied.append(path.name)

    return applied


# The five states migrations/005_team_workflows.sql puts on every team, in its
# order and with its categories.
WORKFLOW_STATE_SEED = (
    ("Backlog", "backlog", 0, "#bec2c8"),
    ("Todo", "unstarted", 1, "#e2e2e2"),
    ("In Progress", "started", 2, "#f2c94c"),
    ("Done", "completed", 3, "#5e6ad2"),
    ("Canceled", "canceled", 4, "#95a2b3"),
)


async def seed_workflow_states(connection, workspace_id, team_id) -> None:
    """Give a team the board 005 would have given it.

    005 seeds `workflow_states` for every team that exists when it runs, so a
    team a fixture inserts *afterwards* has none -- and an issue on that team
    cannot be created at all, because `workflow_state_id` is NOT NULL and
    resolved by category from the team's own states.

    That is not a quirk of the tests: it is what the product will have to do
    when it grows a "create team" path, and this helper is the smallest
    honest stand-in until that path exists.
    """
    await connection.executemany(
        """
        INSERT INTO workflow_states
            (workspace_id, team_id, name, type, position, color)
        VALUES ($1, $2, $3, $4, $5, $6)
        """,
        [
            (workspace_id, team_id, name, category, position, color)
            for name, category, position, color in WORKFLOW_STATE_SEED
        ],
    )


class UnusedService:
    """A context slot the test under way must not touch.

    Filling one with None makes an unexpected call fail as `AttributeError:
    'NoneType' object has no attribute 'list'` from somewhere inside a
    resolver, which says nothing about which service was reached or why that
    was wrong. This fails with a sentence naming both.
    """

    def __init__(self, name: str):
        self._name = name

    def __getattr__(self, attribute):
        raise AssertionError(
            f"this test must not reach {self._name}.{attribute}; "
            "pass a real fake for it if the behaviour is under test"
        )


def graphql_context(**services) -> VectorContext:
    """A `VectorContext` with only the services a test actually uses.

    Transport tests -- body limits, masking, composition -- exercise one
    service and have no opinion about the others, but the context requires
    every one of them, deliberately: a resolver that reaches a service the
    request never wired up is a defect, and a None in the slot would hide it
    behind an AttributeError.

    So the unnamed slots are filled with `UnusedService` rather than None,
    and adding a service to `VectorContext` means editing this function
    rather than every test that builds a context.
    """
    slots = {
        "issue_service",
        "team_service",
        "workspace_service",
        "auth_service",
        "membership_service",
        "label_service",
        "comment_service",
    }
    environment = services.pop("environment", "test")
    tenant = services.pop("tenant", None) or FakeTenant()
    unexpected = set(services) - slots

    assert not unexpected, (
        f"VectorContext has no {', '.join(sorted(unexpected))}; this helper "
        "is out of date with app/graphql/context.py"
    )

    return VectorContext(
        **{name: services.get(name) or UnusedService(name) for name in slots},
        # Not services, so not `UnusedService` slots. `tenant` gets a working
        # fake because almost every resolver path needs a scope to run at
        # all, and `environment` is a plain value whose default only decides
        # whether a Set-Cookie would carry Secure.
        tenant=tenant,
        environment=environment,
    )


class _AcquireContext:
    def __init__(self, connection):
        self._connection = connection

    async def __aenter__(self):
        return self._connection

    async def __aexit__(self, *exc_info):
        return False


class FakePool:
    """Pool that hands out a FakeConnection and counts acquisitions."""

    def __init__(self, connection=None):
        self.acquire_count = 0
        self.connection = connection if connection is not None else FakeConnection()

    def acquire(self):
        self.acquire_count += 1

        return _AcquireContext(self.connection)


class FakeIssueRepository:
    """Returns canned entities and records the arguments it was called with."""

    def __init__(self, rows=None):
        self.rows = rows if rows is not None else []
        self.list_calls: list[dict] = []

    async def list(self, connection, *, scope, limit, after_created_at, after_id):
        self.list_calls.append(
            {
                "scope": scope,
                "limit": limit,
                "after_created_at": after_created_at,
                "after_id": after_id,
            }
        )

        return list(self.rows)


class AnonymousAuthService:
    """Authenticates nobody, which is a real answer rather than a gap.

    `VectorContext.viewer()` resolves through the auth service, so any test
    exercising a resolver that asks who the caller is has to wire one --
    `issueCreate` does, because it records authorship. An `UnusedService` in
    that slot would fail the request instead of answering "anonymous", and
    anonymous is exactly the case these transport tests want: it is what a
    request with no session cookie gets, and `issues.creator_id` is nullable
    to accommodate it.
    """

    async def authenticate(self, token: str | None) -> None:
        return None


class FakeTenant:
    """The tenant seam the GraphQL layer reads, without a database.

    Mirrors app/graphql/tenancy.RequestTenant. Kept in step with it by hand,
    which is the cost of every fake -- the compensating test is
    tests/test_issue_tenancy.py, which asserts the real resolvers pass what
    they get from here straight through to the service.
    """

    def __init__(self, scope=TEST_SCOPE, team_id=TEST_TEAM_ID):
        self._scope = scope
        self._team_id = team_id

    async def scope(self):
        return self._scope

    async def team_id(self, scope):
        return self._team_id


def as_record(entity: IssueEntity) -> dict:
    """asyncpg.Record supports __getitem__, which a dict models well enough.

    Keyed by the column names the repository's SELECT list produces, not by
    the entity's attribute names -- the two differ at `team_key`, which is a
    subquery alias rather than a column of `issues`. Building this from the
    entity keeps a fake row and a real one in step: a column added to the
    entity and forgotten here fails every test that maps a row.
    """
    return {
        "id": entity.id,
        "team_id": entity.team_id,
        "team_key": entity.team_key,
        "number": entity.number,
        "title": entity.title,
        "description": entity.description,
        "priority": entity.priority,
        "workflow_state_id": entity.workflow_state_id,
        "assignee_id": entity.assignee_id,
        "creator_id": entity.creator_id,
        "estimate": entity.estimate,
        "due_date": entity.due_date,
        "completed_at": entity.completed_at,
        "archived_at": entity.archived_at,
        "created_at": entity.created_at,
        "updated_at": entity.updated_at,
    }


def normalize(sql: str) -> str:
    return " ".join(sql.split())


def make_entity(index: int, **overrides) -> IssueEntity:
    """Deterministic entity; higher index means newer created_at.

    `number` follows the index so that `identifier` is predictable per
    entity, and `**overrides` lets a test vary one field without restating
    the other fifteen -- which is what keeps a test about, say, the assignee
    from silently also asserting a title.
    """
    created_at = BASE_TIME + timedelta(minutes=index)

    # Merged as a dict rather than spread after the keywords. `f(id=..., **o)`
    # raises TypeError the moment `o` carries a key already named, so the
    # spread form makes exactly the fields a test is most likely to override
    # the ones it cannot.
    fields = {
        "id": UUID(int=index),
        "team_id": TEST_TEAM_ID,
        "team_key": TEST_TEAM_KEY,
        "number": index,
        "title": f"Issue {index}",
        "description": None,
        "priority": 1,
        "workflow_state_id": TEST_WORKFLOW_STATE_ID,
        "assignee_id": None,
        "creator_id": None,
        "estimate": None,
        "due_date": None,
        "completed_at": None,
        "archived_at": None,
        "created_at": created_at,
        "updated_at": created_at,
    }

    return IssueEntity(**(fields | overrides))


@pytest.fixture
def exploding_pool() -> ExplodingPool:
    return ExplodingPool()


@pytest.fixture
def issue_service(exploding_pool: ExplodingPool) -> IssueService:
    """The service under validation tests, over a pool that refuses to open.

    The team service is built over the *same* exploding pool deliberately.
    Creating an issue asks it for a workflow state and a number, but only
    from inside `pool.acquire()` -- so on valid input the acquire is still
    the first thing that fails, which is what the max-length test reads as
    proof that validation accepted the input.
    """
    return IssueService(
        pool=exploding_pool,
        repository=IssueRepository(),
        teams=TeamService(pool=exploding_pool, repository=TeamRepository()),
    )


@functools.cache
def docker_available() -> bool:
    """Whether a Docker daemon will answer us.

    An absent or unreachable daemon is an environment fact, not a defect, so
    it must produce a skip. Probed once and cached: the answer cannot change
    usefully mid-session, and paying the timeout per test would be a hang in
    all but name.
    """
    try:
        probe = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            timeout=DOCKER_PROBE_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return False

    return probe.returncode == 0


async def _accept_connections(dsn: str) -> None:
    """Poll until postgres completes a real connection.

    Bound the wait from outside, not in here: the container publishes its
    port before the server is ready, so a fixed sleep is either a flake or
    wasted time.
    """
    while True:
        try:
            connection = await asyncpg.connect(dsn, timeout=2.0)
        except (OSError, asyncio.TimeoutError, asyncpg.PostgresError):
            await asyncio.sleep(0.25)
        else:
            await connection.close()

            return


def _inspect_container(container: str) -> tuple[str, dict]:
    """(container status, the published-port map Docker has recorded).

    Read through `docker inspect` rather than `docker port`. `docker port`
    answers in text and exits 0 while printing nothing at all when it knows
    the port but holds no binding for it, so its "not yet" and its "here it
    is" are only distinguishable by parsing -- which is how an absent mapping
    used to surface as an IndexError three lines later. JSON makes the empty
    case an empty list, and the status rides along in the same round trip so
    the caller can tell waiting-will-help from waiting-is-pointless.
    """
    inspected = subprocess.run(
        ["docker", "inspect", container, "--format", INSPECT_FORMAT],
        capture_output=True,
        text=True,
        timeout=DOCKER_PROBE_TIMEOUT,
    )

    if inspected.returncode != 0:
        raise RuntimeError(
            f"docker inspect {container} failed with exit status "
            f"{inspected.returncode}: {inspected.stderr.strip() or '<no stderr>'}"
        )

    status, _, raw = inspected.stdout.strip().partition("\t")

    try:
        ports = json.loads(raw) if raw else None
    except ValueError:
        raise RuntimeError(
            f"docker inspect {container} returned an unreadable port map: {raw!r}"
        ) from None

    return status, ports or {}


def _container_logs(container: str) -> str:
    """Best-effort tail of the container's output, for failure messages.

    Diagnosing a container that never published a port nearly always means
    reading why it stopped, and the fixture deletes it on the way out.
    """
    try:
        logs = subprocess.run(
            ["docker", "logs", "--tail", str(LOG_TAIL_LINES), container],
            capture_output=True,
            text=True,
            timeout=DOCKER_PROBE_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return "<docker logs unavailable>"

    return (logs.stdout + logs.stderr).strip() or "<no container output>"


def _no_port_error(container: str, status: str, ports: dict, why: str) -> RuntimeError:
    """The report that an unpublished port used to make as an IndexError."""
    return RuntimeError(
        f"Docker published no host port for {CONTAINER_PORT} on container "
        f"{container}: {why}. Container status: {status or '<unknown>'}; "
        f"ports Docker reported: {ports!r}; requested publish spec: "
        f"{PUBLISH_SPEC!r}. Last container output:\n{_container_logs(container)}"
    )


def _published_port(container: str) -> int:
    """Host port Docker chose for the container's 5432.

    Polled rather than read once: `docker run --detach` returns when the
    container has started, which is not necessarily the instant the host-side
    binding is queryable. A container that has already stopped will never
    gain one, so that case reports immediately instead of waiting out the
    timeout on a corpse.
    """
    deadline = time.monotonic() + PORT_PUBLISH_TIMEOUT

    while True:
        status, ports = _inspect_container(container)
        bindings = ports.get(CONTAINER_PORT) or []

        # Publishing to 127.0.0.1 yields a single binding, but prefer an IPv4
        # one anyway: the DSN dials 127.0.0.1, so a v6-only binding would hand
        # back a port with nothing listening on it. False sorts first.
        for binding in sorted(
            bindings, key=lambda found: ":" in (found.get("HostIp") or "")
        ):
            host_port = binding.get("HostPort")

            if host_port:
                return int(host_port)

        if status not in LIVE_STATES:
            raise _no_port_error(
                container,
                status,
                ports,
                "the container stopped before Docker recorded one",
            )

        if time.monotonic() >= deadline:
            raise _no_port_error(
                container,
                status,
                ports,
                f"none appeared within {PORT_PUBLISH_TIMEOUT:.0f}s",
            )

        time.sleep(PORT_POLL_INTERVAL)


def _remove_container(container: str) -> None:
    """Delete the container, tolerating one that was never created.

    Called from a finally, so it must not raise: a cleanup failure must not
    replace the failure that caused it.
    """
    try:
        subprocess.run(
            ["docker", "rm", "--force", container],
            capture_output=True,
            timeout=CONTAINER_STOP_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        pass


@pytest.fixture(scope="session")
def postgres_dsn():
    """DSN for a throwaway PostgreSQL container, removed with the session.

    The host port is whatever Docker hands out; 5432 is frequently taken by
    a local server and assuming it would silently test the wrong database.
    """
    if not docker_available():
        pytest.skip("Docker daemon is unreachable")

    container = f"vector-test-{uuid4().hex[:12]}"

    # `docker run` is inside the try because it can leave a container behind
    # even when it does not return one: a pull slow enough to hit
    # CONTAINER_START_TIMEOUT raises out of subprocess with the container
    # already created, and only the finally below can still name it.
    try:
        started = subprocess.run(
            [
                "docker",
                "run",
                "--detach",
                "--name",
                container,
                "--env",
                f"POSTGRES_USER={POSTGRES_USER}",
                "--env",
                f"POSTGRES_PASSWORD={POSTGRES_PASSWORD}",
                "--env",
                f"POSTGRES_DB={POSTGRES_DB}",
                "--publish",
                PUBLISH_SPEC,
                POSTGRES_IMAGE,
            ],
            capture_output=True,
            text=True,
            timeout=CONTAINER_START_TIMEOUT,
        )

        if started.returncode != 0:
            pytest.skip(f"could not start {POSTGRES_IMAGE}: {started.stderr.strip()}")

        port = _published_port(container)
        dsn = (
            f"postgresql://{POSTGRES_USER}:{POSTGRES_PASSWORD}"
            f"@127.0.0.1:{port}/{POSTGRES_DB}"
        )

        try:
            asyncio.run(
                asyncio.wait_for(
                    _accept_connections(dsn),
                    timeout=CONTAINER_READY_TIMEOUT,
                )
            )
        except asyncio.TimeoutError:
            raise RuntimeError(
                f"{POSTGRES_IMAGE} did not accept connections on port {port} "
                f"within {CONTAINER_READY_TIMEOUT:.0f}s"
            ) from None

        yield dsn
    finally:
        # Unconditional: a failure above must not leave a container running,
        # and a failure to remove one must not mask the failure above it.
        _remove_container(container)
