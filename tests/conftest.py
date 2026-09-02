"""Shared pytest fixtures."""

import asyncio
import functools
import json
import subprocess
import time
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import asyncpg
import pytest

from app.domain.issues import IssueEntity
from app.repositories.issues import IssueRepository
from app.services.issues import IssueService


BASE_TIME = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

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

        raise AssertionError(
            "pool.acquire() must not be called for invalid input"
        )


class FakeConnection:
    """Records every query issued against it and replays canned rows.

    `row` defaults to None so a repository's not-found path is the default
    behaviour of the fake rather than something a test has to arrange.
    """

    def __init__(self, rows=None, row=None):
        self.rows = rows if rows is not None else []
        self.row = row
        self.queries: list[dict] = []

    async def fetch(self, query, *args):
        self.queries.append({"query": query, "args": args})

        return self.rows

    async def fetchrow(self, query, *args):
        self.queries.append({"query": query, "args": args})

        return self.row


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

    async def list(self, connection, *, limit, after_created_at, after_id):
        self.list_calls.append(
            {
                "limit": limit,
                "after_created_at": after_created_at,
                "after_id": after_id,
            }
        )

        return list(self.rows)


def as_record(entity: IssueEntity) -> dict:
    """asyncpg.Record supports __getitem__, which a dict models well enough."""
    return {
        "id": entity.id,
        "title": entity.title,
        "description": entity.description,
        "priority": entity.priority,
        "completed_at": entity.completed_at,
        "created_at": entity.created_at,
        "updated_at": entity.updated_at,
    }


def normalize(sql: str) -> str:
    return " ".join(sql.split())


def make_entity(index: int) -> IssueEntity:
    """Deterministic entity; higher index means newer created_at."""
    created_at = BASE_TIME + timedelta(minutes=index)

    return IssueEntity(
        id=UUID(int=index),
        title=f"Issue {index}",
        description=None,
        priority=1,
        completed_at=None,
        created_at=created_at,
        updated_at=created_at,
    )


@pytest.fixture
def exploding_pool() -> ExplodingPool:
    return ExplodingPool()


@pytest.fixture
def issue_service(exploding_pool: ExplodingPool) -> IssueService:
    return IssueService(pool=exploding_pool, repository=IssueRepository())


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
            f"docker inspect {container} returned an unreadable port map: "
            f"{raw!r}"
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


def _no_port_error(
    container: str, status: str, ports: dict, why: str
) -> RuntimeError:
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
            pytest.skip(
                f"could not start {POSTGRES_IMAGE}: {started.stderr.strip()}"
            )

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
