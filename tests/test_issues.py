"""Tests for issue validation and the issueCreate GraphQL contract."""

import pytest

from app.domain.errors import ValidationError
from app.services.issues import IssueService

from tests.conftest import ExplodingPool


async def test_empty_title_is_required(
    issue_service: IssueService,
    exploding_pool: ExplodingPool,
):
    with pytest.raises(ValidationError) as exc_info:
        await issue_service.create(title="", description=None, priority=2)

    issues = exc_info.value.issues

    assert len(issues) == 1
    assert issues[0].field == "title"
    assert issues[0].code == "REQUIRED"
    assert issues[0].message == "Title is required"

    # Invalid input must never reach the database.
    assert exploding_pool.acquire_count == 0


async def test_priority_out_of_range(
    issue_service: IssueService,
    exploding_pool: ExplodingPool,
):
    with pytest.raises(ValidationError) as exc_info:
        await issue_service.create(
            title="Valid title",
            description=None,
            priority=99,
        )

    issues = exc_info.value.issues

    assert len(issues) == 1
    assert issues[0].field == "priority"
    assert issues[0].code == "OUT_OF_RANGE"
    assert issues[0].message == "Priority must be between 0 and 4"

    assert exploding_pool.acquire_count == 0


async def test_negative_priority_out_of_range(issue_service: IssueService):
    with pytest.raises(ValidationError) as exc_info:
        await issue_service.create(
            title="Valid title",
            description=None,
            priority=-1,
        )

    issues = exc_info.value.issues

    assert len(issues) == 1
    assert issues[0].field == "priority"
    assert issues[0].code == "OUT_OF_RANGE"


async def test_multiple_failures_are_collected_in_order(
    issue_service: IssueService,
    exploding_pool: ExplodingPool,
):
    with pytest.raises(ValidationError) as exc_info:
        await issue_service.create(title="", description=None, priority=99)

    issues = exc_info.value.issues

    assert len(issues) == 2

    assert issues[0].field == "title"
    assert issues[0].code == "REQUIRED"

    assert issues[1].field == "priority"
    assert issues[1].code == "OUT_OF_RANGE"

    assert exploding_pool.acquire_count == 0


async def test_title_too_long(issue_service: IssueService):
    with pytest.raises(ValidationError) as exc_info:
        await issue_service.create(
            title="x" * 501,
            description=None,
            priority=2,
        )

    issues = exc_info.value.issues

    assert len(issues) == 1
    assert issues[0].field == "title"
    assert issues[0].code == "TOO_LONG"
    assert issues[0].message == "Title must be at most 500 characters"


async def test_title_at_max_length_is_valid(issue_service: IssueService):
    """500 characters is allowed; only 501 trips TOO_LONG."""
    with pytest.raises(AssertionError, match="must not be called"):
        # Validation passes, so the service proceeds to acquire a
        # connection -- which the exploding pool refuses. Reaching the
        # acquire is exactly the proof that validation accepted the input.
        await issue_service.create(
            title="x" * 500,
            description=None,
            priority=4,
        )


async def test_validation_error_message_is_generic():
    """The exception message must not be built from the field errors."""
    with pytest.raises(ValidationError) as exc_info:
        IssueService._validate_create(title="", priority=99)

    assert str(exc_info.value) == "Validation failed"
