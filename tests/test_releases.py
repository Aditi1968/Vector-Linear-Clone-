"""The releases API, its validation, and the note renderer -- without a database.

Four things are pinned here, and the third is the one this feature exists for:

1. That the release fields are actually ON the schema's root types. The root is
   composed in `app.graphql.schema` with `merge_types`, and a second
   `class Query(...)` written anywhere below that call does not conflict with
   it -- it silently rebinds the name, so every field belonging to the classes
   that definition does not inherit from vanishes from the API with the whole
   suite still green. That has happened in this repository once. These ask.

2. That the two GraphQL enums, the two domain tuples and the two CHECK
   constraints have not drifted apart. `app.graphql.types.release` already
   raises at import if the first pairs disagree; this is what makes that guard
   visible as a test rather than as an ImportError in an unrelated file. The
   CHECK half is tests/test_migration_024_db.py, which reads the constraints
   back out of the catalog.

3. That `render_release_notes` is DETERMINISTIC. Same inputs, same bytes --
   including when the inputs arrive in a different order, which is the way a
   renderer stops being deterministic without anybody noticing: it starts
   depending on one SQL statement's ORDER BY, and then on a query plan.

4. That validation refuses bad input before a connection is acquired. The pool
   used here raises on `acquire()`, so a rule that reached the database would
   fail rather than pass quietly.

The behaviour only a server can decide -- what the composite keys admit, which
issues a commit range actually resolves to, whether a status move is atomic --
is in tests/test_migration_024_db.py.
"""

import random
from uuid import UUID

import pytest

from app.domain.errors import ValidationError
from app.domain.releases import (
    DEFAULT_RELEASE_STATUS,
    ENVIRONMENT_KINDS,
    NO_CHANGES,
    NOTE_TITLE_MAX_LENGTH,
    RANGE_LIMIT,
    RELEASE_STATUSES,
    RELEASE_TRANSITIONS,
    RELEASE_TRANSITIONS_FROM,
    TRUNCATED,
    ReleaseIssueRef,
    ReleasePullRequestRef,
    render_release_notes,
)
from app.graphql.schema import build_schema
from app.graphql.types.release import EnvironmentKindType, ReleaseStatusType
from app.repositories.releases import ReleaseRepository
from app.services.releases import (
    ENVIRONMENT_NAME_MAX_LENGTH,
    NAME_MAX_LENGTH,
    ReleaseService,
)

from tests.conftest import TEST_SCOPE, ExplodingPool


ENVIRONMENT_ID = UUID("00000000-0000-7000-8000-0000000000c1")
RELEASE_ID = UUID("00000000-0000-7000-8000-0000000000c2")

REPOSITORY_ID = 11111

# 40 lowercase hex characters, which is what releases_commit_sha_format wants.
HEAD_SHA = "a39fd12" + "0" * 33
BASE_SHA = "b47ce03" + "1" * 33


@pytest.fixture
def schema():
    return build_schema("test")


@pytest.fixture
def service(exploding_pool: ExplodingPool) -> ReleaseService:
    """The real service over a pool that refuses to open a connection."""
    return ReleaseService(pool=exploding_pool, repository=ReleaseRepository())


def root_fields(schema, type_name: str) -> set[str]:
    """The field names the built schema exposes on one type.

    Read off the schema object rather than out of the SDL text, so this answers
    what the server will actually resolve.
    """
    return set(schema._schema.type_map[type_name].fields)


def issues(raised) -> list[tuple[str, str]]:
    return [(issue.field, issue.code) for issue in raised.value.issues]


def issue_ref(key: str, number: int, title: str = "A change") -> ReleaseIssueRef:
    return ReleaseIssueRef(
        issue_id=UUID(int=number),
        team_key=key,
        number=number,
        title=title,
    )


def pull_ref(number: int, title: str = "A pull request") -> ReleasePullRequestRef:
    return ReleasePullRequestRef(
        repository_id=REPOSITORY_ID,
        number=number,
        title=title,
        url=f"https://github.example/acme/vector/pull/{number}",
    )


def notes_for(
    issues_, pulls, *, previous: str | None = BASE_SHA, truncated: bool = False
) -> str:
    return render_release_notes(
        name="v1.4.0",
        commit_sha=HEAD_SHA,
        previous_commit_sha=previous,
        issues=tuple(issues_),
        pull_requests=tuple(pulls),
        truncated=truncated,
    )


# ------------------------------------------------- the root types are composed


def test_the_release_queries_reach_the_root(schema):
    """All three read fields are on Query, alongside every other feature's.

    `issues` and `me` are asserted here too, and deliberately. The failure this
    test exists for is a second root definition shadowing the merged one, and
    that failure removes OTHER features' fields rather than this one's -- so a
    test that only looked for `releases` would pass against the exact breakage
    it was written to catch.
    """
    fields = root_fields(schema, "Query")

    assert {"release", "releases", "environments"} <= fields
    assert {"issue", "issues", "me", "initiative", "project"} <= fields


def test_the_release_mutations_reach_the_root(schema):
    fields = root_fields(schema, "Mutation")

    assert {
        "environmentCreate",
        "releaseCreate",
        "releaseStatusSet",
        "releaseDelete",
    } <= fields

    # The same argument as above: a shadowed root takes these with it.
    assert {"issueCreate", "initiativeCreate", "login"} <= fields


def test_the_release_type_publishes_its_notes_and_its_edges_as_ids(schema):
    """`notes` is a field, and what shipped is ids rather than objects.

    `notes` being on the type is the whole feature's output. `issueIds` and
    `pullRequestNumbers` rather than `issues` / `pullRequests` follows
    `Initiative.projectIds`: a field returning ids says so, and it keeps
    `releases(first: 100) { ... }` from fanning out into a hundred issue reads
    that app/graphql/limits.py prices as one.
    """
    fields = root_fields(schema, "Release")

    assert {"notes", "issueIds", "pullRequestNumbers", "commitSha"} <= fields
    assert "issues" not in fields
    assert "pullRequests" not in fields


def test_the_release_type_carries_no_workspace(schema):
    """A release is only ever read through a scope the caller already holds.

    A `workspaceId` on the payload is the value a client eventually sends back
    as the scope for the next call, which is how a tenant boundary stops being
    an argument the server supplies and becomes one it can launder.
    """
    assert "workspaceId" not in root_fields(schema, "Release")


# ------------------------------------------------------- vocabularies agree


def test_the_status_enum_and_the_domain_tuple_agree():
    """app.graphql.types.release raises at import if these drift; this is what
    makes that guard a named failure instead of an ImportError somewhere
    else."""
    assert tuple(m.value for m in ReleaseStatusType) == RELEASE_STATUSES


def test_the_environment_kind_enum_and_the_domain_tuple_agree():
    assert tuple(m.value for m in EnvironmentKindType) == ENVIRONMENT_KINDS


def test_every_transition_names_statuses_that_exist():
    """A typo in the transition table is a move that can never be made, or one
    that can never be refused -- and neither shows up as a failure anywhere
    else, because the database holds the vocabulary and not the moves."""
    assert set(RELEASE_TRANSITIONS) == set(RELEASE_STATUSES)

    for targets in RELEASE_TRANSITIONS.values():
        assert set(targets) <= set(RELEASE_STATUSES)


def test_the_reverse_transition_table_is_the_forward_one_inverted():
    """`RELEASE_TRANSITIONS_FROM` is derived, and this pins the derivation.

    It is what `ReleaseRepository.set_status` puts in the statement, so an
    inversion that dropped an entry would silently make a legal move
    impossible -- reported to the client as INVALID_TRANSITION, which reads
    like a product rule rather than a bug.
    """
    for source, targets in RELEASE_TRANSITIONS.items():
        for target in targets:
            assert source in RELEASE_TRANSITIONS_FROM[target]

    for target, sources in RELEASE_TRANSITIONS_FROM.items():
        for source in sources:
            assert target in RELEASE_TRANSITIONS[source]


def test_a_release_starts_pending_and_nothing_may_return_to_that_state():
    """The default is the only way into `pending`, which is what makes
    `deployed_at` a one-way stamp: no move re-enters a state whose CHECK
    requires the instant to be absent."""
    assert DEFAULT_RELEASE_STATUS == "pending"
    assert RELEASE_TRANSITIONS_FROM["pending"] == ()


def test_failed_and_rolled_back_are_terminal():
    """Retrying a deploy is a NEW release. Reusing the failed row would
    overwrite the first attempt's timestamp, which is the one thing an incident
    review is looking for."""
    assert RELEASE_TRANSITIONS["failed"] == ()
    assert RELEASE_TRANSITIONS["rolled_back"] == ()


# ----------------------------------------------------- the note is deterministic


def test_the_same_inputs_render_the_same_bytes():
    """The headline guarantee, stated at its simplest."""
    first = notes_for([issue_ref("ENG", 9)], [pull_ref(84)])
    second = notes_for([issue_ref("ENG", 9)], [pull_ref(84)])

    assert first == second


def test_the_order_the_inputs_arrive_in_does_not_change_the_note():
    """The way determinism is actually lost.

    A renderer that trusted its caller's order would produce identical bytes in
    every test that built its list the same way, and different bytes the day a
    query plan changed or a second caller assembled the same set differently.
    Shuffling here is what makes the sort a property of the FUNCTION.
    """
    shipped = [issue_ref("ENG", n) for n in (1, 9, 10, 142)] + [
        issue_ref("OPS", n) for n in (3, 4)
    ]
    merged = [pull_ref(n) for n in (7, 84, 91)]

    expected = notes_for(shipped, merged)

    for seed in range(20):
        shuffled_issues = list(shipped)
        shuffled_pulls = list(merged)
        random.Random(seed).shuffle(shuffled_issues)
        random.Random(seed).shuffle(shuffled_pulls)

        assert notes_for(shuffled_issues, shuffled_pulls) == expected


def test_issues_are_ordered_numerically_within_a_team_not_lexically():
    """`ENG-9` before `ENG-10`, which sorting the rendered identifier would get
    backwards. Stable either way -- but a document a human reads has one right
    order, and this is why `ReleaseIssueRef` carries the key and the number
    instead of the identifier."""
    rendered = notes_for([issue_ref("ENG", 10), issue_ref("ENG", 9)], [])

    assert rendered.index("ENG-9 ") < rendered.index("ENG-10 ")


def test_teams_are_grouped_by_key_before_number():
    rendered = notes_for(
        [issue_ref("OPS", 1), issue_ref("ENG", 2), issue_ref("ENG", 1)], []
    )
    listed = [line for line in rendered.splitlines() if line.startswith("- ")]

    assert listed == ["- ENG-1 A change", "- ENG-2 A change", "- OPS-1 A change"]


def test_a_range_that_touched_nothing_still_renders_a_document():
    """Total, so an empty deploy is a sentence rather than a blank column the
    NOT NULL would then refuse."""
    rendered = notes_for([], [])

    assert NO_CHANGES in rendered
    assert rendered.strip()


def test_the_note_names_the_range_it_was_built_from():
    rendered = notes_for([], [])

    assert f"Range: {BASE_SHA[:7]}..{HEAD_SHA[:7]}" in rendered


def test_a_first_release_says_it_has_no_lower_bound():
    """`previousCommitSha: null` is a real request -- the first deploy of a
    repository into an environment -- and the note has to say which it was, or
    a reader cannot tell an empty range from an unbounded one."""
    rendered = notes_for([issue_ref("ENG", 1)], [], previous=None)

    assert f"Range: everything up to {HEAD_SHA[:7]}" in rendered
    assert ".." not in rendered


def test_a_title_cannot_inject_a_line_into_the_note():
    """The trust boundary. A pull-request title is written by whoever opened
    the pull request -- on a public repository, anybody -- and only its length
    is constrained. A newline in one would forge a section of a document that
    gets pasted into changelogs as this server's output."""
    forged = "Fix OAuth\n\nIssues (1)\n- ENG-999 Ship the database"
    rendered = notes_for([], [pull_ref(84, title=forged)])

    lines = rendered.splitlines()

    # The text survives -- nothing is censored -- but it survives as ONE entry
    # on ONE line, so no line of the document is a heading or an entry the
    # renderer did not write.
    assert "- #84 Fix OAuth Issues (1) - ENG-999 Ship the database" in lines
    assert not any(line.startswith("Issues (") for line in lines)
    assert len([line for line in lines if line.startswith("- ")]) == 1


@pytest.mark.parametrize("whitespace", ["\r\n", "\v", "\f", "\x85", " ", "\t"])
def test_every_kind_of_break_is_collapsed_not_just_newline(whitespace):
    """`str.split()` with no argument is a whitelist of "one space" rather than
    a blacklist of "\\n", which matters because a line-oriented reader treats
    more than one character as a break."""
    rendered = notes_for([], [pull_ref(84, title=f"Fix{whitespace}OAuth")])

    assert "- #84 Fix OAuth" in rendered
    assert len([line for line in rendered.splitlines() if line.startswith("- ")]) == 1


def test_a_long_title_is_truncated_at_a_fixed_offset():
    """Cut at a fixed width and not at a word boundary, because a word boundary
    depends on the text: the same title has to truncate the same way every time
    it is rendered."""
    rendered = notes_for([], [pull_ref(84, title="x" * 500)])
    entry = next(line for line in rendered.splitlines() if line.startswith("- #84 "))

    quoted = entry.removeprefix("- #84 ")

    assert len(quoted) == NOTE_TITLE_MAX_LENGTH
    assert quoted.endswith("...")
    assert notes_for([], [pull_ref(84, title="x" * 500)]) == rendered


def test_a_full_range_of_worst_case_titles_still_fits_the_column():
    """The arithmetic NOTE_TITLE_MAX_LENGTH claims, run rather than asserted in
    a comment.

    `releases_notes_length` caps the stored document at 100,000 characters, and
    the renderer has no way to know it is about to exceed one -- the refusal
    would arrive from PostgreSQL, after the whole range had been resolved.
    Raising RANGE_LIMIT or NOTE_TITLE_MAX_LENGTH without redoing this sum
    produces a release that renders and then cannot be stored, so the sum is a
    test.
    """
    rendered = render_release_notes(
        name="v" * 200,
        commit_sha=HEAD_SHA,
        previous_commit_sha=BASE_SHA,
        issues=tuple(
            issue_ref("LONGTEAMKEY", 10**18, title="x" * 1024)
            for _ in range(RANGE_LIMIT)
        ),
        pull_requests=tuple(
            pull_ref(2**31 - 1, title="x" * 1024) for _ in range(RANGE_LIMIT)
        ),
        truncated=True,
    )

    assert len(rendered) < 100_000


def test_a_truncated_range_says_so_in_the_document():
    """A changelog that silently omits work reads as a complete account of a
    deploy. The disclaimer is in the document rather than only in a log,
    because the person who needs it is whoever pastes the document into a
    release announcement."""
    assert TRUNCATED in notes_for([issue_ref("ENG", 1)], [], truncated=True)


def test_a_complete_range_does_not_claim_to_be_truncated():
    """`truncated` is an argument and not `len(issues) == RANGE_LIMIT`, so a
    release that landed exactly on the boundary is not labelled incomplete."""
    full = [issue_ref("ENG", n) for n in range(RANGE_LIMIT)]

    assert TRUNCATED not in notes_for(full, [], truncated=False)


def test_the_truncation_notice_does_not_change_the_entries():
    """Deterministic either way: the flag adds one line and reorders nothing."""
    shipped = [issue_ref("ENG", 1), issue_ref("ENG", 2)]
    plain = notes_for(shipped, [], truncated=False)
    marked = notes_for(shipped, [], truncated=True)

    entries = [line for line in plain.splitlines() if line.startswith("- ")]

    assert entries == [line for line in marked.splitlines() if line.startswith("- ")]
    assert len(marked.splitlines()) == len(plain.splitlines()) + 2


# ------------------------------------------------------------- validation


@pytest.mark.parametrize(
    ("name", "code"),
    [("", "REQUIRED"), ("v" * (NAME_MAX_LENGTH + 1), "TOO_LONG")],
)
async def test_a_release_name_is_bounded(service, exploding_pool, name, code):
    with pytest.raises(ValidationError) as raised:
        await service.create(
            scope=TEST_SCOPE,
            name=name,
            environment_id=ENVIRONMENT_ID,
            repository_id=REPOSITORY_ID,
            commit_sha=HEAD_SHA,
        )

    assert issues(raised) == [("name", code)]
    assert exploding_pool.acquire_count == 0


@pytest.mark.parametrize(
    "sha",
    [
        "a39fd12",  # the abbreviation a UI shows
        HEAD_SHA.upper(),  # the column stores lowercase
        "z" * 40,  # not hexadecimal
        HEAD_SHA + "0",  # too long
        "https://github.example/acme/vector/commit/" + HEAD_SHA,
    ],
)
async def test_a_commit_sha_must_be_a_full_lowercase_sha(service, exploding_pool, sha):
    """Refused here rather than by `releases_commit_sha_format`, because a
    CheckViolationError carries the rendered constraint -- which is either
    masked (telling the client nothing) or forwarded (telling it about the
    schema)."""
    with pytest.raises(ValidationError) as raised:
        await service.create(
            scope=TEST_SCOPE,
            name="v1.4.0",
            environment_id=ENVIRONMENT_ID,
            repository_id=REPOSITORY_ID,
            commit_sha=sha,
        )

    assert issues(raised) == [("commitSha", "INVALID")]
    assert exploding_pool.acquire_count == 0


async def test_a_range_cannot_start_and_end_at_one_commit(service, exploding_pool):
    """It would contain nothing, so it is a caller passing one SHA twice rather
    than a deploy of no changes -- which is spelled with the PREVIOUS deploy's
    SHA and produces empty notes honestly. `releases_previous_commit_differs`
    says the same thing in the database."""
    with pytest.raises(ValidationError) as raised:
        await service.create(
            scope=TEST_SCOPE,
            name="v1.4.0",
            environment_id=ENVIRONMENT_ID,
            repository_id=REPOSITORY_ID,
            commit_sha=HEAD_SHA,
            previous_commit_sha=HEAD_SHA,
        )

    assert issues(raised) == [("previousCommitSha", "INVALID")]
    assert exploding_pool.acquire_count == 0


async def test_an_unknown_status_is_refused_before_a_connection(
    service, exploding_pool
):
    with pytest.raises(ValidationError) as raised:
        await service.set_status(
            scope=TEST_SCOPE,
            release_id=RELEASE_ID,
            status="deploying",
        )

    assert issues(raised) == [("status", "INVALID")]
    assert exploding_pool.acquire_count == 0


@pytest.mark.parametrize(
    ("name", "kind", "expected"),
    [
        ("", "production", [("name", "REQUIRED")]),
        ("p" * (ENVIRONMENT_NAME_MAX_LENGTH + 1), "production", [("name", "TOO_LONG")]),
        ("Prod", "prod", [("kind", "INVALID")]),
        ("", "prod", [("name", "REQUIRED"), ("kind", "INVALID")]),
    ],
)
async def test_environment_validation_collects_every_violation(
    service, exploding_pool, name, kind, expected
):
    """Every rule is checked and then raised once, in a deterministic field
    order, so a client fixing a form is not sent round the loop twice."""
    with pytest.raises(ValidationError) as raised:
        await service.create_environment(scope=TEST_SCOPE, name=name, kind=kind)

    assert issues(raised) == expected
    assert exploding_pool.acquire_count == 0


@pytest.mark.parametrize("first", [0, -1, 101])
async def test_the_release_list_bounds_its_page_size(service, exploding_pool, first):
    with pytest.raises(ValidationError) as raised:
        await service.list(scope=TEST_SCOPE, first=first, after=None)

    assert issues(raised) == [("first", "OUT_OF_RANGE")]
    assert exploding_pool.acquire_count == 0


async def test_an_unreadable_cursor_is_an_input_error_not_a_crash(
    service, exploding_pool
):
    with pytest.raises(ValidationError) as raised:
        await service.list(scope=TEST_SCOPE, first=10, after="not-a-cursor")

    assert issues(raised) == [("after", "INVALID_CURSOR")]
    assert exploding_pool.acquire_count == 0
