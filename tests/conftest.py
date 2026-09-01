"""Shared pytest fixtures."""

from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest

from app.domain.issues import IssueEntity
from app.repositories.issues import IssueRepository
from app.services.issues import IssueService


BASE_TIME = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


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
    """Records every query issued against it and replays canned rows."""

    def __init__(self, rows=None):
        self.rows = rows if rows is not None else []
        self.queries: list[dict] = []

    async def fetch(self, query, *args):
        self.queries.append({"query": query, "args": args})

        return self.rows


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
