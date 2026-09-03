"""Independent adversarial regression cover for Phase 1a-1.

Written from outside the three implementation teams, to test the properties
their own suites assert *around* rather than the ones they assert directly.

The file is in two halves and the split is deliberate:

PART ONE holds guards that pass. Each one pins a property that the existing
suite relies on but does not actually assert -- a connection really being
reused across a release, a middleware really being installed, a migration
really being byte-identical -- so that a future change breaking the property
fails here instead of silently invalidating a test elsewhere.

PART TWO began as four tests that failed on purpose -- executable proofs of
the defects the review found, each asserting the property the code's own
comments claimed to provide. All four defects have since been fixed and all
four now pass. They are kept, and kept in this file, because the property
each one asserts is the property that was got wrong once: a shielded release
outlasting the readiness budget, an exponential fragment walk, and a page
size charged at one when the server will fill in fifty.

They are deliberately written against observable behaviour rather than
against the shape of the fix, so they do not have to be rewritten the next
time the implementation changes. None of them is skipped or xfailed -- an
xfail here would turn a live defect back into a green tick, which is the
failure mode this file exists to prevent.
"""

import asyncio
import hashlib
import time
from pathlib import Path
from urllib.parse import urlparse

import asyncpg
import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr
from starlette.middleware.body_limit import RequestBodyLimitMiddleware

import app.db
import app.rest.health as health
from app.db import COMMAND_TIMEOUT_SECONDS, STATEMENT_TIMEOUT_MS, connect, disconnect
from app.domain.pagination import IssuePage
from app.graphql.context import VectorContext, get_context
from app.graphql.limits import MAX_COMPLEXITY
from app.graphql.queries.issues import DEFAULT_FIRST
from app.graphql.schema import MASKED_ERROR_MESSAGE, build_schema
from app.http_limits import MAX_REQUEST_BODY_BYTES
from app.main import create_app
from app.rest.health import READINESS_TIMEOUT_SECONDS, router

from tests.conftest import make_entity
from tests.test_settings import PLACEHOLDER_DSN, use_environment


# The blob git recorded for migrations/001_issues.sql at commit cd06e14, the
# state this phase started from. CLAUDE.md makes that file immutable, so the
# hash is the assertion.
MIGRATION_001_BLOB_SHA1 = "dfbad1b3b57000f671c180855fe283615cc94950"

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent

STATEMENT_TIMEOUT_QUERY = (
    "SELECT setting FROM pg_settings WHERE name = 'statement_timeout'"
)

# Every scalar an Issue has, plus page info: the widest legitimate selection.
FULL_PAGE_SELECTION = """
    nodes { id title description priority completedAt createdAt updatedAt }
    pageInfo { hasNextPage endCursor }
"""

# Scalars the server produces per row of the selection above, and the two
# pageInfo scalars produced once per connection. Used to price what a
# document actually cost against the budget that let it through.
SCALARS_PER_ROW = 7
SCALARS_PER_CONNECTION = 2

# Aliases are capped at 15, and one un-aliased copy of the same field rides
# along free, so sixteen is the widest fan-out a single operation can buy.
ALIASED_FAN_OUT = 15

# Every scalar on Issue, as one selection set. Costs SCALARS_PER_ROW per row,
# so two copies of it on a page of OVER_BUDGET_PAGE_SIZE rows is over budget
# and one copy is not -- which is what makes a missed spread visible.
ISSUE_NODES = "nodes { id title description priority completedAt createdAt updatedAt }"

# The largest page any service serves. Two copies of ISSUE_NODES at this page
# size cost 1400 of the 1000 budget; one costs 700 and would be accepted.
OVER_BUDGET_PAGE_SIZE = 100


class StubSettings:
    """As much of Settings as connect() reads. Never dialled unless a real
    DSN is handed in."""

    def __init__(self, dsn: str):
        self.database_url = SecretStr(dsn)


class RecordingIssueService:
    """Records every page the resolvers actually asked the service for."""

    def __init__(self):
        self.firsts: list[int] = []

    async def list(self, *, first: int, after: str | None) -> IssuePage:
        self.firsts.append(first)

        return IssuePage(nodes=[make_entity(1)], has_next_page=False, end_cursor=None)


class Context:
    def __init__(self, issue_service):
        self.issue_service = issue_service


def git_blob_sha1(path: Path) -> str:
    """The object name git would record for this file's content.

    Line endings are normalised first: the repository has no .gitattributes
    and core.autocrlf rewrites the working copy, so the bytes on disk are
    not the bytes in the object database.
    """
    content = path.read_bytes().replace(b"\r\n", b"\n")

    return hashlib.sha1(b"blob %d\0" % len(content) + content).hexdigest()


def fragment_diamond(levels: int, fan_out: int = 2) -> str:
    """A document whose fragments expand exponentially but whose text is tiny.

    Each fragment spreads the next one `fan_out` times. Nothing is cyclic and
    nothing is unused, so every stock graphql-core rule accepts it; the
    expansion only exists for a counter that follows spreads.
    """
    definitions = ["query Bomb { issues(first: 1) { nodes { ...F0 } } }"]

    for level in range(levels):
        spreads = " ".join([f"...F{level + 1}"] * fan_out)
        definitions.append(f"fragment F{level} on Issue {{ {spreads} }}")

    definitions.append(f"fragment F{levels} on Issue {{ id }}")

    return "\n".join(definitions)


# ---------------------------------------------------------------------------
# PART ONE -- guards that pass
# ---------------------------------------------------------------------------


def test_migration_001_is_byte_for_byte_what_it_was():
    """CLAUDE.md calls this file immutable; nothing in this phase touched it."""
    assert (
        git_blob_sha1(REPOSITORY_ROOT / "migrations" / "001_issues.sql")
        == MIGRATION_001_BLOB_SHA1
    )


def test_the_body_limit_is_starlettes_own_and_is_actually_mounted(
    monkeypatch, tmp_path
):
    """The middleware exists in the installed starlette and reaches the app.

    Two claims worth separating. `starlette.middleware.body_limit` is new
    enough that "it is imported" is not evidence it is a real API, so the
    class is checked to be starlette's own. And a middleware that is
    imported but never added protects nothing, so the composed application's
    own stack is read back rather than inferred from the call site.
    """
    assert RequestBodyLimitMiddleware.__module__ == "starlette.middleware.body_limit"

    use_environment(
        monkeypatch, tmp_path, DATABASE_URL=PLACEHOLDER_DSN, ENVIRONMENT="test"
    )

    mounted = [
        middleware
        for middleware in create_app().user_middleware
        if middleware.cls is RequestBodyLimitMiddleware
    ]

    assert len(mounted) == 1
    assert mounted[0].kwargs == {"max_body_size": MAX_REQUEST_BODY_BYTES}


async def test_batched_documents_are_refused_rather_than_amplified(
    monkeypatch, tmp_path
):
    """Every limit in this phase is per-document, so batching would undo them.

    Fifty individually-legal operations in one body would be fifty times the
    budget for one request. Strawberry rejects batches unless a batching
    config is set, and this pins that: turning batching on later has to fail
    here rather than quietly multiply every cap.
    """
    use_environment(
        monkeypatch, tmp_path, DATABASE_URL=PLACEHOLDER_DSN, ENVIRONMENT="test"
    )

    service = RecordingIssueService()
    application = create_app()
    application.dependency_overrides[get_context] = lambda: VectorContext(
        issue_service=service
    )

    batch = [
        {"query": "query Q { issues(first: 100) { %s } }" % FULL_PAGE_SELECTION}
        for _ in range(50)
    ]

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application),
        base_url="http://vector.test",
    ) as client:
        response = await client.post("/graphql", json=batch)

    assert response.status_code == 400
    assert service.firsts == []


async def test_an_internal_failure_is_masked_over_the_real_http_stack(
    monkeypatch, tmp_path
):
    """Masking is asserted elsewhere against Schema.execute directly.

    That skips the router, the JSON encoder and the response body, which is
    where a leak would actually reach a client -- so the marker is looked for
    in the bytes on the wire.
    """
    marker = "asyncpg_dsn_leak_marker_4a91c7"

    class Exploding:
        async def list(self, *, first, after):
            raise RuntimeError(marker)

    use_environment(
        monkeypatch, tmp_path, DATABASE_URL=PLACEHOLDER_DSN, ENVIRONMENT="production"
    )

    application = create_app()
    application.dependency_overrides[get_context] = lambda: VectorContext(
        issue_service=Exploding()
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application),
        base_url="http://vector.test",
    ) as client:
        response = await client.post(
            "/graphql", json={"query": "{ issues(first: 1) { nodes { id } } }"}
        )

    assert response.status_code == 200
    assert marker not in response.text
    assert "RuntimeError" not in response.text
    assert response.json()["errors"][0]["message"] == MASKED_ERROR_MESSAGE


@pytest.mark.db
async def test_the_statement_timeout_survives_on_the_very_same_backend(
    postgres_dsn, monkeypatch
):
    """The claim is that RESET ALL restores the timeout, not that it is set.

    The existing test acquires twice and reads the setting twice, which a
    brand-new connection would also satisfy -- a connection opened after the
    first was discarded carries the startup packet's value too, so the
    assertion holds whether or not anything survived anything. Pinning
    pg_backend_pid() is what makes the second read evidence: same backend,
    same session, after a release that ran RESET ALL.
    """
    monkeypatch.setattr(app.db, "_pool", None)
    monkeypatch.setattr(app.db, "get_settings", lambda: StubSettings(postgres_dsn))

    await connect()

    try:
        pool = app.db.get_pool()

        async with pool.acquire() as connection:
            first_pid = await connection.fetchval("SELECT pg_backend_pid()")
            first_timeout = await connection.fetchval(STATEMENT_TIMEOUT_QUERY)

        async with pool.acquire() as connection:
            second_pid = await connection.fetchval("SELECT pg_backend_pid()")
            second_timeout = await connection.fetchval(STATEMENT_TIMEOUT_QUERY)
    finally:
        await disconnect()

    assert second_pid == first_pid
    assert first_timeout == STATEMENT_TIMEOUT_MS
    assert second_timeout == STATEMENT_TIMEOUT_MS


@pytest.mark.db
async def test_the_command_timeout_reaches_the_live_connection(
    postgres_dsn, monkeypatch
):
    """Asserted on the connection, not on the kwargs handed to create_pool.

    Recording what was passed to a faked create_pool proves the call site.
    It cannot prove asyncpg kept the value, which is the only form in which
    it bounds anything.
    """
    monkeypatch.setattr(app.db, "_pool", None)
    monkeypatch.setattr(app.db, "get_settings", lambda: StubSettings(postgres_dsn))

    await connect()

    try:
        async with app.db.get_pool().acquire() as connection:
            assert connection._con._config.command_timeout == COMMAND_TIMEOUT_SECONDS
    finally:
        await disconnect()


@pytest.mark.db
async def test_a_client_side_timeout_really_does_abort_the_query(postgres_dsn):
    """The mechanism, at a bound short enough to assert quickly.

    Run at the configured 10s this would be a ten-second test; the value is
    checked against the live connection above, so what is left to prove is
    that command_timeout aborts rather than merely being stored.
    """
    pool = await asyncpg.create_pool(
        dsn=postgres_dsn,
        min_size=1,
        max_size=1,
        command_timeout=1.0,
        server_settings={"statement_timeout": STATEMENT_TIMEOUT_MS},
    )

    try:
        started = time.monotonic()

        async with pool.acquire() as connection:
            with pytest.raises(TimeoutError):
                await connection.fetchval("SELECT pg_sleep(30)")

        assert time.monotonic() - started < 5.0
    finally:
        await pool.close()


# ---------------------------------------------------------------------------
# PART TWO -- these FAIL, on purpose. Each one is a defect, not a flake.
# ---------------------------------------------------------------------------


class _ShieldedRelease:
    """A connection whose release cannot be cancelled, like asyncpg's.

    asyncpg's Pool.release does `await asyncio.shield(ch.release(timeout))`
    (asyncpg/pool.py:937) and, when the acquire carried no timeout, hands
    that release a budget of None -- which PoolConnectionHolder.release
    spends on an unbounded `_wait_for_cancellation` (asyncpg/pool.py:225-231)
    and an unbounded RESET ALL. So a cancellation delivered mid-query does
    not end the request; it starts a wait nothing can interrupt.

    Modelled here with a finite shielded delay so the test reports rather
    than hangs. Against a real partitioned server there is no delay to wait
    out; see test_readiness_answers_within_its_budget_under_a_partition.
    """

    def __init__(self, release_seconds: float):
        self._release_seconds = release_seconds

    async def __aenter__(self):
        return self

    async def fetchval(self, query, *args):
        await asyncio.Event().wait()

    async def __aexit__(self, *exc_info):
        await asyncio.shield(asyncio.sleep(self._release_seconds))

        return False


class StalledPool:
    def __init__(self, release_seconds: float):
        self._release_seconds = release_seconds

    def acquire(self):
        return _ShieldedRelease(self._release_seconds)


async def test_readiness_answers_within_its_budget_when_release_cannot_be_cancelled(
    monkeypatch,
):
    """DELIBERATE FAILURE -- defect in app/rest/health.py:35-37.

    READINESS_TIMEOUT_SECONDS is documented as the budget for the whole
    check, on the grounds that "a probe that hangs is worse than one that
    fails". asyncio.timeout cancels the task once; the cancellation then has
    to unwind through the acquire context's __aexit__, and asyncpg shields
    that. The endpoint answers only when the shielded release finishes, so
    the budget bounds when cancellation is *requested*, not when the client
    is answered.

    Fix under test: hand the budget to acquire -- pool.acquire(timeout=...)
    -- because asyncpg records it as the release budget too.
    """
    release_seconds = 3.0
    monkeypatch.setattr(health, "get_pool", lambda: StalledPool(release_seconds))

    application = FastAPI()
    application.include_router(router)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application),
        base_url="http://health.test",
    ) as client:
        started = time.monotonic()
        response = await client.get("/readyz")
        elapsed = time.monotonic() - started

    assert response.status_code == 503
    assert elapsed < READINESS_TIMEOUT_SECONDS + 1.0, (
        f"/readyz took {elapsed:.2f}s against a {READINESS_TIMEOUT_SECONDS}s "
        f"budget: the {release_seconds}s shielded release ran to completion "
        "before the 503 was produced"
    )


async def test_refusing_a_fragment_bomb_does_not_grow_exponentially():
    """Was: app/graphql/limits.py measuring 2**levels visits to say no.

    The walk expanded every spread on every path and memoised nothing, and
    the budgets were only consulted once the whole walk had finished. A
    document whose fragments each spread the next one twice therefore cost
    2**levels visits *to reject* -- synchronously, on the event loop. Before
    the fix, on this machine: 772 bytes took 2.3s, 849 bytes 8.6s across both
    limiters, 929 bytes 31.5s. The 256KB body limit stops none of that,
    because the document is under a kilobyte.

    It was a regression, not a shortfall: the pre-phase schema, with no
    extensions at all, executed the same document in ~0.01s, because
    graphql-core merges repeated identical spreads during field collection.

    Two assertions, because either alone can be fooled. The wall-clock bound
    is generous enough never to flake -- the memoised walk takes ~0.01s, and
    the smaller of these two documents took 2.3s before the fix, so there is
    two orders of magnitude of daylight. But a wall clock only ever means
    something on the machine that ran it, so the growth ratio is asserted
    too: four more fragment levels is sixteen times the work if anything
    re-expands, and no more than a handful of extra visits if nothing does.
    That one holds on any machine, however slow.
    """
    smaller = fragment_diamond(levels=18)
    larger = fragment_diamond(levels=22)
    schema = build_schema("test")

    async def refuse(document: str) -> float:
        started = time.monotonic()
        result = await schema.execute(
            document, context_value=Context(RecordingIssueService())
        )
        elapsed = time.monotonic() - started

        assert result.errors is not None, "the bomb must still be refused"

        return elapsed

    # Warm first, on a document that is itself refused. graphql-core does
    # one-off work on a schema's first execution, and charging that to the
    # smaller document would understate the growth between the two.
    await refuse(smaller)

    small_seconds = await refuse(smaller)
    large_seconds = await refuse(larger)

    assert small_seconds + large_seconds < 0.5, (
        f"refusing {len(smaller)} and {len(larger)} bytes took "
        f"{small_seconds:.3f}s and {large_seconds:.3f}s of event-loop time; "
        "before the fix the smaller of the two took 2.3s, and the pre-phase "
        "schema executed it in ~0.01s"
    )

    # The floor is not a fudge: a memoised walk of either document finishes
    # in about a millisecond, which is the clock's own noise, and a ratio
    # taken against noise is noise. Anything that re-expands spreads takes
    # ~27s over this span, so 0.16s is a bound only exponential growth can
    # cross.
    assert large_seconds < max(small_seconds, 0.02) * 8, (
        f"four more fragment levels cost {small_seconds:.3f}s -> "
        f"{large_seconds:.3f}s; a walk that expands spreads rather than "
        "reusing them grows 16x over that span"
    )


async def test_omitting_a_page_size_costs_exactly_what_writing_it_costs():
    """Was: app/graphql/limits.py charging an omitted page size as one row.

    `Query.issues` defaults `first` to 50 (app/graphql/queries/issues.py:11,
    29). While the budget read cardinality from the document alone, omitting
    the argument bought fifty rows for the price of one: the document below
    was priced at 144, accepted, and asked the service for 800 rows across
    sixteen calls.

    The property asserted is the one that closes it, and it is stated
    without naming a number: a page size the client leaves out must cost the
    same as the same page size written down. Anything else makes omission a
    discount, whatever the constants happen to be. The two documents differ
    in one respect only -- whether `first: DEFAULT_FIRST` appears -- so
    identical errors are identical prices.

    The arithmetic is then checked once, derived from the constants rather
    than transcribed, so that retuning DEFAULT_FIRST or MAX_COMPLEXITY moves
    this test with the code instead of leaving it asserting a stale figure.
    """
    fan_out = ALIASED_FAN_OUT + 1

    def document(argument: str) -> str:
        page = f"issues{argument} {{ {FULL_PAGE_SELECTION} }}"

        return (
            "query Pages { "
            + page
            + " "
            + " ".join(f"a{index}: {page}" for index in range(ALIASED_FAN_OUT))
            + " }"
        )

    defaulted = document("")
    written_out = document(f"(first: {DEFAULT_FIRST})")

    schema = build_schema("test")
    service = RecordingIssueService()

    refused = await schema.execute(defaulted, context_value=Context(service))
    equivalent = await schema.execute(written_out, context_value=Context(service))

    # Refused, and refused before anything reached a resolver.
    assert refused.errors is not None
    assert refused.data is None
    assert service.firsts == []

    # Omission is not a discount.
    assert [error.message for error in refused.errors] == [
        error.message for error in equivalent.errors or []
    ]

    # And the price is the one the constants imply: every field under a page
    # is produced once per row, so the whole selection is charged per row.
    scalars_per_page = SCALARS_PER_ROW + SCALARS_PER_CONNECTION
    charged = fan_out * DEFAULT_FIRST * scalars_per_page

    assert [error.message for error in refused.errors] == [
        f"Query complexity is {charged}; the maximum is {MAX_COMPLEXITY}."
    ]
    assert charged > MAX_COMPLEXITY, (
        "the document has to be over budget for this test to mean anything; "
        f"{fan_out} pages of {DEFAULT_FIRST} now costs {charged}"
    )


@pytest.mark.parametrize(
    ("label", "spreads", "definitions", "copies"),
    [
        (
            "one fragment spread twice",
            "...Leaf ...Leaf",
            [f"fragment Leaf on IssueConnection {{ {ISSUE_NODES} }}"],
            2,
        ),
        (
            "reached inside another fragment first, then at the top level",
            "...Outer ...Leaf",
            [
                "fragment Outer on IssueConnection { ...Leaf }",
                f"fragment Leaf on IssueConnection {{ {ISSUE_NODES} }}",
            ],
            2,
        ),
        (
            "a diamond, where two parents share one child",
            "...Left ...Right",
            [
                "fragment Left on IssueConnection { ...Leaf ...Leaf }",
                "fragment Right on IssueConnection { ...Leaf }",
                f"fragment Leaf on IssueConnection {{ {ISSUE_NODES} }}",
            ],
            3,
        ),
    ],
)
async def test_reusing_a_fragment_measurement_still_charges_every_spread(
    label: str, spreads: str, definitions: list[str], copies: int
):
    """Memoisation must not have quietly become a discount.

    Measuring a fragment once and reusing the answer is what made the walk
    linear, and it is sound only because a fragment costs the same wherever
    it is spread -- its selections and its type condition come from its own
    definition, not from the spread site. If that were ever wrong the
    limiter would under-charge silently, which is a worse failure than the
    slowness it replaced, and nothing about the reported numbers would look
    wrong from outside.

    Each shape is therefore priced against the same document with the
    fragments written out by hand. Identical refusals mean the cache charged
    every spread exactly as an inlined copy would; a cache that answered
    once and let the second spread ride free would quote a smaller number
    and the two messages would differ. The shapes are the three where a
    cache could plausibly leak: the same fragment spread twice as siblings,
    a fragment first measured while another was still on the path and then
    spread again at the top level, and a diamond where two parents share a
    child.

    Verified more broadly during review by differential fuzzing -- 4,000
    generated documents measured with and without the cache, no divergence;
    and 773 executable documents in which the charge was never below the
    scalars the server actually produced.
    """
    schema = build_schema("test")
    service = RecordingIssueService()

    through_fragments = "query Q { issues(first: %d) { %s } }\n%s" % (
        OVER_BUDGET_PAGE_SIZE,
        spreads,
        "\n".join(definitions),
    )
    written_out = "query Q { issues(first: %d) { %s } }" % (
        OVER_BUDGET_PAGE_SIZE,
        " ".join([ISSUE_NODES] * copies),
    )

    fragmented = await schema.execute(through_fragments, context_value=Context(service))
    inlined = await schema.execute(written_out, context_value=Context(service))

    assert fragmented.errors is not None, label
    assert [error.message for error in fragmented.errors] == [
        error.message for error in inlined.errors or []
    ], label
    assert service.firsts == [], label


@pytest.mark.db
async def test_readiness_answers_within_its_budget_under_a_partition(
    postgres_dsn, monkeypatch
):
    """DELIBERATE FAILURE -- the same defect, against a real asyncpg pool.

    A TCP relay in front of the container stops moving bytes without closing
    anything, which is what a partitioned or wedged database looks like: the
    socket is fine, the answer never comes. This is precisely the condition
    /readyz exists to report, and it is the condition under which the probe
    does not report at all.

    Observed: still unanswered at 40s against a 2s budget, for both pool
    shapes. With pool.acquire(timeout=READINESS_TIMEOUT_SECONDS) the same
    probe answers in ~4s.
    """
    parsed = urlparse(postgres_dsn)
    proxy = _BlackHoleProxy(parsed.hostname, parsed.port)
    await proxy.start()

    pool = await asyncpg.create_pool(
        dsn=postgres_dsn.replace(
            f"@{parsed.hostname}:{parsed.port}/", f"@127.0.0.1:{proxy.port}/"
        ),
        min_size=1,
        max_size=5,
        command_timeout=COMMAND_TIMEOUT_SECONDS,
        server_settings={"statement_timeout": STATEMENT_TIMEOUT_MS},
    )
    monkeypatch.setattr(health, "get_pool", lambda: pool)

    application = FastAPI()
    application.include_router(router)

    hang_guard = 20.0

    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application),
            base_url="http://health.test",
        ) as client:
            assert (await client.get("/readyz")).status_code == 200

            proxy.block()

            probe = asyncio.ensure_future(client.get("/readyz"))
            started = time.monotonic()
            answered, _ = await asyncio.wait({probe}, timeout=hang_guard)
            elapsed = time.monotonic() - started

            if not answered:
                probe.cancel()

            assert answered, (
                f"/readyz had not answered after {elapsed:.1f}s against a "
                f"{READINESS_TIMEOUT_SECONDS}s budget, with the database "
                "reachable but silent"
            )
            assert elapsed < READINESS_TIMEOUT_SECONDS + 1.0
    finally:
        pool.terminate()
        await proxy.stop()


class _BlackHoleProxy:
    """A TCP relay to PostgreSQL that can be told to stop relaying.

    A partition, not a refusal: the sockets stay open and readable, and
    nothing is ever answered or reset. Closing them instead would produce a
    prompt connection error, which is the case that already works.
    """

    def __init__(self, target_host: str, target_port: int):
        self._target = (target_host, target_port)
        self._blocked = asyncio.Event()
        self._writers: list = []
        self._server = None
        self.port: int | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    def block(self) -> None:
        self._blocked.set()

    async def stop(self) -> None:
        self._blocked.set()

        for writer in self._writers:
            writer.transport.abort()

        self._server.close()

    async def _handle(self, client_reader, client_writer) -> None:
        self._writers.append(client_writer)

        try:
            server_reader, server_writer = await asyncio.open_connection(*self._target)
        except OSError:
            return

        self._writers.append(server_writer)

        await asyncio.gather(
            self._pump(client_reader, server_writer),
            self._pump(server_reader, client_writer),
            return_exceptions=True,
        )

    async def _pump(self, reader, writer) -> None:
        blocked = asyncio.ensure_future(self._blocked.wait())

        try:
            while True:
                read = asyncio.ensure_future(reader.read(65536))
                done, _ = await asyncio.wait(
                    {read, blocked}, return_when=asyncio.FIRST_COMPLETED
                )

                if blocked in done:
                    read.cancel()

                    return

                data = read.result()

                if not data:
                    return

                writer.write(data)
                await writer.drain()
        except (OSError, asyncio.CancelledError):
            pass
        finally:
            blocked.cancel()
