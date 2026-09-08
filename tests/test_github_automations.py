"""What a pull request does to an issue, and what it must refuse to do.

No database, no Docker, no network. The fake repository in tests/test_github.py
stands in for the statements; tests/test_github_automations_db.py runs the real
ones -- including the forward-only UPDATE, which is the piece a fake can only
imitate -- against a real schema.

Five properties, and four of them are refusals:

  * a DRAFT pull request moves nothing. An issue that jumped to In Progress
    because somebody pushed a work-in-progress branch is the bug report this
    feature would otherwise generate;
  * a pull request CLOSED WITHOUT MERGING is not a completion, and does not
    move the issue back either;
  * a workspace that has configured nothing gets nothing. The absence of a
    configuration is a refusal to guess, not a licence to pick a state;
  * a person who has moved the issue on is not overridden, and a redelivery
    does not move it twice;
  * and the one that is not a refusal: an opened pull request starts the
    issue and a merged one completes it, with an activity row that says which
    pull request did it.
"""

from uuid import UUID

import pytest

from app.domain.errors import GithubRepositoriesInUseError, ValidationError
from app.domain.github import (
    GithubAutomationEntity,
    GithubRepositoryEntity,
    automated_move_for,
    categories_below,
    pull_request_cause,
)
from app.domain.tenancy import WorkspaceScope

from tests.test_github import WORKSPACE_ID, build_service, make_scope
from tests.test_github_development import (
    OUR_ISSUE_ID,
    PULL_NUMBER,
    REPOSITORY_ID,
    SECOND_ISSUE_ID,
    development_service,
    ours,
    pull_payload,
)


SCOPE = WorkspaceScope(workspace_id=WORKSPACE_ID)

TEAM_ID = UUID("00000000-0000-7000-8000-0000000000d1")
OTHER_TEAM_ID = UUID("00000000-0000-7000-8000-0000000000d2")

BACKLOG_STATE = UUID("00000000-0000-7000-8000-0000000000f0")
TODO_STATE = UUID("00000000-0000-7000-8000-0000000000f1")
IN_PROGRESS_STATE = UUID("00000000-0000-7000-8000-0000000000f2")
IN_REVIEW_STATE = UUID("00000000-0000-7000-8000-0000000000f3")
DONE_STATE = UUID("00000000-0000-7000-8000-0000000000f4")
CANCELED_STATE = UUID("00000000-0000-7000-8000-0000000000f5")

# 005's own seed, in its own order, plus a second `started` state -- which is
# the case the whole "which one?" question is about and which no default can
# answer on a team's behalf.
BOARD = {
    BACKLOG_STATE: (WORKSPACE_ID, TEAM_ID, "backlog", 0),
    TODO_STATE: (WORKSPACE_ID, TEAM_ID, "unstarted", 1),
    IN_PROGRESS_STATE: (WORKSPACE_ID, TEAM_ID, "started", 2),
    IN_REVIEW_STATE: (WORKSPACE_ID, TEAM_ID, "started", 3),
    DONE_STATE: (WORKSPACE_ID, TEAM_ID, "completed", 4),
    CANCELED_STATE: (WORKSPACE_ID, TEAM_ID, "canceled", 5),
}


# --- the rule, as a pure function ---------------------------------------


@pytest.mark.parametrize(
    ("action", "display_state", "expected"),
    [
        # The two that fire.
        ("opened", "open", "started"),
        ("reopened", "open", "started"),
        ("ready_for_review", "open", "started"),
        ("closed", "merged", "completed"),
        # A draft is open on GitHub and is not ready by its author's own
        # statement. Marking it ready is what fires.
        ("opened", "draft", None),
        ("ready_for_review", "draft", None),
        # Closed without merging: an abandoned attempt is not shipped work,
        # and the issue does not move back either.
        ("closed", "closed", None),
        ("converted_to_draft", "draft", None),
        # Everything that RESTATES an open pull request. GitHub sends the
        # whole object on each of these, under a delivery id nothing has seen,
        # so a rule keyed on "the pull request is open" would re-assert the
        # move on every push.
        ("synchronize", "open", None),
        ("edited", "open", None),
        ("labeled", "open", None),
        ("assigned", "open", None),
        # And on an already-merged pull request, which GitHub also re-sends.
        ("edited", "merged", None),
        ("synchronize", "merged", None),
        # A payload with no action, or a non-string one.
        (None, "open", None),
        (17, "merged", None),
    ],
)
def test_which_deliveries_move_an_issue(action, display_state, expected):
    assert automated_move_for(action=action, display_state=display_state) == expected


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("started", ("backlog", "unstarted")),
        ("completed", ("backlog", "unstarted", "started")),
        # Never a source. A person who cancels an issue has made a statement
        # about the work, and a branch that merges afterwards must not reopen
        # the question.
        ("canceled", ()),
        ("nonsense", ()),
    ],
)
def test_an_automation_only_moves_an_issue_forward(target, expected):
    assert categories_below(target) == expected


def test_a_canceled_issue_is_never_a_source_for_any_move():
    """Stated as its own assertion because it is the rule most likely to be
    lost the day somebody adds a category to the ladder."""
    assert not any("canceled" in categories_below(target) for target in BOARD)


@pytest.mark.parametrize(
    ("full_name", "expected"),
    [
        ("acme/vector", "github_pull_request:acme/vector#84"),
        # A payload with no usable repository name still names the pull
        # request, which beats a history row that says nobody did it.
        (None, "github_pull_request:#84"),
        ("", "github_pull_request:#84"),
    ],
)
def test_the_history_says_which_pull_request_moved_the_issue(full_name, expected):
    assert pull_request_cause(full_name, 84) == expected


# --- the service, over a fake repository --------------------------------


def automated_service(
    *,
    started=IN_PROGRESS_STATE,
    completed=DONE_STATE,
    placed=TODO_STATE,
    issues=None,
    **kwargs,
):
    """A confirmed installation, a configured team, and one issue on it."""
    service, repository = development_service(
        issues=issues if issues is not None else ours(),
        **kwargs,
    )

    repository.states = dict(BOARD)
    repository.issue_states = {OUR_ISSUE_ID: (WORKSPACE_ID, TEAM_ID, placed)}

    if started is not None or completed is not None:
        repository.automations = {
            (WORKSPACE_ID, TEAM_ID): GithubAutomationEntity(
                team_id=TEAM_ID,
                started_state_id=started,
                completed_state_id=completed,
            )
        }

    return service, repository


def state_of(repository, issue_id=OUR_ISSUE_ID):
    return repository.issue_states[issue_id][2]


async def deliver(service, payload, delivery_id="d-1"):
    await service.apply_webhook(
        event="pull_request",
        payload=payload,
        delivery_id=delivery_id,
    )


async def test_an_opened_pull_request_starts_the_issue_it_names():
    service, repository = automated_service()

    await deliver(service, pull_payload(title="Fix ENG-142"))

    assert state_of(repository) == IN_PROGRESS_STATE


async def test_a_merged_pull_request_completes_the_issue_it_names():
    service, repository = automated_service(placed=IN_PROGRESS_STATE)

    await deliver(
        service,
        pull_payload(
            title="Fix ENG-142",
            action="closed",
            state="closed",
            merged_at="2026-04-01T11:00:00Z",
        ),
    )

    assert state_of(repository) == DONE_STATE


async def test_a_draft_pull_request_moves_nothing():
    """The one an admin would file a bug about. A draft is the author saying
    the work is not ready; the product must not disagree with them."""
    service, repository = automated_service()

    await deliver(service, pull_payload(title="Fix ENG-142", draft=True))

    assert state_of(repository) == TODO_STATE


async def test_marking_a_draft_ready_for_review_starts_the_issue():
    """The other half of the draft rule: the move is deferred, not dropped."""
    service, repository = automated_service()

    await deliver(service, pull_payload(title="Fix ENG-142", draft=True))
    await deliver(
        service,
        pull_payload(title="Fix ENG-142", action="ready_for_review"),
        delivery_id="d-2",
    )

    assert state_of(repository) == IN_PROGRESS_STATE


async def test_a_pull_request_closed_without_merging_is_not_a_completion():
    """`state`, `draft` and `merged_at` are stored separately in 017 precisely
    so this is answerable, and this is the answer."""
    service, repository = automated_service(placed=IN_PROGRESS_STATE)

    await deliver(
        service,
        pull_payload(
            title="Fix ENG-142",
            action="closed",
            state="closed",
            merged_at=None,
        ),
    )

    assert state_of(repository) == IN_PROGRESS_STATE


async def test_a_team_with_no_configuration_gets_no_movement():
    """The refusal to guess. There is no global "In Progress" to fall back
    on, and picking one would be choosing a state on a workspace's behalf."""
    service, repository = automated_service(started=None, completed=None)

    await deliver(service, pull_payload(title="Fix ENG-142"))

    assert state_of(repository) == TODO_STATE


async def test_a_team_that_automated_only_the_merge_is_not_started_by_an_open():
    service, repository = automated_service(started=None)

    await deliver(service, pull_payload(title="Fix ENG-142"))

    assert state_of(repository) == TODO_STATE


async def test_a_second_delivery_of_the_same_pull_request_moves_nothing_again():
    """Idempotency without a dedupe table.

    Two OPEN deliveries for one pull request, under two delivery ids
    `github_deliveries` has never seen -- which is what a reopen after a
    close, or a provider replay, actually looks like. The first moves the
    issue; the second finds it already at the target and writes nothing, so
    there is no second activity row and no second notification.
    """
    service, repository = automated_service()

    await deliver(service, pull_payload(title="Fix ENG-142"))
    await deliver(
        service,
        pull_payload(title="Fix ENG-142", action="reopened"),
        delivery_id="d-2",
    )

    moves = [
        call for call in repository.calls if call[0] == "move_issues_for_automation"
    ]

    assert len(moves) == 2
    assert state_of(repository) == IN_PROGRESS_STATE


async def test_a_person_who_moved_the_issue_on_is_not_overridden():
    """A human moved ENG-142 into In Review, which is a SECOND started state.

    A rule that re-asserted the configured state on every delivery would drag
    it back to In Progress and keep doing so on every push. Forward-only is
    what stops that: `started` is not below `started`.
    """
    service, repository = automated_service(placed=IN_REVIEW_STATE)

    await deliver(service, pull_payload(title="Fix ENG-142", action="reopened"))

    assert state_of(repository) == IN_REVIEW_STATE


async def test_a_canceled_issue_is_not_completed_by_a_merge():
    """A branch outlives the decision to drop the work it was for."""
    service, repository = automated_service(placed=CANCELED_STATE)

    await deliver(
        service,
        pull_payload(
            title="Fix ENG-142",
            action="closed",
            state="closed",
            merged_at="2026-04-01T11:00:00Z",
        ),
    )

    assert state_of(repository) == CANCELED_STATE


async def test_an_untracked_repository_moves_nothing_and_stores_nothing():
    """Repository selection is one predicate in `repository_exists`, and both
    development events pass through it before writing anything."""
    service, repository = automated_service(
        repositories=[
            GithubRepositoryEntity(
                repository_id=REPOSITORY_ID,
                full_name="acme/vector",
                tracked=False,
            )
        ],
    )

    await deliver(service, pull_payload(title="Fix ENG-142"))

    assert state_of(repository) == TODO_STATE
    assert repository.pull_requests == {}


async def test_a_pull_request_naming_two_issues_moves_both():
    service, repository = automated_service(
        issues=ours() | ours(number=143, issue_id=SECOND_ISSUE_ID),
    )
    repository.issue_states[SECOND_ISSUE_ID] = (WORKSPACE_ID, TEAM_ID, TODO_STATE)

    await deliver(service, pull_payload(title="Fix ENG-142 and ENG-143"))

    assert state_of(repository) == IN_PROGRESS_STATE
    assert state_of(repository, SECOND_ISSUE_ID) == IN_PROGRESS_STATE


async def test_an_issue_on_a_team_with_no_automation_is_left_alone():
    """Two teams, one configured, one not. A pull request naming both moves
    only the issue whose team asked for it."""
    service, repository = automated_service(
        issues=ours() | ours(team_key="WEB", number=3, issue_id=SECOND_ISSUE_ID),
    )
    repository.issue_states[SECOND_ISSUE_ID] = (
        WORKSPACE_ID,
        OTHER_TEAM_ID,
        TODO_STATE,
    )

    await deliver(service, pull_payload(title="Fix ENG-142 and WEB-3"))

    assert state_of(repository) == IN_PROGRESS_STATE
    assert state_of(repository, SECOND_ISSUE_ID) == TODO_STATE


# --- configuring it -----------------------------------------------------


def configuring_service():
    service, repository = build_service(workspace_id=WORKSPACE_ID)
    repository.states = dict(BOARD)

    return service, repository


async def test_turning_it_on_without_naming_states_stores_the_board_defaults():
    """The default derived from category, resolved ONCE and stored explicitly.

    The team has two `started` states; the first by board order is chosen, and
    what gets written is that id -- so adding a third tomorrow does not
    silently change what the automation does, and the settings screen shows a
    named state rather than a rule.
    """
    service, repository = configuring_service()

    await service.set_issue_automation(make_scope(), team_id=TEAM_ID, enabled=True)

    stored = repository.automations[(WORKSPACE_ID, TEAM_ID)]

    assert stored.started_state_id == IN_PROGRESS_STATE
    assert stored.completed_state_id == DONE_STATE


async def test_a_named_state_of_the_wrong_category_is_a_field_error():
    """A started slot pointing at a backlog state would move issues BACKWARDS
    the first time somebody opened a pull request."""
    service, _ = configuring_service()

    with pytest.raises(ValidationError) as raised:
        await service.set_issue_automation(
            make_scope(),
            team_id=TEAM_ID,
            enabled=True,
            started_state_id=BACKLOG_STATE,
        )

    assert [issue.field for issue in raised.value.issues] == ["startedStateId"]


async def test_a_state_belonging_to_another_team_answers_exactly_as_a_typo():
    """Telling the two apart would confirm that a guessed id names a real
    state somewhere, which is not this caller's business."""
    service, repository = configuring_service()
    repository.states[IN_PROGRESS_STATE] = (WORKSPACE_ID, OTHER_TEAM_ID, "started", 2)

    with pytest.raises(ValidationError) as raised:
        await service.set_issue_automation(
            make_scope(),
            team_id=TEAM_ID,
            enabled=True,
            started_state_id=IN_PROGRESS_STATE,
        )

    assert [issue.code for issue in raised.value.issues] == ["INVALID_STATE"]


async def test_naming_only_one_state_leaves_the_other_half_doing_nothing():
    service, repository = configuring_service()

    await service.set_issue_automation(
        make_scope(),
        team_id=TEAM_ID,
        enabled=True,
        completed_state_id=DONE_STATE,
    )

    stored = repository.automations[(WORKSPACE_ID, TEAM_ID)]

    assert stored.started_state_id is None
    assert stored.completed_state_id == DONE_STATE


async def test_turning_it_off_removes_the_configuration():
    """Off is the absence of a row, so this is a delete. Idempotent."""
    service, repository = configuring_service()

    await service.set_issue_automation(make_scope(), team_id=TEAM_ID, enabled=True)
    await service.set_issue_automation(make_scope(), team_id=TEAM_ID, enabled=False)
    await service.set_issue_automation(make_scope(), team_id=TEAM_ID, enabled=False)

    assert repository.automations == {}


async def test_a_team_with_no_started_or_completed_state_is_told_so():
    service, repository = configuring_service()
    repository.states = {BACKLOG_STATE: (WORKSPACE_ID, TEAM_ID, "backlog", 0)}

    with pytest.raises(ValidationError) as raised:
        await service.set_issue_automation(make_scope(), team_id=TEAM_ID, enabled=True)

    assert [issue.code for issue in raised.value.issues] == ["NO_DEFAULT_STATES"]


@pytest.mark.parametrize("role", ["member", "guest"])
async def test_only_an_admin_may_configure_an_automation(role):
    from app.domain.errors import WorkspaceAccessDeniedError

    service, _ = configuring_service()

    with pytest.raises(WorkspaceAccessDeniedError):
        await service.set_issue_automation(
            make_scope(role=role), team_id=TEAM_ID, enabled=True
        )


# --- choosing repositories ----------------------------------------------


def tracking_service(released=()):
    service, repository = build_service(
        workspace_id=WORKSPACE_ID,
        installation=None,
        repositories=[
            GithubRepositoryEntity(repository_id=1, full_name="acme/vector"),
            GithubRepositoryEntity(repository_id=2, full_name="acme/docs"),
        ],
    )
    repository.released = set(released)

    return service, repository


async def test_choosing_repositories_narrows_the_set_that_is_applied():
    service, repository = tracking_service()

    await service.set_tracked_repositories(make_scope(), repository_ids=[1])

    assert {one.repository_id: one.tracked for one in repository.repositories} == {
        1: True,
        2: False,
    }


async def test_an_empty_selection_tracks_nothing_rather_than_everything():
    """A connected workspace that wants no development activity yet is a real
    state, and it must not read as "the client forgot the field"."""
    service, repository = tracking_service()

    await service.set_tracked_repositories(make_scope(), repository_ids=[])

    assert not any(one.tracked for one in repository.repositories)


async def test_untracking_a_repository_a_release_names_is_refused():
    """The same refusal `disconnect` earns from `releases_repository_fk`.

    Untracking is an UPDATE, so RESTRICT never fires -- which is exactly why
    the rule is checked here rather than left to a schema that cannot see it.
    """
    service, repository = tracking_service(released=(2,))

    with pytest.raises(GithubRepositoriesInUseError):
        await service.set_tracked_repositories(make_scope(), repository_ids=[1])

    assert all(one.tracked for one in repository.repositories)


async def test_a_repository_a_release_names_may_still_be_kept():
    """The refusal is about DROPPING one, not about touching the setting."""
    service, repository = tracking_service(released=(2,))

    await service.set_tracked_repositories(make_scope(), repository_ids=[1, 2])

    assert all(one.tracked for one in repository.repositories)


@pytest.mark.parametrize("role", ["member", "guest"])
async def test_only_an_admin_may_choose_repositories(role):
    from app.domain.errors import WorkspaceAccessDeniedError

    service, _ = tracking_service()

    with pytest.raises(WorkspaceAccessDeniedError):
        await service.set_tracked_repositories(
            make_scope(role=role), repository_ids=[1]
        )


def test_the_pull_number_constant_is_the_one_the_cause_string_carries():
    """Guards the fixture rather than the code: every assertion above about
    `github_pull_request:...#84` is only meaningful while the payload really
    is #84."""
    assert PULL_NUMBER == 84
    assert SCOPE.workspace_id == WORKSPACE_ID
