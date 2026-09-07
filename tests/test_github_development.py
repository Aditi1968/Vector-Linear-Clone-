"""What a `pull_request` or `push` delivery is allowed to do, and to whom.

No database, no Docker, no network, no GitHub credentials -- the fake
repository in tests/test_github.py stands in for the statements, and
tests/test_github_development_db.py runs the real ones against a real schema.

The one sentence this file exists to defend is the one
migrations/017_github_development.sql opens with: the identifier in a payload
is text somebody wrote in a pull request, and on a public repository that
somebody is anybody at all. So it is a REQUEST to link and never a proof of
one, and the four questions asked here follow from that:

  * a pull request on workspace A's repository, titled with workspace B's
    identifier, links to NOTHING -- and not because a write was refused, but
    because the resolver never finds an id to write;
  * a redelivery is applied once, and still answers 2xx;
  * a redelivery that arrives out of order does not overwrite newer data with
    older, and does not re-derive links from a title that has since changed;
  * a forged signature never reaches any of it.

Everything else here is the ordinary path, pinned because the refusals prove
nothing if the feature does not work: the four pull-request lifecycle states,
the three link sources retracting independently, commits, disconnection, and
the branch-name helper.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import UUID

import pytest

from app.domain.github import (
    LINK_SOURCES,
    GithubRepositoryEntity,
    branch_name_for,
    issue_identifiers,
    pull_request_display_state,
)
from app.domain.tenancy import WorkspaceScope
from app.graphql.schema import build_schema
from app.graphql.types.github import (
    DEFAULT_DEVELOPMENT_FIRST,
    GithubLinkSourceType,
    GithubPullRequestStateType,
)
from app.services.github import COMMITS_PER_PUSH_LIMIT, DELIVERY_ID_MAX_LENGTH

from tests.conftest import (
    TEST_AUTHORIZED_SCOPE,
    TEST_WORKSPACE_ID,
    TEST_WORKSPACE_SLUG,
    graphql_context,
    make_entity,
)
from tests.test_github import (
    INSTALLATION_ID,
    OTHER_WORKSPACE_ID,
    WORKSPACE_ID,
    build_service,
)


schema = build_schema("test")

REPOSITORY_ID = 11111
OTHER_REPOSITORY_ID = 22222
PULL_NUMBER = 84

OUR_ISSUE_ID = UUID("00000000-0000-7000-8000-0000000000c1")
THEIR_ISSUE_ID = UUID("00000000-0000-7000-8000-0000000000c2")
SECOND_ISSUE_ID = UUID("00000000-0000-7000-8000-0000000000c3")

# 40 lowercase hex, which is what github_commits_sha_format demands.
SHA = "a39fd12" + "0" * 33
OTHER_SHA = "b41ce09" + "1" * 33

EARLIER = "2026-04-01T09:00:00Z"
LATER = "2026-04-01T10:00:00Z"

SCOPE = WorkspaceScope(workspace_id=WORKSPACE_ID)


def instant(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def development_service(*, issues=None, repositories=None, **kwargs):
    """A confirmed installation covering one repository, with issues to find.

    `issues` is keyed exactly as the fake's map is -- (workspace, team key,
    number) -- because that triple is what the real statement's predicate is,
    and a fixture that dropped the workspace would make every cross-tenant
    assertion below pass for the wrong reason.
    """
    service, repository = build_service(
        workspace_id=WORKSPACE_ID,
        repositories=(
            repositories
            if repositories is not None
            else [
                GithubRepositoryEntity(
                    repository_id=REPOSITORY_ID,
                    full_name="acme/vector",
                )
            ]
        ),
        **kwargs,
    )

    repository.issues = dict(issues or {})

    return service, repository


def ours(team_key="ENG", number=142, issue_id=OUR_ISSUE_ID):
    return {(WORKSPACE_ID, team_key, number): issue_id}


def pull_payload(
    *,
    action="opened",
    title="Fix the OAuth callback",
    body=None,
    head_ref=None,
    state="open",
    draft=False,
    merged_at=None,
    updated_at=LATER,
    number=PULL_NUMBER,
    repository_id=REPOSITORY_ID,
):
    return {
        "action": action,
        "installation": {"id": INSTALLATION_ID},
        "repository": {"id": repository_id, "full_name": "acme/vector"},
        "pull_request": {
            "number": number,
            "title": title,
            "body": body,
            "state": state,
            "draft": draft,
            "merged_at": merged_at,
            "updated_at": updated_at,
            "head": {"ref": head_ref},
            "html_url": f"https://github.com/acme/vector/pull/{number}",
        },
    }


def push_payload(*, commits=None, repository_id=REPOSITORY_ID, head_commit=None):
    return {
        "ref": "refs/heads/main",
        "installation": {"id": INSTALLATION_ID},
        "repository": {"id": repository_id, "full_name": "acme/vector"},
        "commits": (
            commits
            if commits is not None
            else [
                {
                    "id": SHA,
                    "message": "ENG-142 handle the redirect",
                    "url": f"https://github.com/acme/vector/commit/{SHA}",
                    "timestamp": LATER,
                }
            ]
        ),
        "head_commit": head_commit,
    }


def links(repository, source=None):
    """The (issue, source) pairs stored for the pull request under test."""
    return {
        (link[3], link[4])
        for link in repository.pull_links
        if source is None or link[4] == source
    }


# --- the branch-name helper -------------------------------------------
#
# Pure, total and used by a human rather than by a write path: it creates
# nothing on GitHub and needs no permission. Which is why it is tested to
# destruction here -- its whole contract is that no title can make it produce
# something that is not a checkout-able ref.


@pytest.mark.parametrize(
    ("identifier", "title", "expected"),
    [
        ("ENG-142", "Fix Slack OAuth callback", "eng-142-fix-slack-oauth-callback"),
        # Punctuation, symbols and runs of whitespace all collapse to one
        # hyphen, and a leading or trailing one is trimmed.
        ("ENG-1", "  Fix: the (broken) thing!!  ", "eng-1-fix-the-broken-thing"),
        ("ENG-2", "a/b/c", "eng-2-a-b-c"),
        ("ENG-3", "Don't -- really -- do this", "eng-3-don-t-really-do-this"),
        # Unicode that has an ASCII form keeps it.
        ("ENG-4", "Café checkout naïve", "eng-4-cafe-checkout-naive"),
        # Unicode that has none is dropped rather than escaped.
        ("ENG-5", "修复登录", "eng-5"),
        ("ENG-6", "🚀🚀🚀", "eng-6"),
        # Nothing to slug at all: the identifier alone is still unique, still
        # valid, and still checkout-able.
        ("ENG-7", "", "eng-7"),
        ("ENG-8", "   ", "eng-8"),
        ("ENG-9", "...", "eng-9"),
        # An identifier arriving in the lowercase a branch is conventionally
        # written in is the same branch.
        ("eng-10", "Already lower", "eng-10-already-lower"),
    ],
)
def test_the_branch_name_is_what_a_human_would_have_typed(identifier, title, expected):
    assert branch_name_for(identifier, title) == expected


def test_a_very_long_title_is_cut_without_leaving_a_trailing_hyphen():
    """The cut can land on a separator, and `eng-1-fix-the-` is not a name."""
    name = branch_name_for("ENG-1", "fix the " * 40)

    assert len(name) <= 60
    assert not name.endswith("-")
    assert name.startswith("eng-1-fix-the")


def test_a_long_identifier_is_never_the_part_that_is_cut():
    """Trimming after joining is what guarantees this.

    The identifier is what makes the name unique and what makes a pull request
    opened from the branch link back, so a helper that truncated it would
    quietly produce two issues' branches with one name.
    """
    name = branch_name_for("PLATFORM-999999", "a" * 200)

    assert name.startswith("platform-999999-")


@pytest.mark.parametrize("title", ["", "🚀", "Fix: the thing", "a" * 500, "../../etc"])
def test_no_title_can_produce_something_git_would_refuse(title):
    """The charset is the guarantee, so it is asserted as one.

    Lowercase alphanumerics and single hyphens cannot spell any of the
    sequences git rejects in a ref -- `..`, `@{`, a trailing `.lock`, a
    leading `-` that a shell would read as an option.
    """
    name = branch_name_for("ENG-1", title)

    assert name
    assert set(name) <= set("abcdefghijklmnopqrstuvwxyz0123456789-")
    assert ".." not in name
    assert not name.startswith("-")
    assert not name.endswith("-")


def test_the_branch_name_is_deterministic():
    assert branch_name_for("ENG-142", "Fix OAuth") == branch_name_for(
        "ENG-142", "Fix OAuth"
    )


def test_a_generated_branch_name_names_its_own_issue_back():
    """The round trip the helper exists for.

    A branch made from this name, opened as a pull request, arrives as a
    `head_ref` -- and `issue_identifiers` has to find the issue in it, or the
    convenience of the helper buys nothing.
    """
    name = branch_name_for("ENG-142", "Fix Slack OAuth callback")

    assert issue_identifiers(name) == (("ENG", 142),)


# --- finding an identifier in somebody else's text ---------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("ENG-142", (("ENG", 142),)),
        ("Fixes ENG-142 at last", (("ENG", 142),)),
        ("eng-142-fix-slack-oauth", (("ENG", 142),)),
        ("feature/ENG-142", (("ENG", 142),)),
        ("[ENG-142] title", (("ENG", 142),)),
        # Several, in the order they appear, because "Fixes ENG-1, ENG-2" is
        # the case github_pull_request_issues exists as a join table for.
        ("Fixes ENG-1, closes CORE-2", (("ENG", 1), ("CORE", 2))),
        # De-duplicated: a repeat is one request, not a primary key violation.
        ("ENG-1 and again ENG-1", (("ENG", 1),)),
        # Case-folded, because a branch is conventionally lowercase and names
        # the same issue.
        ("eng-142", (("ENG", 142),)),
        # Not identifiers.
        ("", ()),
        ("no identifier here", ()),
        # The lookbehind: a key cannot start mid-word. `XENG` is itself a
        # legal team key, so the case has to be a run too long to be one.
        ("ABCDEFGHIJKLM-142", ()),
        ("1ENG-142", ()),
        ("ENG-", ()),
        ("-142", ()),
        ("2024-142", ()),  # a key must start with a letter
        ("TOOLONGAKEY-1", ()),  # teams_key_format allows ten characters
        ("ENG-" + "9" * 19, ()),  # longer than BIGINT, so not a number to bind
    ],
)
def test_what_counts_as_an_identifier(text, expected):
    assert issue_identifiers(text) == expected


def test_a_longer_number_wins_over_its_own_prefix():
    """`ENG-1425` must not also offer `ENG-142`.

    Without the trailing lookahead it would, and the extra link would be onto
    a real, unrelated issue -- which is the worst kind of false positive this
    parser can produce.
    """
    assert issue_identifiers("ENG-1425") == (("ENG", 1425),)


def test_a_hostile_body_cannot_ask_for_unbounded_work():
    """Both bounds, from one input.

    The text is written by whoever opened the pull request, so the scan is
    capped and the number of distinct identifiers is capped -- each one
    becomes an array element and a row, and a body listing ten thousand of
    them is a write amplification with a signature on it.
    """
    found = issue_identifiers(" ".join(f"ENG-{n}" for n in range(1, 5000)))

    assert len(found) == 20


# --- the display state ------------------------------------------------


@pytest.mark.parametrize(
    ("state", "draft", "merged_at", "expected"),
    [
        ("open", False, None, "open"),
        ("open", True, None, "draft"),
        ("closed", False, None, "closed"),
        # What GitHub actually sends for a merge: closed, with an instant.
        ("closed", False, instant(LATER), "merged"),
        # `draft` is NOT cleared when a draft is closed, so this is the
        # ordering that is observable and the one that gets chosen wrong.
        # Answering DRAFT here would say work is in progress on a pull request
        # nobody is going to finish.
        ("closed", True, None, "closed"),
        ("closed", True, instant(LATER), "merged"),
    ],
)
def test_the_display_state_is_derived_from_githubs_three_columns(
    state, draft, merged_at, expected
):
    assert (
        pull_request_display_state(state=state, draft=draft, merged_at=merged_at)
        == expected
    )


def test_the_link_source_enum_matches_the_domain_vocabulary():
    """Three copies exist -- the CHECK in 017, the domain tuple and the
    GraphQL enum -- and two of the three are pinned equal here."""
    assert tuple(source.value for source in GithubLinkSourceType) == LINK_SOURCES


# --- the cross-tenant refusal -----------------------------------------


async def test_a_pull_request_titled_with_another_workspaces_identifier_links_to_nothing():
    """The attack migrations/017_github_development.sql was written for.

    Someone opens a pull request on a repository their OWN workspace has
    connected -- so the delivery is genuine, signed, and routes correctly --
    and titles it with an identifier belonging to another tenant. A resolver
    that forgot to scope its lookup would hand back that tenant's issue id,
    and the link row would put a private issue into a Development panel.

    The assertion is that nothing is written, and the stronger claim behind it
    is that nothing was even found: the resolution ran under this workspace's
    scope, so `OTH-9` resolved to no id at all. 017's composite foreign keys
    are the floor under that rather than the mechanism.
    """
    service, repository = development_service(
        # The other workspace's issue is REAL, and the fake holds it. A
        # fixture where the target did not exist would pass this test for a
        # reason that says nothing about scoping.
        issues={(OTHER_WORKSPACE_ID, "OTH", 9): THEIR_ISSUE_ID}
    )

    await service.apply_webhook(
        event="pull_request",
        payload=pull_payload(title="Fixes OTH-9", body="also OTH-9", head_ref="oth-9"),
        delivery_id="d-1",
    )

    assert repository.pull_links == set()

    # And the lookup it made was scoped to the delivery's own workspace, with
    # no second workspace anywhere in the call.
    assert [call[1] for call in repository.called("resolve_issue_ids")] == [
        WORKSPACE_ID
    ]


async def test_a_delivery_for_a_repository_this_workspace_does_not_have_writes_nothing():
    """A repository id the installation does not cover.

    `github_pull_requests_repository_fk` would refuse the row, but a foreign
    key violation aborts the delivery's transaction and reaches GitHub as a
    500 -- which it answers by redelivering, forever. So the check happens
    first and the delivery is dropped in silence.
    """
    service, repository = development_service(issues=ours())

    await service.apply_webhook(
        event="pull_request",
        payload=pull_payload(title="Fixes ENG-142", repository_id=OTHER_REPOSITORY_ID),
        delivery_id="d-1",
    )

    assert repository.pull_requests == {}
    assert repository.called("upsert_pull_request") == []


async def test_a_delivery_for_an_unconfirmed_installation_writes_nothing():
    """A `pull_request` cannot confirm a claim, and cannot be applied under one.

    `_resolve_workspace` only promotes a claim from an `installation` event,
    so a development delivery for a workspace GitHub has not vouched for
    resolves to no workspace and is dropped. Otherwise an attacker's claim on
    somebody else's installation would start collecting that organisation's
    pull request titles.
    """
    service, repository = development_service(issues=ours(), confirmed=False)

    await service.apply_webhook(
        event="pull_request",
        payload=pull_payload(title="Fixes ENG-142"),
        delivery_id="d-1",
    )

    assert repository.pull_requests == {}
    assert repository.confirmed is False


# --- delivery idempotency ---------------------------------------------


async def test_a_redelivered_push_is_applied_once():
    """GitHub retries anything it did not see a 2xx for.

    The check runs as the first statement of the delivery's transaction --
    before the routing lookup, and therefore before anything is applied -- so
    the second attempt does no work at all rather than doing work that has to
    be idempotent statement by statement.
    """
    service, repository = development_service(issues=ours())
    payload = push_payload()

    await service.apply_webhook(event="push", payload=payload, delivery_id="d-1")

    applied = list(repository.calls)

    await service.apply_webhook(event="push", payload=payload, delivery_id="d-1")

    assert repository.calls[len(applied) :] == [("record_delivery", "d-1", "push")]
    assert len(repository.commits) == 1


async def test_a_second_delivery_with_a_different_id_is_applied():
    """The guard is the id and not the payload: two genuine events for one
    pull request carry two ids and both have to land."""
    service, repository = development_service(issues=ours())

    await service.apply_webhook(
        event="pull_request",
        payload=pull_payload(title="Fixes ENG-142"),
        delivery_id="d-1",
    )
    await service.apply_webhook(
        event="pull_request",
        payload=pull_payload(action="closed", state="closed", title="Fixes ENG-142"),
        delivery_id="d-2",
    )

    stored = repository.pull_requests[(WORKSPACE_ID, REPOSITORY_ID, PULL_NUMBER)]

    assert stored["state"] == "closed"


async def test_a_delivery_with_no_id_is_applied_unguarded():
    """Absent rather than refused.

    A header this server did not get is not a reason to drop a signed
    delivery; unguarded is exactly as safe as every delivery was before
    `github_deliveries` existed.
    """
    service, repository = development_service(issues=ours())

    await service.apply_webhook(event="push", payload=push_payload(), delivery_id=None)

    assert len(repository.commits) == 1
    assert repository.called("record_delivery") == []


async def test_an_over_long_delivery_id_does_not_take_the_delivery_down():
    """github_deliveries_delivery_id_length is a CHECK, and a CHECK violation
    inside the transaction is a 500 -- which is an infinite redelivery loop
    for a header this server merely did not like. It is ignored instead."""
    service, repository = development_service(issues=ours())

    await service.apply_webhook(
        event="push",
        payload=push_payload(),
        delivery_id="d" * (DELIVERY_ID_MAX_LENGTH + 1),
    )

    assert len(repository.commits) == 1
    assert repository.called("record_delivery") == []


async def test_an_unhandled_event_is_not_even_recorded():
    """The event filter runs before a connection is acquired, so a GitHub
    App's default subscription list costs no database work at all."""
    service, repository = development_service()

    await service.apply_webhook(
        event="check_run",
        payload={"installation": {"id": INSTALLATION_ID}},
        delivery_id="d-1",
    )

    assert repository.calls == []


# --- the pull-request lifecycle ---------------------------------------


async def test_an_opened_pull_request_is_stored_and_linked_from_every_source():
    service, repository = development_service(issues=ours())

    await service.apply_webhook(
        event="pull_request",
        payload=pull_payload(
            title="Fixes ENG-142",
            body="More about ENG-142",
            head_ref="eng-142-fix-oauth",
        ),
        delivery_id="d-1",
    )

    stored = repository.pull_requests[(WORKSPACE_ID, REPOSITORY_ID, PULL_NUMBER)]

    assert stored["title"] == "Fixes ENG-142"
    assert stored["state"] == "open"
    assert stored["head_ref"] == "eng-142-fix-oauth"
    assert stored["github_updated_at"] == instant(LATER)
    assert links(repository) == {
        (OUR_ISSUE_ID, "title"),
        (OUR_ISSUE_ID, "body"),
        (OUR_ISSUE_ID, "branch"),
    }


@pytest.mark.parametrize(
    ("action", "state", "draft", "merged_at", "expected"),
    [
        ("opened", "open", False, None, "open"),
        ("opened", "open", True, None, "draft"),
        ("converted_to_draft", "open", True, None, "draft"),
        ("ready_for_review", "open", False, None, "open"),
        ("reopened", "open", False, None, "open"),
        ("closed", "closed", False, None, "closed"),
        ("closed", "closed", False, LATER, "merged"),
    ],
)
async def test_every_lifecycle_action_stores_what_github_sent(
    action, state, draft, merged_at, expected
):
    """One code path for all of them, because each action carries the WHOLE
    pull request object rather than a diff -- so there is nothing to branch on
    and nothing for a missing branch to get wrong."""
    service, repository = development_service()

    await service.apply_webhook(
        event="pull_request",
        payload=pull_payload(
            action=action, state=state, draft=draft, merged_at=merged_at
        ),
        delivery_id=f"d-{action}-{state}-{draft}-{merged_at}",
    )

    stored = repository.pull_requests[(WORKSPACE_ID, REPOSITORY_ID, PULL_NUMBER)]

    assert (
        pull_request_display_state(
            state=stored["state"],
            draft=stored["draft"],
            merged_at=stored["merged_at"],
        )
        == expected
    )


async def test_a_merged_at_on_an_open_pull_request_is_dropped():
    """github_pull_requests_merged_is_closed refuses that combination.

    Storing it would abort the delivery on the CHECK, so it is cleared here --
    and the derived display state then cannot answer something no payload
    said.
    """
    service, repository = development_service()

    await service.apply_webhook(
        event="pull_request",
        payload=pull_payload(state="open", merged_at=LATER),
        delivery_id="d-1",
    )

    assert (
        repository.pull_requests[(WORKSPACE_ID, REPOSITORY_ID, PULL_NUMBER)][
            "merged_at"
        ]
        is None
    )


@pytest.mark.parametrize(
    "pull",
    [
        None,
        {},
        {"number": 0, "title": "x", "state": "open"},
        {"number": 1, "title": "", "state": "open"},
        {"number": 1, "title": "x", "state": "merged"},  # not a state GitHub has
        {"number": 1, "title": "x", "state": None},
    ],
)
async def test_a_pull_request_payload_that_is_not_one_writes_nothing(pull):
    service, repository = development_service()

    payload = pull_payload()
    payload["pull_request"] = pull

    await service.apply_webhook(
        event="pull_request", payload=payload, delivery_id="d-1"
    )

    assert repository.called("upsert_pull_request") == []


async def test_a_head_ref_that_the_column_would_refuse_is_dropped_not_stored():
    """github_pull_requests_head_ref_format is a CHECK, and a CHECK violation
    is a redelivery loop. The pull request is still stored -- losing the whole
    delivery over a branch name is the worse answer."""
    service, repository = development_service(issues=ours())

    await service.apply_webhook(
        event="pull_request",
        payload=pull_payload(title="Fixes ENG-142", head_ref="a branch with spaces"),
        delivery_id="d-1",
    )

    stored = repository.pull_requests[(WORKSPACE_ID, REPOSITORY_ID, PULL_NUMBER)]

    assert stored["head_ref"] is None
    assert links(repository, "branch") == set()
    assert links(repository, "title") == {(OUR_ISSUE_ID, "title")}


# --- editing, and what an edit may retract ----------------------------


async def test_editing_a_title_retracts_only_what_the_title_supported():
    """`source` is inside github_pull_request_issues_pkey for this.

    The pull request was linked twice to one issue -- once by its title, once
    by its branch. The title is edited to drop the identifier; the branch has
    not changed. The title's link goes and the branch's stays, which is what a
    single link row per (pull request, issue) could not express.
    """
    service, repository = development_service(issues=ours())

    await service.apply_webhook(
        event="pull_request",
        payload=pull_payload(title="Fixes ENG-142", head_ref="eng-142-fix"),
        delivery_id="d-1",
    )

    assert links(repository) == {
        (OUR_ISSUE_ID, "title"),
        (OUR_ISSUE_ID, "branch"),
    }

    await service.apply_webhook(
        event="pull_request",
        payload=pull_payload(
            action="edited",
            title="Fix the thing",
            head_ref="eng-142-fix",
            updated_at="2026-04-01T11:00:00Z",
        ),
        delivery_id="d-2",
    )

    assert links(repository) == {(OUR_ISSUE_ID, "branch")}


async def test_an_edit_can_add_a_link_as_well_as_remove_one():
    service, repository = development_service(
        issues=ours() | ours(number=7, issue_id=SECOND_ISSUE_ID)
    )

    await service.apply_webhook(
        event="pull_request",
        payload=pull_payload(title="Fixes ENG-142"),
        delivery_id="d-1",
    )
    await service.apply_webhook(
        event="pull_request",
        payload=pull_payload(
            action="edited",
            title="Fixes ENG-7",
            updated_at="2026-04-01T11:00:00Z",
        ),
        delivery_id="d-2",
    )

    assert links(repository) == {(SECOND_ISSUE_ID, "title")}


async def test_a_body_edit_does_not_disturb_the_title():
    service, repository = development_service(issues=ours())

    await service.apply_webhook(
        event="pull_request",
        payload=pull_payload(title="Fixes ENG-142", body="and ENG-142 again"),
        delivery_id="d-1",
    )
    await service.apply_webhook(
        event="pull_request",
        payload=pull_payload(
            action="edited",
            title="Fixes ENG-142",
            body="nothing here now",
            updated_at="2026-04-01T11:00:00Z",
        ),
        delivery_id="d-2",
    )

    assert links(repository) == {(OUR_ISSUE_ID, "title")}


# --- out-of-order redelivery ------------------------------------------


async def test_an_out_of_order_redelivery_does_not_regress_the_state():
    """Arrival order is not a fact about the pull request.

    GitHub queues, retries and redelivers, so the payload that arrives second
    is routinely the older one. `github_updated_at` is what decides, and the
    older payload here would otherwise reopen a merged pull request and put
    its old title back.
    """
    service, repository = development_service()

    await service.apply_webhook(
        event="pull_request",
        payload=pull_payload(
            action="closed",
            state="closed",
            merged_at=LATER,
            title="Fix the callback",
            updated_at=LATER,
        ),
        delivery_id="d-later",
    )
    await service.apply_webhook(
        event="pull_request",
        payload=pull_payload(
            action="opened",
            state="open",
            title="WIP",
            updated_at=EARLIER,
        ),
        delivery_id="d-earlier",
    )

    stored = repository.pull_requests[(WORKSPACE_ID, REPOSITORY_ID, PULL_NUMBER)]

    assert stored["state"] == "closed"
    assert stored["merged_at"] == instant(LATER)
    assert stored["title"] == "Fix the callback"


async def test_a_stale_redelivery_re_derives_no_links():
    """The second half of the same defence.

    Links come from the title, so applying them from a payload too old to
    store would retract, from an older title, links the current title still
    supports.
    """
    service, repository = development_service(issues=ours())

    await service.apply_webhook(
        event="pull_request",
        payload=pull_payload(title="Fixes ENG-142", updated_at=LATER),
        delivery_id="d-later",
    )
    await service.apply_webhook(
        event="pull_request",
        payload=pull_payload(title="No identifier", updated_at=EARLIER),
        delivery_id="d-earlier",
    )

    assert links(repository) == {(OUR_ISSUE_ID, "title")}


async def test_a_payload_with_no_timestamp_does_not_overwrite_one_that_has():
    service, repository = development_service()

    await service.apply_webhook(
        event="pull_request",
        payload=pull_payload(title="Known age", updated_at=LATER),
        delivery_id="d-1",
    )
    await service.apply_webhook(
        event="pull_request",
        payload=pull_payload(title="Unknown age", updated_at=None),
        delivery_id="d-2",
    )

    assert (
        repository.pull_requests[(WORKSPACE_ID, REPOSITORY_ID, PULL_NUMBER)]["title"]
        == "Known age"
    )


# --- pushes -----------------------------------------------------------


async def test_a_push_records_its_commits_and_links_what_they_name():
    service, repository = development_service(issues=ours())

    await service.apply_webhook(event="push", payload=push_payload(), delivery_id="d-1")

    stored = repository.commits[(WORKSPACE_ID, REPOSITORY_ID, SHA)]

    assert stored["message"] == "ENG-142 handle the redirect"
    assert stored["committed_at"] == instant(LATER)
    assert repository.commit_links == {(WORKSPACE_ID, REPOSITORY_ID, SHA, OUR_ISSUE_ID)}


async def test_a_commit_message_naming_another_workspaces_issue_links_to_nothing():
    """The same attack through a push, refused the same way and asserted
    separately: two tables, two constraints, two resolutions."""
    service, repository = development_service(
        issues={(OTHER_WORKSPACE_ID, "OTH", 9): THEIR_ISSUE_ID}
    )

    await service.apply_webhook(
        event="push",
        payload=push_payload(
            commits=[{"id": SHA, "message": "OTH-9 fix", "timestamp": LATER}]
        ),
        delivery_id="d-1",
    )

    assert repository.commit_links == set()
    assert len(repository.commits) == 1


async def test_the_head_commit_is_read_alongside_the_list():
    """GitHub caps `commits` at twenty, so on a large push the tip -- where a
    merge commit's "Fixes ENG-142" lives -- is in `head_commit` and nowhere
    else."""
    service, repository = development_service(issues=ours())

    await service.apply_webhook(
        event="push",
        payload=push_payload(
            commits=[{"id": SHA, "message": "no identifier", "timestamp": LATER}],
            head_commit={
                "id": OTHER_SHA,
                "message": "Merge pull request: Fixes ENG-142",
                "timestamp": LATER,
            },
        ),
        delivery_id="d-1",
    )

    assert len(repository.commits) == 2
    assert repository.commit_links == {
        (WORKSPACE_ID, REPOSITORY_ID, OTHER_SHA, OUR_ISSUE_ID)
    }


async def test_a_head_commit_already_in_the_list_is_one_commit():
    service, repository = development_service()
    commit = {"id": SHA, "message": "one commit", "timestamp": LATER}

    await service.apply_webhook(
        event="push",
        payload=push_payload(commits=[commit], head_commit=commit),
        delivery_id="d-1",
    )

    assert len(repository.commits) == 1


@pytest.mark.parametrize(
    "commit",
    [
        "not-a-commit",
        {},
        {"id": "a39fd12", "message": "abbreviated"},  # 7 characters, not 40
        {"id": "z" * 40, "message": "not hex"},
        {"id": SHA.upper() + "!", "message": "not a sha"},
        {"id": SHA, "message": ""},
        {"id": SHA, "message": None},
    ],
)
async def test_a_malformed_commit_is_skipped_not_fatal(commit):
    """The payload is proven to be GitHub's, so a bad entry is a schema change
    rather than an attack -- and raising would fail the whole delivery, which
    GitHub then retries, which fails again."""
    service, repository = development_service()

    await service.apply_webhook(
        event="push",
        payload=push_payload(commits=[commit]),
        delivery_id="d-1",
    )

    assert repository.commits == {}


async def test_an_uppercase_sha_is_stored_in_the_case_the_column_keys_on():
    """git and GitHub both accept either case; github_commits_sha_format
    accepts one."""
    service, repository = development_service()

    await service.apply_webhook(
        event="push",
        payload=push_payload(
            commits=[{"id": SHA.upper(), "message": "shouted", "timestamp": LATER}]
        ),
        delivery_id="d-1",
    )

    assert (WORKSPACE_ID, REPOSITORY_ID, SHA) in repository.commits


async def test_a_push_with_no_usable_commit_asks_the_database_nothing():
    """A branch deletion sends an empty list, and it is the ordinary case."""
    service, repository = development_service()

    await service.apply_webhook(
        event="push",
        payload=push_payload(commits=[]),
        delivery_id="d-1",
    )

    assert repository.called("repository_exists") == []
    assert repository.called("resolve_issue_ids") == []


async def test_a_push_reporting_more_commits_than_the_cap_is_bounded():
    service, repository = development_service()

    await service.apply_webhook(
        event="push",
        payload=push_payload(
            commits=[
                {"id": f"{index:040x}", "message": f"commit {index}"}
                for index in range(1, COMMITS_PER_PUSH_LIMIT + 30)
            ]
        ),
        delivery_id="d-1",
    )

    assert len(repository.commits) == COMMITS_PER_PUSH_LIMIT


# --- disconnection and removal ----------------------------------------


async def test_an_uninstall_removes_the_development_history_children_first():
    """RESTRICT everywhere, so the order is the correctness.

    The app is gone from GitHub, so Vector must not go on holding the pull
    request titles and commit messages it collected while it had access.
    """
    service, repository = development_service(issues=ours())

    await service.apply_webhook(
        event="pull_request",
        payload=pull_payload(title="Fixes ENG-142"),
        delivery_id="d-1",
    )
    await service.apply_webhook(
        event="installation",
        payload={"action": "deleted", "installation": {"id": INSTALLATION_ID}},
        delivery_id="d-2",
    )

    assert repository.pull_requests == {}
    assert repository.pull_links == set()
    assert repository.repositories == []
    assert repository.installation is None

    ordered = [call[0] for call in repository.calls]
    assert ordered[-3:] == [
        "delete_development",
        "delete_repositories",
        "delete_installation",
    ]


async def test_a_repository_removed_from_the_installation_takes_its_history():
    """The access-changed case that actually fires.

    An organisation narrowing an installation's repository list sends
    `installation_repositories.removed` and nothing else. The app can no
    longer read that repository, so Vector must not keep showing what it read.
    """
    service, repository = development_service(issues=ours())

    await service.apply_webhook(
        event="pull_request",
        payload=pull_payload(title="Fixes ENG-142"),
        delivery_id="d-1",
    )
    await service.apply_webhook(
        event="installation_repositories",
        payload={
            "action": "removed",
            "installation": {"id": INSTALLATION_ID, "account": {"login": "acme"}},
            "repositories_removed": [{"id": REPOSITORY_ID, "full_name": "acme/vector"}],
        },
        delivery_id="d-2",
    )

    assert repository.pull_requests == {}
    assert repository.pull_links == set()
    assert repository.repositories == []


async def test_removing_one_repository_leaves_anothers_history_alone():
    service, repository = development_service(
        issues=ours(),
        repositories=[
            GithubRepositoryEntity(repository_id=REPOSITORY_ID, full_name="acme/one"),
            GithubRepositoryEntity(
                repository_id=OTHER_REPOSITORY_ID, full_name="acme/two"
            ),
        ],
    )

    for index, repository_id in enumerate((REPOSITORY_ID, OTHER_REPOSITORY_ID)):
        await service.apply_webhook(
            event="pull_request",
            payload=pull_payload(
                title="Fixes ENG-142",
                repository_id=repository_id,
                number=PULL_NUMBER + index,
            ),
            delivery_id=f"d-{index}",
        )

    await service.apply_webhook(
        event="installation_repositories",
        payload={
            "action": "removed",
            "installation": {"id": INSTALLATION_ID, "account": {"login": "acme"}},
            "repositories_removed": [{"id": REPOSITORY_ID, "full_name": "acme/one"}],
        },
        delivery_id="d-remove",
    )

    assert set(repository.pull_requests) == {
        (WORKSPACE_ID, OTHER_REPOSITORY_ID, PULL_NUMBER + 1)
    }
    assert {link[1] for link in repository.pull_links} == {OTHER_REPOSITORY_ID}


async def test_disconnecting_removes_the_development_history_too():
    service, repository = development_service(issues=ours())

    await service.apply_webhook(
        event="pull_request",
        payload=pull_payload(title="Fixes ENG-142"),
        delivery_id="d-1",
    )
    await service.disconnect(
        SimpleNamespace(workspace_id=WORKSPACE_ID, user_id=UUID(int=1), role="admin")
    )

    assert repository.pull_requests == {}
    assert repository.pull_links == set()


# --- reading it back --------------------------------------------------


async def test_the_development_read_is_scoped_to_the_callers_workspace():
    """An issue id from another workspace answers with two empty lists.

    The same answer an issue with no activity gets, which is what stops this
    confirming that a leaked id is real.
    """
    service, repository = development_service(issues=ours())

    await service.apply_webhook(
        event="pull_request",
        payload=pull_payload(title="Fixes ENG-142", head_ref="eng-142-fix"),
        delivery_id="d-1",
    )
    await service.apply_webhook(event="push", payload=push_payload(), delivery_id="d-2")

    mine = await service.development_for_issue(
        SCOPE, issue_id=OUR_ISSUE_ID, identifier="ENG-142", title="Fix OAuth"
    )
    theirs = await service.development_for_issue(
        WorkspaceScope(workspace_id=OTHER_WORKSPACE_ID),
        issue_id=OUR_ISSUE_ID,
        identifier="ENG-142",
        title="Fix OAuth",
    )

    assert len(mine.pull_requests) == 1
    assert mine.pull_requests[0].link_sources == ("branch", "title")
    assert mine.pull_requests[0].display_state == "open"
    assert len(mine.commits) == 1
    assert mine.commits[0].short_sha == SHA[:7]

    assert theirs.pull_requests == ()
    assert theirs.commits == ()


async def test_an_issue_with_no_activity_still_gets_its_branch_name():
    """The panel's first job on an empty issue is to offer the branch."""
    service, _ = development_service()

    view = await service.development_for_issue(
        SCOPE,
        issue_id=OUR_ISSUE_ID,
        identifier="ENG-142",
        title="Fix Slack OAuth callback",
    )

    assert view.branch_name == "eng-142-fix-slack-oauth-callback"
    assert view.pull_requests == ()
    assert view.commits == ()


# --- the GraphQL boundary ---------------------------------------------


DEVELOPMENT_QUERY = """
query IssueDevelopment($slug: String!, $id: UUID!) {
  issue(workspaceSlug: $slug, id: $id) {
    identifier

    development {
      branchName

      pullRequests {
        repository
        number
        title
        state
        branch
        url
        linkedBy
      }

      commits {
        sha
        shortSha
        summary
        url
      }
    }
  }
}
"""


class FakeIssueService:
    """Answers `issue(workspaceSlug:, id:)` with one entity, or nothing."""

    def __init__(self, entity=None):
        self.entity = entity
        self.calls: list[dict] = []

    async def get_by_id(self, *, scope, issue_id):
        self.calls.append({"scope": scope, "issue_id": issue_id})

        return self.entity


async def test_the_development_section_is_reachable_from_the_issue():
    """Hung off `Issue` rather than published as a root field.

    That is the authorization decision: the scope is the one the root resolver
    already authorised the issue under, so there is no second boundary and no
    `issueId` argument for a caller to aim at another workspace.
    """
    service, _ = development_service(
        issues={(TEST_WORKSPACE_ID, "ENG", 142): UUID(int=3)}
    )

    await service.apply_webhook(
        event="pull_request",
        payload=pull_payload(title="Fixes ENG-142", head_ref="eng-142-fix"),
        delivery_id="d-1",
    )

    result = await schema.execute(
        DEVELOPMENT_QUERY,
        variable_values={"slug": TEST_WORKSPACE_SLUG, "id": str(UUID(int=3))},
        context_value=graphql_context(
            issue_service=FakeIssueService(
                make_entity(3, number=142, title="Fix Slack OAuth callback")
            ),
            github_service=service,
        ),
    )

    assert result.errors is None

    development = result.data["issue"]["development"]

    assert development["branchName"] == "eng-142-fix-slack-oauth-callback"
    assert development["pullRequests"] == []
    assert development["commits"] == []


async def test_the_panel_renders_what_a_delivery_wrote():
    """End to end through the schema, with the service the webhook wrote to.

    The workspace is `TEST_WORKSPACE_ID` on both sides, because that is what
    `FakeMembershipService` resolves the slug to -- the point being that the
    resolver passes the scope it was authorised with rather than one the
    document chose.
    """
    issue_id = UUID(int=3)
    service, repository = build_service(workspace_id=TEST_WORKSPACE_ID)
    repository.repositories = [
        GithubRepositoryEntity(repository_id=REPOSITORY_ID, full_name="acme/vector")
    ]
    repository.issues = {(TEST_WORKSPACE_ID, "ENG", 142): issue_id}

    await service.apply_webhook(
        event="pull_request",
        payload=pull_payload(
            action="closed",
            title="Fixes ENG-142",
            head_ref="eng-142-fix",
            state="closed",
            merged_at=LATER,
        ),
        delivery_id="d-1",
    )
    await service.apply_webhook(event="push", payload=push_payload(), delivery_id="d-2")

    result = await schema.execute(
        DEVELOPMENT_QUERY,
        variable_values={"slug": TEST_WORKSPACE_SLUG, "id": str(issue_id)},
        context_value=graphql_context(
            issue_service=FakeIssueService(make_entity(3, number=142)),
            github_service=service,
        ),
    )

    assert result.errors is None

    development = result.data["issue"]["development"]
    (pull,) = development["pullRequests"]

    assert pull["repository"] == "acme/vector"
    assert pull["number"] == PULL_NUMBER
    assert pull["state"] == GithubPullRequestStateType.MERGED.name
    assert pull["branch"] == "eng-142-fix"
    assert pull["url"].endswith(f"/pull/{PULL_NUMBER}")
    assert pull["linkedBy"] == [
        GithubLinkSourceType.BRANCH.name,
        GithubLinkSourceType.TITLE.name,
    ]

    (commit,) = development["commits"]

    assert commit["sha"] == SHA
    assert commit["shortSha"] == SHA[:7]
    assert commit["summary"] == "ENG-142 handle the redirect"


async def test_the_development_section_needs_no_admin_role():
    """`githubIntegration` is admin-only; this is not, and the difference is
    the point. Connecting an organisation is an admin's act; the pull requests
    attached to an issue are part of the issue.

    `TEST_AUTHORIZED_SCOPE` carries role="member", so this asserts the absence
    of a role check rather than merely not exercising one.
    """
    assert TEST_AUTHORIZED_SCOPE.role == "member"

    service, _ = development_service()
    result = await schema.execute(
        DEVELOPMENT_QUERY,
        variable_values={"slug": TEST_WORKSPACE_SLUG, "id": str(UUID(int=3))},
        context_value=graphql_context(
            issue_service=FakeIssueService(make_entity(3)),
            github_service=service,
        ),
    )

    assert result.errors is None
    assert result.data["issue"]["development"]["branchName"]


SLICED_QUERY = """
query Sliced($slug: String!, $id: UUID!, $first: Int!) {
  issue(workspaceSlug: $slug, id: $id) {
    development {
      pullRequests(first: $first) { number }
      commits(first: $first) { sha }
    }
  }
}
"""


@pytest.mark.parametrize(
    ("first", "expected"),
    [
        (0, 0),
        (1, 1),
        (10, 1),
        # Clamped rather than refused: the lists are already in memory and
        # already capped by the service, so there is nothing to refuse.
        (10_000, 1),
        # A negative would otherwise slice from the END, silently answering
        # with the oldest activity instead of the newest.
        (-5, 0),
    ],
)
async def test_first_slices_the_panel_rather_than_refusing_it(first, expected):
    issue_id = UUID(int=3)
    service, repository = build_service(workspace_id=TEST_WORKSPACE_ID)
    repository.repositories = [
        GithubRepositoryEntity(repository_id=REPOSITORY_ID, full_name="acme/vector")
    ]
    repository.issues = {(TEST_WORKSPACE_ID, "ENG", 142): issue_id}

    await service.apply_webhook(
        event="pull_request",
        payload=pull_payload(title="Fixes ENG-142"),
        delivery_id="d-1",
    )
    await service.apply_webhook(event="push", payload=push_payload(), delivery_id="d-2")

    result = await schema.execute(
        SLICED_QUERY,
        variable_values={
            "slug": TEST_WORKSPACE_SLUG,
            "id": str(issue_id),
            "first": first,
        },
        context_value=graphql_context(
            issue_service=FakeIssueService(make_entity(3, number=142)),
            github_service=service,
        ),
    )

    assert result.errors is None

    development = result.data["issue"]["development"]

    assert len(development["pullRequests"]) == expected
    assert len(development["commits"]) == expected


def test_both_development_lists_declare_a_page_size():
    """`app.graphql.limits` prices a composite field as `page_size *
    inner_complexity`, and reads that page size from a `first` argument. A
    list field without one is charged as though it returned a single row --
    which is how this panel, selected under a hundred issues, would measure as
    cheap while returning five thousand pull requests.

    Asserted against the schema rather than against the resolver, because the
    validation rule reads the schema.
    """
    development = schema.schema_converter.type_map["GithubDevelopment"]

    for name in ("pullRequests", "commits"):
        declared = development.implementation.fields[name].args["first"]

        assert declared.default_value in PAGE_SIZE_ARGUMENT_DEFAULTS


# The default the schema declares for both lists, which is what the
# complexity rule charges when a document omits `first`.
PAGE_SIZE_ARGUMENT_DEFAULTS = (DEFAULT_DEVELOPMENT_FIRST,)


def test_the_development_types_expose_only_facts_about_the_activity():
    """The field list, pinned, so adding one is a deliberate act.

    The same guard tests/test_github.py puts on `GithubIntegration`, and it
    matters more here: these three types are built from a payload somebody
    else wrote, so a field added carelessly publishes whatever that payload
    happened to carry.
    """
    fields = {
        name
        for type_name in ("GithubPullRequest", "GithubCommit", "GithubDevelopment")
        for name in schema.schema_converter.type_map[
            type_name
        ].implementation.fields.keys()
    }

    assert fields == {
        "repositoryId",
        "repository",
        "number",
        "title",
        "state",
        "branch",
        "url",
        "mergedAt",
        "updatedAt",
        "linkedBy",
        "sha",
        "shortSha",
        "message",
        "summary",
        "committedAt",
        "branchName",
        "pullRequests",
        "commits",
    }


def test_a_delivery_that_arrives_a_year_late_is_still_ordered_by_github():
    """A property rather than a case: the stored instant is the payload's, so
    a comparison against it is a comparison of GitHub's clock with itself
    rather than of two machines'."""
    earlier = instant(EARLIER)

    assert earlier < earlier + timedelta(hours=1)
    assert earlier.tzinfo is timezone.utc
