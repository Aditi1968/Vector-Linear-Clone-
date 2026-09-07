"""Releases, the environments they go to, and the notes they carry.

Pure application code -- no Strawberry, FastAPI, asyncpg or PostgreSQL.

The interesting thing in this module is `render_release_notes`, which is the
whole "deterministic" half of the feature: a total function with no clock, no
randomness, no I/O and no dependence on the order its inputs arrive in. Given
the same issues and pull requests it returns the same bytes, today and in a
year. The other half -- that the inputs stop changing once a release is cut --
is migrations/024_releases.sql's, which freezes them into `release_issues` and
`release_pull_requests` and stores the rendered text in `releases.notes`.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Final
from uuid import UUID


# What an environment can be, restating `environments_kind_check` in
# migrations/024_releases.sql.
#
# This list and that constraint are two statements of one rule and have to be
# changed together, for the reason app.domain.initiatives gives about
# INITIATIVE_STATUSES. tests/test_migration_024_db.py asserts the two agree.
#
# Order is the order a pipeline runs in and is documentation only; nothing may
# read a comparison out of it. 'custom' is last because it is the one that
# names no position in a pipeline at all.
ENVIRONMENT_KINDS: Final[tuple[str, ...]] = (
    "development",
    "staging",
    "production",
    "custom",
)

# The four states a release moves through. See the long note on
# `releases.status` in migrations/024_releases.sql for why there are four and
# why 'deploying' is deliberately not one of them.
PENDING: Final = "pending"
DEPLOYED: Final = "deployed"
FAILED: Final = "failed"
ROLLED_BACK: Final = "rolled_back"

RELEASE_STATUSES: Final[tuple[str, ...]] = (PENDING, DEPLOYED, FAILED, ROLLED_BACK)

# What a release starts in. Here rather than as a column DEFAULT, for the
# reason app.domain.initiatives gives about DEFAULT_INITIATIVE_STATUS: a
# default in the schema outlives the migration, so an insert that forgot
# `status` would succeed quietly instead of failing.
DEFAULT_RELEASE_STATUS: Final = PENDING

# Which status may follow which.
#
# In the domain and not in the database, because no CHECK can read the previous
# value of a row and no trigger belongs in this schema (migrations/ hold plain
# DDL; see the PL/pgSQL note in tests/test_migration_lint.py). What the
# database does hold is the vocabulary and the agreement between the status and
# `deployed_at`, so the worst a bug here can do is refuse a legal move or allow
# an odd one -- never store a release claiming to be deployed with no instant.
#
# `failed` and `rolled_back` are terminal, and that is a decision rather than
# an oversight: retrying a deploy is a NEW release, because a second attempt
# that reuses the first attempt's row destroys the first attempt's timestamp --
# which is the one thing an incident review is looking for.
RELEASE_TRANSITIONS: Final[dict[str, tuple[str, ...]]] = {
    PENDING: (DEPLOYED, FAILED),
    DEPLOYED: (ROLLED_BACK,),
    FAILED: (),
    ROLLED_BACK: (),
}

# Which statuses may precede each one, which is the direction
# `ReleaseRepository.set_status` needs: it moves a release in one statement
# that names the states it is willing to move FROM, so a concurrent transition
# loses rather than being read and then overwritten.
RELEASE_TRANSITIONS_FROM: Final[dict[str, tuple[str, ...]]] = {
    target: tuple(
        source for source, targets in RELEASE_TRANSITIONS.items() if target in targets
    )
    for target in RELEASE_STATUSES
}


@dataclass(frozen=True, slots=True)
class EnvironmentEntity:
    """One deploy target of one workspace.

    No `workspace_id`, matching InitiativeEntity and IssueEntity: the workspace
    is how an operation is scoped, not something an entity carries around
    afterwards. A caller that could read it off an entity would eventually pass
    it back down as the scope for the next call.
    """

    id: UUID
    name: str
    kind: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ReleaseIssueRef:
    """One issue a release shipped, with the two fields a note quotes.

    Carries `team_key` and `number` rather than a rendered identifier, because
    the identifier is a rendering and the sort is not: ordering by the string
    "ENG-10" puts it before "ENG-9", which is a wrong -- if stable -- order for
    a document a human reads.
    """

    issue_id: UUID
    team_key: str
    number: int
    title: str

    @property
    def identifier(self) -> str:
        """`ENG-142`, the same rendering IssueEntity produces."""
        return f"{self.team_key}-{self.number}"


@dataclass(frozen=True, slots=True)
class ReleasePullRequestRef:
    """One pull request a release shipped.

    A snapshot, not a pointer: `title` and `url` are what
    `github_pull_requests` said at the moment the release was cut, and
    migrations/024_releases.sql explains why they are copied rather than
    joined.
    """

    repository_id: int
    number: int
    title: str
    url: str | None


@dataclass(frozen=True, slots=True)
class ReleaseRange:
    """What one commit range turned out to contain.

    The intermediate value between "resolve the range" and "write the release":
    it is what the notes are rendered from AND what is frozen into
    `release_issues` / `release_pull_requests`, so the text and the structured
    record are built from one object and cannot describe different releases.
    """

    issues: tuple[ReleaseIssueRef, ...]
    pull_requests: tuple[ReleasePullRequestRef, ...]


@dataclass(frozen=True, slots=True)
class CommitBoundary:
    """One end of a release's range, as `github_commits` holds it.

    `committed_at` is nullable there -- a push payload can omit it -- and a
    boundary with no instant cannot bound a window at all. The service reports
    that as an input error rather than silently widening the range, which would
    put another release's changes into this one's notes.
    """

    sha: str
    committed_at: datetime | None


@dataclass(frozen=True, slots=True)
class ReleaseEntity:
    """One release, as the rest of the application sees it.

    `issue_ids` and `pull_request_numbers` are part of the entity rather than
    fetched per release, for the reason InitiativeEntity carries `project_ids`:
    "what did this ship" is the question the feature exists to answer, and a
    shape that answered it only through a second round trip would make the
    headline capability the expensive path. The repository aggregates both in
    the same statement.

    `notes` is a column and not a property, because it was rendered once and
    stored; re-rendering it here would be a second implementation that could
    disagree with the text that actually went out.
    """

    id: UUID
    name: str
    environment_id: UUID
    repository_id: int

    commit_sha: str
    previous_commit_sha: str | None

    status: str
    notes: str

    deployed_at: datetime | None
    created_at: datetime
    updated_at: datetime

    issue_ids: tuple[UUID, ...]
    pull_request_numbers: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class ReleasePage:
    """A forward keyset page of releases.

    The same three fields as IssuePage and InitiativePage and for the same
    reasons: the nodes, a flag the caller cannot compute for itself, and the
    position to resume from.
    """

    nodes: list[ReleaseEntity]
    has_next_page: bool
    end_cursor: str | None


# How much of a commit SHA a note shows. Seven is what GitHub renders and what
# a human compares; the full value is on the release row for anything that has
# to be exact.
SHORT_SHA_LENGTH: Final = 7

# How many issues and how many pull requests one release resolves.
#
# A bound on work AND on the size of the document, not a product rule. A client
# sending `previousCommitSha: null` against a repository with ten thousand
# commits is asking for the whole history in one note, and the honest failure
# is a truncated list rather than a statement that will not fit in the column
# `releases_notes_length` bounds.
#
# In the domain rather than in the service, because both the service (which
# bounds the read) and this renderer (whose output has to fit) depend on the
# same number, and app/graphql may not import app/services.
RANGE_LIMIT: Final = 200

# How much of one title a note quotes.
#
# The arithmetic is the point: `releases_notes_length` caps the stored document
# at 100,000 characters, and 200 issues plus 200 pull requests at this width
# come to roughly 55,000 -- comfortably inside it with the headings and the
# range line. Raising either number without redoing this sum produces a release
# that renders and then cannot be stored.
NOTE_TITLE_MAX_LENGTH: Final = 120

# What a release that shipped nothing says. A range can legitimately be empty
# -- a deploy of a configuration change, a re-run after a failure -- and the
# renderer is total, so this is a real sentence rather than an empty document.
NO_CHANGES: Final = "No linked issues or pull requests in this range."

# What a release that hit RANGE_LIMIT says.
#
# A truncated changelog that does not admit it is worse than a refused one: it
# reads as a complete account of a deploy and quietly omits work. The line is
# in the document rather than only in a log because the document is what
# somebody pastes into a release announcement, and it is the reader of THAT who
# needs to know to go and look.
TRUNCATED: Final = (
    f"Only the first {RANGE_LIMIT} of each kind are listed; this range holds more."
)


def _one_line(text: str) -> str:
    """Collapse whitespace to single spaces, then bound the length.

    The collapse is not cosmetic. Issue and pull-request titles reach this
    function from `github_pull_requests.title`, which 017's header explains is
    written by whoever opened the pull request -- on a public repository,
    anybody -- and only its LENGTH is constrained. A title containing newlines
    would let that person inject lines into a document that gets pasted into
    changelogs and read as this server's own output:
    `"Fix\\n\\nIssues (1)\\n- ENG-1 Pwned"` renders as a second, forged section.

    `str.split()` with no argument splits on every Unicode whitespace character
    -- including the vertical tab, form feed and NEL that a line-oriented
    reader also treats as breaks -- so this is a whitelist of "one space", not
    a blacklist of "\\n".

    The truncation is what keeps the rendered document inside
    `releases_notes_length`; see NOTE_TITLE_MAX_LENGTH for the arithmetic. It
    cuts at a fixed offset rather than at a word boundary, because a word
    boundary depends on the text and this has to be predictable: the same title
    must truncate the same way every time it is rendered.
    """
    collapsed = " ".join(text.split())

    if len(collapsed) <= NOTE_TITLE_MAX_LENGTH:
        return collapsed

    return collapsed[: NOTE_TITLE_MAX_LENGTH - 3] + "..."


def _range_line(*, commit_sha: str, previous_commit_sha: str | None) -> str:
    """The one line saying which commits this release spans."""
    head = commit_sha[:SHORT_SHA_LENGTH]

    if previous_commit_sha is None:
        return f"Range: everything up to {head}"

    return f"Range: {previous_commit_sha[:SHORT_SHA_LENGTH]}..{head}"


def render_release_notes(
    *,
    name: str,
    commit_sha: str,
    previous_commit_sha: str | None,
    issues: tuple[ReleaseIssueRef, ...],
    pull_requests: tuple[ReleasePullRequestRef, ...],
    truncated: bool,
) -> str:
    """The release note for one range, as a deterministic document.

    Deterministic in the strong sense, and each clause below is one of the ways
    that could quietly stop being true:

    * It sorts its OWN inputs. Ordering by `(team_key, number)` and by
      `(repository_id, number)` here rather than trusting the caller's order is
      what makes the guarantee a property of this function instead of a
      property of one SQL statement's ORDER BY -- so a second caller, a
      re-render from stored rows, or a repository whose plan changes cannot
      produce different bytes from the same set.
    * It reads no clock. There is no "generated at" line, because a timestamp
      is precisely the field that makes two renderings of one release differ.
      When the release was cut is `releases.created_at`, which is data.
    * It is total. Every input produces a document: an empty range renders
      NO_CHANGES, and a title of only whitespace collapses to an empty string
      rather than raising.
    * Titles are flattened to one line. See `_one_line` -- this is a trust
      boundary, not formatting.

    `truncated` is a required argument rather than something inferred from
    `len(issues)`, and the difference matters: the caller reads RANGE_LIMIT + 1
    rows and trims, so it is the only frame that can tell "exactly RANGE_LIMIT
    changes shipped" from "at least RANGE_LIMIT did". Inferring it here would
    put a false disclaimer on the first release that happened to land on the
    boundary exactly.

    Plain text rather than Markdown. The one thing a note has to do is survive
    being pasted somewhere else unchanged, and Markdown would mean escaping
    user-authored titles against a syntax this function does not otherwise
    need. A `- ` prefix reads as a list in a Markdown renderer anyway.

    Pure application code: no Strawberry, FastAPI, asyncpg or PostgreSQL.
    """
    ordered_issues = sorted(issues, key=lambda ref: (ref.team_key, ref.number))
    ordered_pulls = sorted(
        pull_requests, key=lambda ref: (ref.repository_id, ref.number)
    )

    lines = [_one_line(name), ""]

    if ordered_issues:
        lines.append(f"Issues ({len(ordered_issues)})")
        lines.extend(
            f"- {ref.identifier} {_one_line(ref.title)}" for ref in ordered_issues
        )
        lines.append("")

    if ordered_pulls:
        lines.append(f"Pull requests ({len(ordered_pulls)})")
        lines.extend(f"- #{ref.number} {_one_line(ref.title)}" for ref in ordered_pulls)
        lines.append("")

    if not ordered_issues and not ordered_pulls:
        lines.append(NO_CHANGES)
        lines.append("")

    if truncated:
        lines.append(TRUNCATED)
        lines.append("")

    lines.append(
        _range_line(commit_sha=commit_sha, previous_commit_sha=previous_commit_sha)
    )

    return "\n".join(lines)
