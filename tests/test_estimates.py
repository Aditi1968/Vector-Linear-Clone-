"""Estimate scales: what 21 means, and which team gets to say.

migration 006 stored `issues.estimate INTEGER` with no unit and argued for the
refusal in writing -- "a limit the product wants is a product policy, enforced
where the product knows the team's unit". This is that policy, and the two
things worth testing about it are:

  * THE DEFAULT LEAVES EVERY EXISTING ROW MEANING WHAT IT MEANT. `none` admits
    every non-negative integer, so migration 029 needs no backfill and no row
    written before it becomes invalid;
  * A T-SHIRT SCALE CONSTRAINS RATHER THAN RENDERS. The stored integer is a
    position on a ladder, so 8 is not a large t-shirt -- it is not one at all,
    and the write has to be refused.

The vocabulary is pinned against the CHECK constraint by reading the migration,
for the reason tests/test_teams.py pins `WorkflowStateCategory`: a value the
enum knows and the schema does not is a write that fails at runtime, and the
reverse is a row the application cannot read back. Neither is visible from
either file alone.

No `db` mark: nothing here reaches PostgreSQL.
"""

import re
from pathlib import Path
from uuid import UUID

import pytest

from app.domain.errors import ValidationError
from app.domain.estimates import (
    ESTIMATE_MIN,
    TSHIRT_LABELS,
    EstimateScale,
    estimate_error_message,
    is_valid_estimate,
)
from app.domain.issues import IssuePatch
from app.domain.tenancy import WorkspaceScope
from app.repositories.issues import IssueRepository
from app.services.issues import IssueService

from tests.conftest import FakePool


MIGRATION_029 = (
    Path(__file__).resolve().parents[1] / "migrations" / "029_estimates_dates.sql"
).read_text(encoding="utf-8")

WORKSPACE_ID = UUID("00000000-0000-7000-8000-000000000001")
TEAM_ID = UUID("00000000-0000-7000-8000-000000000002")
ISSUE_ID = UUID("00000000-0000-7000-8000-000000000009")


def scales_in_migration() -> set[str]:
    """The values `teams_estimate_scale_known` admits, read out of the SQL."""
    match = re.search(
        r"teams_estimate_scale_known\s*\n?\s*CHECK \(estimate_scale IN \(([^)]*)\)\)",
        MIGRATION_029,
    )

    assert match is not None, "teams_estimate_scale_known not found in migration 029"

    return set(re.findall(r"'([a-z]+)'", match.group(1)))


# --- the vocabulary ---------------------------------------------------


def test_the_enum_and_the_check_constraint_admit_the_same_four_words():
    assert {scale.value for scale in EstimateScale} == scales_in_migration()


def test_the_column_defaults_to_the_scale_every_existing_row_is_already_in():
    """The whole backfill, and it is a word in a DEFAULT clause.

    Every estimate written before migration 029 was written under no scale, and
    NONE is defined as exactly that. A default of POINTS would have silently
    relabelled every number in the installation; a default of TSHIRT would have
    made most of them unwritable on their next edit.
    """
    assert "estimate_scale TEXT NOT NULL DEFAULT 'none'" in MIGRATION_029
    assert EstimateScale.NONE.value == "none"


# --- what each scale admits -------------------------------------------


@pytest.mark.parametrize(
    "scale",
    [EstimateScale.NONE, EstimateScale.POINTS, EstimateScale.HOURS],
)
@pytest.mark.parametrize("estimate", [0, 1, 21, 100, 10_000])
def test_the_unbounded_scales_take_any_whole_number(scale, estimate):
    """No ceiling, matching 006's refusal to guess one.

    21 is absurd in hours and ordinary in points, and 100 is ordinary in
    either. Pinning a Fibonacci ladder onto POINTS would additionally make a
    team that already estimates 4 and 6 unable to edit its own issues.
    """
    assert is_valid_estimate(scale, estimate)


@pytest.mark.parametrize("scale", list(EstimateScale))
def test_no_scale_takes_a_negative_number(scale):
    """`issues_estimate_non_negative` in 006 is the floor under every scale,
    including the ones that name no unit."""
    assert not is_valid_estimate(scale, -1)


@pytest.mark.parametrize("estimate", [1, 2, 3, 4, 5])
def test_a_tshirt_scale_takes_exactly_its_five_rungs(estimate):
    assert is_valid_estimate(EstimateScale.TSHIRT, estimate)


@pytest.mark.parametrize("estimate", [0, 6, 8, 21])
def test_a_tshirt_scale_refuses_anything_off_the_ladder(estimate):
    """THE ASSERTION THE FEATURE EXISTS FOR.

    8 is the number somebody types when their last team estimated in points,
    and under a scale that only renders rather than constrains it would be
    stored and drawn as a t-shirt size nobody can name.

    Zero is refused too, and it is the one place this disagrees with
    `ESTIMATE_MIN`: "we looked at this and it is free" is a QUANTITY, and a
    ladder of sizes has no rung for it. A team that wants to record free work
    is not on a t-shirt scale.
    """
    assert not is_valid_estimate(EstimateScale.TSHIRT, estimate)


def test_the_ladders_length_is_read_from_the_labels_and_not_written_twice():
    """The valid range falls out of `len(TSHIRT_LABELS)`, so adding XXL is one
    edit rather than two that can disagree."""
    assert is_valid_estimate(EstimateScale.TSHIRT, len(TSHIRT_LABELS))
    assert not is_valid_estimate(EstimateScale.TSHIRT, len(TSHIRT_LABELS) + 1)


# --- what a person is told --------------------------------------------


def test_a_tshirt_refusal_names_the_ladder_rather_than_saying_invalid():
    """Somebody told "invalid estimate" has to discover that the setting
    exists; somebody told the five names can act on it immediately."""
    message = estimate_error_message(EstimateScale.TSHIRT)

    assert f"1-{len(TSHIRT_LABELS)}" in message

    for label in TSHIRT_LABELS:
        assert label in message


@pytest.mark.parametrize(
    "scale",
    [EstimateScale.NONE, EstimateScale.POINTS, EstimateScale.HOURS],
)
def test_an_unbounded_scale_reports_the_only_bound_it_has(scale):
    assert (
        estimate_error_message(scale) == f"Estimate must be {ESTIMATE_MIN} or greater"
    )


@pytest.mark.parametrize("scale", list(EstimateScale))
def test_every_scale_has_a_message(scale):
    """A scale added to the enum without one would render as an empty error
    beside a field the person cannot correct."""
    assert estimate_error_message(scale)


# --- the service, where the rule is actually enforced -----------------
#
# The rule relates two tables -- an issue's estimate and its team's scale -- so
# it cannot be a CHECK constraint, and this project has deliberately not taken
# up triggers (see `IssueService`'s note on the completed_at rule, which is the
# same shape for the same reason). The write path is therefore the enforcement,
# and these are the tests that say it runs on both halves of it.


class ScaleReadingTeams:
    """The team service's estimate-scale read, recorded."""

    def __init__(self, scale: EstimateScale | None):
        self.scale = scale
        self.calls: list[dict] = []

    async def estimate_scale(self, connection, *, scope, team_id):
        self.calls.append({"scope": scope, "team_id": team_id})

        return self.scale

    async def default_workflow_state_id(self, connection, *, scope, team_id):
        raise AssertionError(
            "an estimate the team's scale refuses must be caught before the "
            "transaction that allocates a number opens"
        )

    async def allocate_issue_number(self, connection, *, scope, team_id):
        raise AssertionError("unreachable: the create should have been refused")


class ScaleReadingIssues:
    """The issue repository's estimate-scale read, recorded."""

    def __init__(self, scale: EstimateScale | None):
        self.scale = scale
        self.calls: list[dict] = []

    async def find_estimate_scale(self, connection, *, scope, issue_id):
        self.calls.append({"scope": scope, "issue_id": issue_id})

        return self.scale

    async def lock_snapshot(self, connection, *, scope, issue_id):
        raise AssertionError(
            "an estimate the team's scale refuses must be caught before the "
            "transaction that locks the row opens"
        )


def issue_service_with(*, teams=None, repository=None):
    return IssueService(
        pool=FakePool(),
        repository=repository if repository is not None else IssueRepository(),
        teams=teams,
    )


async def test_a_create_checks_the_estimate_against_the_teams_own_scale():
    teams = ScaleReadingTeams(EstimateScale.TSHIRT)
    scope = WorkspaceScope(workspace_id=WORKSPACE_ID)

    with pytest.raises(ValidationError) as raised:
        await issue_service_with(teams=teams).create(
            scope=scope,
            team_id=TEAM_ID,
            title="Rebuild the importer",
            estimate=8,
        )

    assert [(issue.field, issue.code) for issue in raised.value.issues] == [
        ("estimate", "OUT_OF_RANGE")
    ]
    assert teams.calls == [{"scope": scope, "team_id": TEAM_ID}]


async def test_an_update_checks_it_against_the_scale_of_the_issues_own_team():
    """The team is not in the patch, so the lookup goes through the ISSUE.

    That is the whole reason `IssueRepository.find_estimate_scale` exists: an
    update carries an issue id and nothing else, and the scale that governs it
    belongs to whichever team the database says owns that issue.
    """
    repository = ScaleReadingIssues(EstimateScale.TSHIRT)
    scope = WorkspaceScope(workspace_id=WORKSPACE_ID)

    with pytest.raises(ValidationError):
        await issue_service_with(teams=None, repository=repository).update(
            scope=scope,
            issue_id=ISSUE_ID,
            patch=IssuePatch(estimate=8),
        )

    assert repository.calls == [{"scope": scope, "issue_id": ISSUE_ID}]


async def test_clearing_an_estimate_needs_no_scale_and_costs_no_round_trip():
    """Every scale admits an issue that is not sized, which is what
    `estimate IS NULL` has meant since 006 -- and an ordinary title edit must
    not pay for a lookup it has no use for."""
    repository = ScaleReadingIssues(EstimateScale.TSHIRT)

    # `lock_snapshot` raises, which is how this fake says the service got past
    # the scale check -- the point being that `find_estimate_scale` was never
    # called on the way.
    with pytest.raises(AssertionError):
        await issue_service_with(teams=None, repository=repository).update(
            scope=WorkspaceScope(workspace_id=WORKSPACE_ID),
            issue_id=ISSUE_ID,
            patch=IssuePatch(estimate=None),
        )

    assert repository.calls == []


async def test_an_issue_that_is_not_here_is_not_reported_as_a_bad_estimate():
    """A scale of None is the issue being absent -- nonexistent, another
    tenant's, or archived -- and raising a different error for a bad estimate on
    an unreachable issue would be an oracle for which ids exist."""
    repository = ScaleReadingIssues(None)

    # Past the check, into `lock_snapshot`, which is the fake's way of saying
    # the estimate was not refused.
    with pytest.raises(AssertionError):
        await issue_service_with(teams=None, repository=repository).update(
            scope=WorkspaceScope(workspace_id=WORKSPACE_ID),
            issue_id=ISSUE_ID,
            patch=IssuePatch(estimate=8),
        )
