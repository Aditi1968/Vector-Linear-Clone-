import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final
from uuid import UUID


# What a workspace's GitHub integration can be, and the four answers are
# deliberately four rather than a boolean.
#
# UNCONFIGURED  this deployment has no GitHub App credentials at all, so no
#               workspace here can connect one. An operator's problem.
# DISCONNECTED  the deployment is configured; this workspace has not
#               installed the app, or has removed it. A user's problem, and
#               one a "Connect" button fixes.
# PENDING       this workspace has claimed an installation and GitHub has not
#               confirmed it. Nobody's problem yet, and nobody's data either:
#               the account and the repositories stay empty until a signed
#               delivery says the claim was true.
# CONNECTED     GitHub has confirmed the claim.
#
# Collapsing UNCONFIGURED and DISCONNECTED into "not connected" would put a
# Connect button in front of a user whose click cannot possibly work, and hide
# from the operator that the deployment they are looking at was never given a
# key. Collapsing PENDING into CONNECTED is worse, and is the defect
# migrations/016_github_installation_trust.sql exists to close: an id the
# caller typed is not evidence that the caller owns the installation it names,
# so reporting a claim as a connection reports somebody else's organisation as
# this workspace's.
#
# Order is documentation only; nothing may read a comparison out of it. The
# GraphQL enum in app/graphql/types/github.py is the transport's copy, and
# tests/test_github.py pins the two equal.
GITHUB_STATUSES: Final = ("unconfigured", "disconnected", "pending", "connected")

UNCONFIGURED: Final = "unconfigured"
DISCONNECTED: Final = "disconnected"
PENDING: Final = "pending"
CONNECTED: Final = "connected"


@dataclass(frozen=True, slots=True)
class GithubInstallationEntity:
    """One workspace's installation of the GitHub App.

    Carries no `workspace_id`, exactly as CycleEntity carries no workspace:
    it is only ever read through a scope the caller already holds, and a
    second copy travelling on the entity is the copy that eventually gets
    trusted by mistake.

    Carries no token, no key and no signing material, and never will. The
    row it is built from has no such column; see
    migrations/013_github_integration.sql.

    Pure application code: no Strawberry, FastAPI, asyncpg or PostgreSQL.
    """

    installation_id: int

    # None until the first signed `installation` webhook tells us the account.
    # A real, short-lived state rather than a missing value -- the setup
    # redirect GitHub sends the admin back with does not carry it. While
    # `confirmed_at` is None this is None too, and the database says so rather
    # than the writer remembering to: see
    # github_installations_unconfirmed_holds_no_account in 016.
    account_login: str | None

    connected_by: UUID
    connected_at: datetime
    updated_at: datetime

    # When a signature-verified delivery named this installation, or None while
    # the row is still only a claim the callback wrote.
    #
    # This is the whole difference between "a workspace said it installed the
    # app" and "GitHub said so", and every read that matters keys off it: an
    # unconfirmed row is PENDING, resolves no webhook, and may hold nothing
    # about the organisation it names.
    confirmed_at: datetime | None


@dataclass(frozen=True, slots=True)
class GithubRepositoryEntity:
    """One repository an installation covers.

    `repository_id` is GitHub's, and is what survives a rename; `full_name`
    is what a human reads and what a rename rewrites.
    """

    repository_id: int
    full_name: str

    # Whether this workspace applies deliveries about it. See
    # `github_repositories.tracked` in migration 030.
    #
    # Defaulted to True because this type is two things: a row read back, where
    # the column says, and a PARSE RESULT off a webhook payload, where there is
    # no column to read -- GitHub reports what an installation covers and has
    # no opinion about what a Vector workspace wants from it. A newly seeded
    # repository is tracked, so the default is the same answer the column's own
    # DEFAULT gives and the parse path needs no third value meaning "unknown".
    tracked: bool = True


@dataclass(frozen=True, slots=True)
class GithubGrant:
    """What one OAuth code turned out to be worth.

    Two answers from a single exchange, because the code buys exactly one.
    `installation_ids` is every installation the consenting account
    administers -- the evidence that promotes a claim. `repositories` is the
    set covered by the ONE installation that grant resolves to, or empty when
    it resolves to none.

    The pairing exists because of a gap the webhook path cannot close.
    Repositories are otherwise written only by an `installation` or
    `installation_repositories` delivery, and GitHub emits `installation`
    exactly once -- when the app is first installed. A workspace connecting to
    an app that is ALREADY installed therefore gets a confirmed integration
    covering nothing, and no later event ever fills it in, because from
    GitHub's side nothing changed. Disconnecting inside Vector and pressing
    Connect again is precisely that case: Vector's rows go, the installation
    on GitHub stays, and the reconnect has no delivery to wait for.
    """

    installation_ids: tuple[int, ...]
    repositories: tuple[GithubRepositoryEntity, ...]


# What every unusable grant is. Named once rather than spelled at each of the
# half-dozen `return`s that mean it: a deployment with no ownership check, a
# provider that is down, an exchange GitHub refused, a token that came back
# malformed, and a callback with no `code` at all are the same answer to
# everyone downstream, and a caller must not be able to tell them apart.
NO_GRANT: Final = GithubGrant(installation_ids=(), repositories=())


@dataclass(frozen=True, slots=True)
class GithubIntegrationEntity:
    """What one workspace's integration looks like right now.

    `installation` is present whenever a row exists, including when `status`
    is UNCONFIGURED -- a deployment whose credentials were removed still has
    workspaces that connected under the old ones, and hiding that would tell
    a user their integration is gone when it is only unusable.
    """

    status: str
    installation: GithubInstallationEntity | None
    repositories: tuple[GithubRepositoryEntity, ...]

    # The per-team status automations, for the teams that have configured one.
    #
    # A forward reference because the type is declared with the rest of the
    # development-activity vocabulary below, which is where it belongs: it is
    # what a pull request does, not what an installation is. Empty for a
    # workspace where nobody has turned one on, which is also every workspace
    # until somebody does -- absence is the off switch, so an empty tuple and
    # "automation disabled everywhere" are the same fact rather than two.
    automations: tuple["GithubAutomationEntity", ...] = ()


# --- development activity ---------------------------------------------


# The three places a pull request can carry an identifier, matching
# github_pull_request_issues_source_check in 017.
#
# They are kept apart rather than collapsed into "linked" because they are not
# equally strong evidence and because they retract independently: editing a
# title must remove the link the title made and leave the one the branch name
# still supports. A commit has no such tuple -- a commit message is immutable
# and is the only place an identifier can appear in one.
LINK_SOURCE_TITLE: Final = "title"
LINK_SOURCE_BODY: Final = "body"
LINK_SOURCE_BRANCH: Final = "branch"
LINK_SOURCES: Final = (LINK_SOURCE_TITLE, LINK_SOURCE_BODY, LINK_SOURCE_BRANCH)

# What GitHub's `state` may be, restated from
# github_pull_requests_state_check. A payload carrying anything else is a
# schema change at the provider, and writing it would abort the delivery on
# the CHECK -- so the parser refuses it before the statement runs.
PULL_REQUEST_STATES: Final = ("open", "closed")

# The four things a Development section renders, derived rather than stored.
# See `pull_request_display_state`.
DRAFT: Final = "draft"
OPEN: Final = "open"
MERGED: Final = "merged"
CLOSED: Final = "closed"

PULL_REQUEST_DISPLAY_STATES: Final = (DRAFT, OPEN, MERGED, CLOSED)

# How much development activity one issue's panel holds.
#
# A cap rather than a page, deliberately. The Development section is a short
# list beside an issue, not a feed: an issue with more than this many linked
# pull requests is not a paging problem, it is a repository where the
# identifier appears in every branch name. A cursor here would be API surface
# for a scroll nobody performs.
#
# In the domain rather than in the service, because both the service (which
# bounds the read) and the transport (which bounds the slice it hands back)
# have to agree on it, and app/graphql may not import app/services -- nothing
# else in the GraphQL layer does, and a transport reaching into a service
# module for a constant is the first step to reaching in for a method.
DEVELOPMENT_LIMIT: Final = 50


def pull_request_display_state(
    *,
    state: str,
    draft: bool,
    merged_at: datetime | None,
) -> str:
    """The one word a Development section shows for a pull request.

    GitHub has no such field, which is why 017 stores its three source
    columns instead and this derives the fourth: `state` is open or closed, a
    merged pull request arrives as closed WITH `merged_at`, and `draft` is a
    boolean orthogonal to both.

    The precedence is merged, then closed, then draft, then open, and each
    step is a decision rather than an ordering that fell out:

    * merged first, because a merged pull request is closed and reporting it
      as CLOSED would lose the only distinction anyone cares about --
      "abandoned" versus "shipped";
    * closed before draft, because `draft` is not cleared when a draft is
      closed and GitHub goes on sending it as true. Answering DRAFT for one
      would tell a reader that work is still in progress on a pull request
      nobody is going to finish -- the ONE ordering here that is observable
      and gets chosen wrong;
    * draft before open, because a draft is open on GitHub and showing it as
      OPEN tells a reader work is ready for review when its author says it is
      not;
    * `merged_at` rather than a `merged` boolean, because that is the column
      017 has, and `github_pull_requests_merged_is_closed` already refuses the
      one combination this function could not answer honestly.
    """
    if merged_at is not None:
        return MERGED

    if state != "open":
        return CLOSED

    return DRAFT if draft else OPEN


# --- moving an issue because of a pull request --------------------------


@dataclass(frozen=True, slots=True)
class GithubAutomationEntity:
    """One team's answer to "what does a pull request do to an issue".

    Both ids are optional and NULL means that half does nothing, which is a
    real configuration: a team may want the merge automated and the start left
    to whoever is doing the work. A row with both NULL cannot exist --
    `github_issue_automations_moves_something` refuses it -- because the
    absence of the row is already how the automation is turned off.

    Carries no workspace, exactly as every other entity here: it is only ever
    read through a scope the caller already holds.
    """

    team_id: UUID
    started_state_id: UUID | None
    completed_state_id: UUID | None


# The pull-request actions that are a TRANSITION rather than a restatement.
#
# This is the one place in the GitHub feature that branches on `action`, and
# the divergence from `GithubService._apply_pull_request` -- which deliberately
# does not, because every action carries the whole `pull_request` object -- is
# the difference between STORING a pull request and REACTING to one. Storing
# asks what the pull request IS, and every payload answers that in full.
# Moving an issue asks what just CHANGED, and `action` is the only field that
# says; every other field says what is true now and would say it again on the
# next `synchronize`.
#
# That distinction is also the idempotency and the don't-fight-a-human rule in
# one. GitHub sends the whole object on every later edit of an open pull
# request, each under a delivery id `github_deliveries` has never seen, so a
# rule keyed on "the pull request is open" would re-assert the move on every
# push -- dragging an issue back out of wherever a person had since moved it.
# Keyed on the transition, the automation acts once and then leaves the issue
# alone.
#
# `reopened` and `ready_for_review` are here for the same reason `opened` is:
# each is the instant work restarts or stops being a draft. `synchronize`,
# `edited`, `labeled`, `assigned` and the rest are absent -- they change
# something about a pull request that was already open.
#
# `converted_to_draft` is deliberately absent too, and that is a decision
# rather than an omission: it would have to move the issue BACKWARDS, and an
# automation that can undo a state move is one that can undo a person's.
START_ACTIONS: Final = frozenset({"opened", "reopened", "ready_for_review"})

# The action a merge arrives under. GitHub has no `merged` action: a merge is
# `closed` with `merged_at` set, which is why 017 stores the three source
# fields rather than a flattened enum, and why the check below reads the
# derived display state instead of the action alone.
CLOSE_ACTION: Final = "closed"

# The categories an automated move may move an issue OUT of, in order.
#
# `canceled` is deliberately not on this ladder. A person who cancels an issue
# has made a statement about the work, and a pull request merging afterwards --
# which happens, because a branch outlives the decision to drop it -- must not
# quietly reopen the question by marking it done. Off the ladder means never a
# source, which makes that a property of the vocabulary rather than a condition
# somebody has to remember to write.
CATEGORY_LADDER: Final = ("backlog", "unstarted", "started", "completed")


def automated_move_for(*, action: Any, display_state: str) -> str | None:
    """Which category of state this delivery moves a linked issue to, if any.

    A category and not a state id, because there is no such thing as "the
    started state": 005 makes states team-scoped and user-named, so the id is
    the team's configured answer and this is only the question.

    The four outcomes, and the two that return None are the interesting ones:

    * a pull request that OPENS, reopens or leaves draft -> 'started';
    * one that MERGES -> 'completed';
    * one that is a DRAFT -> nothing. Opening a draft is a statement that the
      work is not ready, and an issue that jumped to In Progress because
      somebody pushed a work-in-progress branch is the bug report this rule
      exists to prevent. Marking it ready for review is what fires;
    * one that is CLOSED WITHOUT MERGING -> nothing, and emphatically not a
      completion. An abandoned attempt is not shipped work, and moving the
      issue to a completed state for one would report as finished something
      nobody did. The issue also does not move BACK: what state it should
      return to is a question only the person who abandoned the pull request
      can answer.

    `action` is typed `Any` because it arrives out of a JSON payload, where a
    key can be missing or hold anything at all. Anything that is not one of the
    strings named above answers None, which is the same answer the ordinary
    `synchronize` gets.

    Pure application code: no Strawberry, FastAPI, asyncpg or PostgreSQL.
    """
    if display_state == MERGED:
        # Guarded by the action as well, so a later `edited` of an
        # already-merged pull request -- which GitHub sends in full, under a
        # fresh delivery id -- does not re-assert the move.
        return "completed" if action == CLOSE_ACTION else None

    if display_state == OPEN and action in START_ACTIONS:
        return "started"

    return None


def categories_below(target: str) -> tuple[str, ...]:
    """The categories an issue may be moved out of, to reach `target`.

    Forward-only, and this is the whole of "do not fight a human". An
    automation may carry an issue further along the ladder and never back down
    it, so:

    * an issue a person has already moved to a `started` state stays in THAT
      state when a pull request opens -- the team may have three of them, and
      "In Review" is not something to overwrite with "In Progress";
    * an issue already in a completed or canceled state is never moved by
      anything;
    * a merge still completes an issue sitting in backlog, unstarted or
      started, because every one of those is behind the merge.

    It is also what makes redelivery harmless without a dedupe table: the
    second application finds the issue already at or past the target and moves
    nothing, so there is no second activity row and no second notification.

    An unknown target answers `()` -- move nothing -- rather than raising. The
    only callers pass a value `automated_move_for` returned, and failing closed
    is the right shape for a rule that decides whether to write.
    """
    if target not in CATEGORY_LADDER:
        return ()

    return CATEGORY_LADDER[: CATEGORY_LADDER.index(target)]


# How the history records that a pull request moved an issue, and the prefix a
# renderer matches on. See `issue_activity.caused_by` in migration 030.
#
# Built from the repository's `full_name` rather than its numeric id, because
# this string is read by a person: "#84" alone is ambiguous the moment a
# workspace tracks two repositories, and GitHub's ids are not something anybody
# recognises.
AUTOMATION_CAUSE_PREFIX: Final = "github_pull_request:"

# `issue_activity_caused_by_length`, restated so the value is clamped here
# rather than aborting a delivery on the CHECK.
CAUSE_MAX_LENGTH: Final = 200


def pull_request_cause(repository_full_name: str | None, number: int) -> str:
    """What `issue_activity.caused_by` says when a pull request did it.

    `github_pull_request:acme/vector#84`, or the number alone when the payload
    carried no usable repository name -- which is still enough for a reader to
    find the pull request in the Development section beside the timeline, and
    much better than a row that says nobody did it.
    """
    if not repository_full_name:
        return f"{AUTOMATION_CAUSE_PREFIX}#{number}"

    return f"{AUTOMATION_CAUSE_PREFIX}{repository_full_name}#{number}"[
        :CAUSE_MAX_LENGTH
    ]


# `ENG-142` wherever it appears, rather than anchored the way
# app.domain.search._IDENTIFIER is.
#
# The same two halves and the same reasons -- `teams_key_format` in 005 is
# `^[A-Z][A-Z0-9]{0,9}$`, so the key half forbids a hyphen and `ENG-142` has
# exactly one parse; `issues.number` is BIGINT, so a longer run of digits is
# not a large issue number but an int asyncpg would refuse to bind.
#
# The two lookarounds are what stop a false positive being written into a link
# row. Without the lookbehind, `XENG-142` offers `ENG-142`; without the
# lookahead, `ENG-1425` offers `ENG-142` as well as itself. A hyphen is
# deliberately allowed before the key, because `fix/eng-142-oauth` is the
# ordinary shape of a branch name and is the source this whole detection is
# most confident about.
#
# Case-insensitive and uppercased before use, exactly as the search parser
# does: a branch is conventionally lowercase and names the same issue.
_MENTION = re.compile(
    r"(?<![A-Za-z0-9])([A-Za-z][A-Za-z0-9]{0,9})-([0-9]{1,18})(?![0-9])"
)

# How much of a title, body or commit message is scanned.
#
# A bound on work, not a product rule. A pull-request body is written by
# whoever opened the pull request -- on a public repository, anybody -- and
# GitHub will accept a megabyte of it, so an unbounded scan is an unbounded
# regex run on text an attacker chooses. Well above any real description; the
# identifiers that matter are near the top in every case anyone has.
MENTION_SCAN_LIMIT: Final = 10_000

# How many DISTINCT identifiers one piece of text may offer.
#
# The same bound from the other side: each one becomes a row in an array
# parameter and a row in a join table, so a body listing ten thousand of them
# is a write amplification with a signature on it. Twenty is more than any
# genuine "Fixes ENG-1, ENG-2, ENG-3" has ever needed.
MENTION_LIMIT: Final = 20


def issue_identifiers(text: str | None) -> tuple[tuple[str, int], ...]:
    """Every `ENG-142` this text offers, as (team key, number) pairs.

    A REQUEST to link and never a proof of one, which is the whole framing of
    migrations/017_github_development.sql: this text is authored by whoever
    opened the pull request. Nothing here resolves anything -- the caller
    resolves these pairs inside the workspace that owns the repository the
    delivery came from, and the schema refuses the row if it somehow does not.

    Ordered by first appearance and de-duplicated, so `Fixes ENG-1, closes
    ENG-1` is one request rather than a primary key violation.

    Pure application code: no Strawberry, FastAPI, asyncpg or PostgreSQL.
    """
    if not text:
        return ()

    found: dict[tuple[str, int], None] = {}

    for match in _MENTION.finditer(text[:MENTION_SCAN_LIMIT]):
        found[(match.group(1).upper(), int(match.group(2)))] = None

        if len(found) >= MENTION_LIMIT:
            break

    return tuple(found)


# What a generated branch name may contain, after folding.
#
# Lowercase alphanumerics and single hyphens, which is a strict subset of what
# git accepts as a ref: it cannot contain the sequences git forbids (`..`,
# `@{`, a trailing `.lock`), cannot be mistaken for an option, and needs no
# shell quoting -- so `git checkout -b <name>` is safe to paste whatever the
# issue was called. It also satisfies
# github_pull_requests_head_ref_format, which is what the branch comes back
# through when a pull request is opened from it.
_UNSAFE = re.compile(r"[^a-z0-9]+")

# The ceiling on a generated name.
#
# Not git's limit -- git's is a filesystem path -- but a length a terminal and
# a pull-request page can both show whole. A truncated slug is still unique
# because the identifier leads it, which is the property that actually
# matters: two issues never generate the same branch name however similar
# their titles.
BRANCH_NAME_MAX_LENGTH: Final = 60


def branch_name_for(identifier: str, title: str) -> str:
    """A deterministic, safe branch name for an issue: `eng-142-fix-oauth`.

    Pure and total. Every input produces a usable ref, because the parts that
    could fail are the parts that get dropped:

    * unicode is folded to ASCII through NFKD and then discarded if it does not
      survive -- `Ünicode` becomes `unicode`, and a title written entirely in a
      non-Latin script becomes nothing at all;
    * punctuation, whitespace and emoji collapse into single hyphens;
    * an empty result -- an empty title, a title of only punctuation, a title
      of only CJK -- leaves the identifier alone, which is still a valid,
      unique, checkout-able branch name.

    The identifier leads, and that is the product decision here rather than a
    formatting one: it is what makes the branch name detectable as this
    issue's by `issue_identifiers` above when a pull request is later opened
    from it, without anyone having to type the identifier a second time.

    No GitHub permission is needed to produce one. This creates nothing; it is
    a string for a human to copy.

    Pure application code: no Strawberry, FastAPI, asyncpg or PostgreSQL.
    """
    folded = (
        unicodedata.normalize("NFKD", title)
        .encode("ascii", "ignore")
        .decode("ascii")
        .lower()
    )

    slug = _UNSAFE.sub("-", folded).strip("-")
    prefix = identifier.lower()

    if not slug:
        return prefix

    # Trimmed after joining rather than before, so the identifier is never the
    # part that gets cut, and re-stripped because the cut can land on a hyphen.
    return f"{prefix}-{slug}"[:BRANCH_NAME_MAX_LENGTH].rstrip("-")


@dataclass(frozen=True, slots=True)
class GithubPullRequestEntity:
    """One pull request, as the Development section shows it.

    Carries the repository's `full_name` beside its id because the panel
    renders "acme/vector #84" and the id alone is not a thing to show a human.
    Carries no workspace, exactly as GithubInstallationEntity does not: it is
    only ever read through a scope the caller already holds.
    """

    repository_id: int
    repository_full_name: str
    number: int
    title: str

    # GitHub's own three, stored as sent. `display_state` is the derivation.
    state: str
    draft: bool
    merged_at: datetime | None

    head_ref: str | None
    url: str | None
    github_updated_at: datetime | None

    # Which of `title` / `body` / `branch` link this pull request to the issue
    # being asked about. Plural because they are independent: a pull request
    # whose title and branch both name the issue has two, and losing one does
    # not lose the other.
    link_sources: tuple[str, ...] = ()

    @property
    def display_state(self) -> str:
        return pull_request_display_state(
            state=self.state,
            draft=self.draft,
            merged_at=self.merged_at,
        )


@dataclass(frozen=True, slots=True)
class GithubCommitEntity:
    """One commit, as the Development section shows it."""

    repository_id: int
    repository_full_name: str
    sha: str
    message: str
    url: str | None
    committed_at: datetime | None

    @property
    def short_sha(self) -> str:
        """The seven characters a human reads.

        Derived here rather than stored, for the reason 017 gives for keying
        on the full SHA: the abbreviation is ambiguous by construction, so it
        is a rendering and never an identity.
        """
        return self.sha[:7]

    @property
    def summary(self) -> str:
        """The first line of the message, which is what a list shows."""
        return self.message.split("\n", 1)[0]


@dataclass(frozen=True, slots=True)
class GithubDevelopmentEntity:
    """What one issue's Development section has to render.

    `branch_name` is present whether or not anything is linked, and that is
    the point of it: the section's first job on an issue with no activity is
    to offer the branch to start.
    """

    branch_name: str
    pull_requests: tuple[GithubPullRequestEntity, ...]
    commits: tuple[GithubCommitEntity, ...]
