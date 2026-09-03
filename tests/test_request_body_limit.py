"""Oversized request bodies die at the edge. No database involved.

A body limit is only worth having if it holds against a request that lies
about its size, so each of the three shapes is asserted separately: an
honest oversize declaration, an understated one, and a chunked request that
declares nothing at all. The interesting assertion in every case is not the
status code but what the server did *not* do -- read the body, or run the
query.

The application under test is the real one from create_app, so a limit
that were dropped from the composition root would fail these tests rather
than pass them against a hand-wired stand-in.
"""

import json

import httpx
import pytest

from app.domain.pagination import IssuePage
from app.graphql.context import VectorContext, get_context
from app.http_limits import MAX_REQUEST_BODY_BYTES
from app.main import create_app

from tests.conftest import make_entity
from tests.test_settings import PLACEHOLDER_DSN, use_environment


ISSUES_QUERY = """
query ListIssues {
  issues(first: 1) {
    nodes {
      id
      title
    }
  }
}
"""

JSON_HEADERS = {"content-type": "application/json"}

# Small enough that "how much did the server read before giving up" is a
# meaningful measurement rather than a single all-or-nothing chunk.
CHUNK_BYTES = 16 * 1024

# Far enough either side of the limit that the tests are about the policy
# and not about the exact byte the envelope adds.
MARGIN_BYTES = 4 * CHUNK_BYTES


class RecordingIssueService:
    """Returns a fixed page and remembers whether it was ever asked.

    The count is the whole point: a 413 proves the client got an error, and
    only this proves the error arrived before the query ran.
    """

    def __init__(self):
        self.calls = 0

    async def list(self, *, first: int, after: str | None) -> IssuePage:
        self.calls += 1

        return IssuePage(
            nodes=[make_entity(1)],
            has_next_page=False,
            end_cursor=None,
        )


class CountingStream:
    """A request body that records how much of itself the server pulled.

    httpx's ASGI transport draws chunks from here on demand, so bytes_read
    is what the application actually consumed rather than what the client
    was prepared to send.
    """

    def __init__(self, body: bytes):
        self._body = body
        self.bytes_read = 0

    async def __aiter__(self):
        for start in range(0, len(self._body), CHUNK_BYTES):
            chunk = self._body[start : start + CHUNK_BYTES]
            self.bytes_read += len(chunk)

            yield chunk


def graphql_body(size: int) -> bytes:
    """A GraphQL POST body of exactly `size` bytes.

    The padding rides in `variables` under a name the document never
    declares, which graphql-core ignores. So the request stays one the
    server would ordinarily execute, and a rejection is about its size and
    nothing else.
    """
    envelope = json.dumps({"query": ISSUES_QUERY, "variables": {"padding": ""}})
    padding = size - len(envelope.encode())

    if padding < 0:
        raise ValueError(f"{size} bytes is smaller than the query itself")

    return json.dumps(
        {"query": ISSUES_QUERY, "variables": {"padding": "x" * padding}}
    ).encode()


@pytest.fixture
def recording_service() -> RecordingIssueService:
    return RecordingIssueService()


@pytest.fixture
async def client(monkeypatch, tmp_path, recording_service):
    """The composed application, wired to a service instead of a database.

    ASGITransport never runs the lifespan, so no pool is created; the
    context override supplies what the real context factory would have
    borrowed from one.
    """
    use_environment(
        monkeypatch,
        tmp_path,
        DATABASE_URL=PLACEHOLDER_DSN,
        ENVIRONMENT="test",
    )

    application = create_app()
    application.dependency_overrides[get_context] = lambda: VectorContext(
        issue_service=recording_service
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application),
        base_url="http://vector.test",
    ) as client:
        yield client


async def test_a_body_under_the_limit_reaches_graphql(client, recording_service):
    """The limit has to leave ordinary traffic alone to be worth anything."""
    entity = make_entity(1)

    response = await client.post(
        "/graphql",
        content=graphql_body(MAX_REQUEST_BODY_BYTES - MARGIN_BYTES),
        headers=JSON_HEADERS,
    )

    assert response.status_code == 200
    assert response.json() == {
        "data": {"issues": {"nodes": [{"id": str(entity.id), "title": entity.title}]}}
    }
    assert recording_service.calls == 1


async def test_an_oversized_body_is_rejected_before_graphql_runs(
    client, recording_service
):
    response = await client.post(
        "/graphql",
        content=graphql_body(MAX_REQUEST_BODY_BYTES + MARGIN_BYTES),
        headers=JSON_HEADERS,
    )

    assert response.status_code == 413
    assert recording_service.calls == 0


async def test_a_body_declaring_an_oversized_length_is_never_read(
    client, recording_service
):
    """A refused request should cost the server nothing to refuse.

    Content-Length is a claim, but an oversized claim is still self-
    incriminating: there is no body it could describe that would be
    acceptable, so the answer is available before the first byte arrives.
    """
    body = graphql_body(MAX_REQUEST_BODY_BYTES + MARGIN_BYTES)
    stream = CountingStream(body)

    request = client.build_request(
        "POST",
        "/graphql",
        content=stream,
        headers=JSON_HEADERS | {"content-length": str(len(body))},
    )
    response = await client.send(request)

    assert response.status_code == 413
    assert stream.bytes_read == 0
    assert recording_service.calls == 0


async def test_an_oversized_body_without_a_content_length_is_rejected(
    client, recording_service
):
    """A chunked request declares no size, so the size must be counted.

    Trusting the header alone would make the limit optional: omit it and
    send whatever you like. Counting must also stop at the limit rather
    than draining the stream to find out how big it was, which is why the
    bytes read are bounded and not merely non-total.
    """
    body = graphql_body(MAX_REQUEST_BODY_BYTES + MARGIN_BYTES)
    stream = CountingStream(body)

    request = client.build_request(
        "POST", "/graphql", content=stream, headers=JSON_HEADERS
    )

    assert "content-length" not in request.headers
    assert request.headers["transfer-encoding"] == "chunked"

    response = await client.send(request)

    assert response.status_code == 413
    assert recording_service.calls == 0
    assert stream.bytes_read <= MAX_REQUEST_BODY_BYTES + CHUNK_BYTES
    assert stream.bytes_read < len(body)


async def test_an_oversized_body_understating_its_length_is_rejected(
    client, recording_service
):
    """The same counting has to catch a header that is present and false."""
    body = graphql_body(MAX_REQUEST_BODY_BYTES + MARGIN_BYTES)
    stream = CountingStream(body)

    request = client.build_request(
        "POST",
        "/graphql",
        content=stream,
        headers=JSON_HEADERS | {"content-length": "42"},
    )

    assert request.headers["content-length"] == "42"

    response = await client.send(request)

    assert response.status_code == 413
    assert recording_service.calls == 0
    assert stream.bytes_read <= MAX_REQUEST_BODY_BYTES + CHUNK_BYTES


async def test_the_limit_covers_routes_beyond_graphql(client):
    """The limit wraps the application, not the GraphQL mount.

    Scoped to /graphql it would protect today's only body-reading route and
    leave every route added after it unbounded by default. An unrouted path
    is the cheapest proof that the ceiling applies before routing decides
    anything: the same request under the limit gets the 404 it deserves.
    """
    oversized = await client.post(
        "/no-such-route",
        content=graphql_body(MAX_REQUEST_BODY_BYTES + MARGIN_BYTES),
        headers=JSON_HEADERS,
    )
    ordinary = await client.post("/no-such-route", content=b"{}", headers=JSON_HEADERS)

    assert oversized.status_code == 413
    assert ordinary.status_code == 404
