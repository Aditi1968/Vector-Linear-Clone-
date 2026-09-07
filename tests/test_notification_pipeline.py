"""The notification pipeline's properties that need no database.

Three groups, and the first is the one worth reading.

THE SEPARATION IS PROVEN, NOT PROMISED. The whole point of routing writes
through `domain_events` is that a write path cannot reach Slack: not the GitHub
webhook handler, not the issue services, not the project service. A comment
saying so decays; an import added on a busy afternoon does not announce itself.
So `test_no_write_path_can_reach_slack` walks the real import graph of this
application, transitively, from each of those roots and asserts that no Slack
module is reachable at all. If somebody wires a post into a webhook handler,
this fails before the code review does.

THE TWO VOCABULARIES AGREE. `DomainEventKind` and `SLACK_NOTIFICATION_EVENTS`
are deliberately two tuples in two modules that do not import each other -- the
whole reason the event vocabulary is separate is so a write path never imports
the Slack domain -- and they have to stay spelled identically, because a
preference lookup is `kind = event`. Divergence is silent: the event is
emitted, no preference row can enable it, and it is skipped forever.

THE MESSAGE READS PROPERLY. Compact, with a deep link, and without a fabricated
one when the deployment has not said where it lives.

`tests/test_notification_pipeline_db.py` is the other half, and it is where the
end-to-end claims live: a redelivery posting once, a disabled preference posting
nothing, a refusal recorded as a failure, and one workspace's event being unable
to reach another's channel.
"""

import ast
from pathlib import Path
from uuid import UUID

import pytest

from app.domain.events import (
    HEADLINES,
    NO_INSTALLATION,
    PREFERENCE_DISABLED,
    DeliveryState,
    DomainEventEntity,
    DomainEventKind,
    message_for,
)
from app.domain.slack import SLACK_FAILURES, SLACK_NOTIFICATION_EVENTS
from app.services.events import (
    UPDATE_SUMMARY_FALLBACK,
    UPDATE_SUMMARY_MAX_LENGTH,
    _update_summary,
)


APP_ROOT = Path(__file__).resolve().parents[1] / "app"

# Every module a write path must not be able to reach, however indirectly.
#
# All three, and not just the service. The domain module carries the failure
# vocabulary and the repository carries the token reference, so a write path
# that imported either would already be a write path with an opinion about
# Slack -- and would be one import away from the client.
SLACK_MODULES = frozenset(
    {
        "app.services.slack",
        "app.domain.slack",
        "app.repositories.slack",
        "app.rest.slack",
    }
)

# The modules that WRITE, from which no Slack module may be reachable.
#
# `app.services.github` is the one the brief names, and it is the sharpest
# case: a webhook handler that could post directly would bypass the preference
# toggle, post twice on a redelivery, report nothing when it failed, and make
# the HTTP call inside an open transaction holding a pool connection. The other
# three are here because the property is about write paths and not about
# GitHub -- a rule that held for one module and not its neighbours would be a
# rule nobody could state.
WRITE_PATHS = (
    "app.services.github",
    "app.rest.github",
    "app.services.activity",
    "app.services.projects",
    "app.services.issues",
    "app.services.events",
)


def _module_name(path: Path) -> str:
    """`app/services/github.py` -> `app.services.github`."""
    relative = path.relative_to(APP_ROOT.parent).with_suffix("")

    parts = relative.parts

    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def _imports(path: Path) -> set[str]:
    """Every `app.*` module this file imports, by name.

    Read from the AST rather than by importing anything. Importing would run
    module-level code and -- worse for this test -- would resolve
    `TYPE_CHECKING` blocks and conditional imports differently from how the
    application does, which is exactly the kind of gap a security-shaped
    assertion must not have.

    A `from app.x import y` is recorded as both `app.x` and `app.x.y`, because
    the name after `import` may be a submodule or a symbol and this cannot tell
    which without resolving it. The caller keeps only the names that are real
    modules.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
            found.update(f"{node.module}.{alias.name}" for alias in node.names)

    return {name for name in found if name == "app" or name.startswith("app.")}


@pytest.fixture(scope="module")
def import_graph() -> dict[str, set[str]]:
    """Every module under `app/`, and the `app.*` modules each one imports."""
    modules = {_module_name(path): path for path in APP_ROOT.rglob("*.py")}

    return {
        name: {edge for edge in _imports(path) if edge in modules}
        for name, path in modules.items()
    }


def _reachable(graph: dict[str, set[str]], root: str) -> set[str]:
    """Every module reachable from `root`, transitively, including itself."""
    seen: set[str] = set()
    pending = [root]

    while pending:
        current = pending.pop()

        if current in seen:
            continue

        seen.add(current)
        pending.extend(graph.get(current, set()))

    return seen


# --- the separation ---------------------------------------------------


@pytest.mark.parametrize("root", WRITE_PATHS)
def test_no_write_path_can_reach_slack(import_graph, root):
    """A module that records that something happened must not be able to post.

    Transitive, deliberately. A direct-import check would pass the moment
    somebody put the Slack client one hop away behind a helper, which is
    precisely how this separation would be lost -- nobody adds
    `from app.services.slack import ...` to a webhook handler; they add a
    helper that has it.

    The route this leaves open is the one the pipeline is: a write path imports
    `app.services.events`, which imports a repository and a domain module, and
    the reachable set stops there. Slack is on the other side of a table.
    """
    assert root in import_graph, f"{root} is not a module under app/"

    reached = _reachable(import_graph, root) & SLACK_MODULES

    assert not reached, (
        f"{root} can reach {sorted(reached)}. A write path that can see Slack "
        "is one somebody will eventually post from -- bypassing the preference "
        "toggle, the duplicate suppression and the failure record, and holding "
        "a pool connection across the call. Emit a domain event instead."
    )


def _code_without_prose(path: Path) -> str:
    """The file's executable text: no comments, no docstrings, strings kept.

    `ast.unparse` drops comments on its own; the docstrings are stripped here.
    Both have to go for the assertion below to be about the CODE -- these two
    modules explain at length why they do not talk to Slack, and prose about a
    rule must not be indistinguishable from breaking it.

    String literals are deliberately KEPT. A module that reached the client
    through `importlib.import_module("app.services.slack")` or through a
    settings key would have a clean import graph and a dirty string, and that
    string is the only thing left to catch it with.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))

    for node in ast.walk(tree):
        if not isinstance(
            node,
            ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef,
        ):
            continue

        first = node.body[0] if node.body else None

        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            node.body = node.body[1:] or [ast.Pass()]

    return ast.unparse(tree)


def test_the_github_modules_name_slack_nowhere_they_could_call_it():
    """A second assertion over the same property, at a different altitude.

    The import graph would still be clean if somebody reached the client
    through a dynamic import or a configuration key, so this one reads the
    executable text and is dumb enough not to be fooled by either.

    Deliberately narrow to the GitHub modules. That is where the requirement
    points, because a signature-verified delivery is the one place in this
    application where a third party's payload meets a write path -- and a
    handler that could post directly from it would bypass the preference
    toggle, the duplicate suppression and the failure record at once.
    """
    for name in ("services/github.py", "rest/github.py"):
        code = _code_without_prose(APP_ROOT / name).lower()

        assert "slack" not in code, (
            f"app/{name} names Slack in code. The webhook handler emits an "
            "event; the pipeline decides what to do with it."
        )


def test_the_delivery_service_is_the_only_thing_that_reaches_slack(import_graph):
    """The other side of the same rule, so it is not vacuously satisfied.

    A test that only asserted absence would pass on an application where
    nothing anywhere posted to Slack -- which is the state this whole task
    existed to fix. This one names the module that IS allowed to, and fails if
    the wire is ever cut.
    """
    reached = _reachable(import_graph, "app.services.notifications")

    assert "app.services.slack" in reached
    assert "app.repositories.events" in reached


# --- the two vocabularies ---------------------------------------------


def test_the_event_kinds_are_the_slack_preference_events():
    """Spelled identically, so a preference lookup is `kind = event`.

    Two tuples in two modules that do not import each other, because a write
    path importing the Slack domain to say an issue was assigned is the
    coupling the whole pipeline avoids. The cost of that is this test: without
    it, a rename on one side produces an event no toggle can enable, which is
    silently never announced.
    """
    assert {kind.value for kind in DomainEventKind} == set(SLACK_NOTIFICATION_EVENTS)


def test_every_kind_has_a_headline():
    """A kind added without one renders as a KeyError here rather than as a
    message reading "None" in somebody's channel."""
    assert set(HEADLINES) == set(DomainEventKind)


def test_the_pipelines_own_reasons_do_not_collide_with_slacks():
    """`preference_disabled` and `no_installation` are decisions this pipeline
    makes, not answers Slack gave, which is why they are separate members.

    In particular `no_installation` is not `not_connected`: the second is what
    a token that has STOPPED working reports, and the two need different
    sentences -- "reconnect" against "there is nothing wrong here".
    """
    assert PREFERENCE_DISABLED not in SLACK_FAILURES
    assert NO_INSTALLATION not in SLACK_FAILURES


def test_no_state_can_spell_success_except_delivered():
    """There is no member meaning "probably fine".

    A vocabulary that could spell a hopeful outcome is one a caller reports
    success with while holding an exception -- the failure mode
    `SlackDeliveryResult` documents for the test button, at the layer nobody is
    watching.
    """
    assert set(DeliveryState) == {
        DeliveryState.PENDING,
        DeliveryState.DELIVERED,
        DeliveryState.FAILED,
        DeliveryState.SKIPPED,
    }


# --- the message ------------------------------------------------------


def event(**overrides) -> DomainEventEntity:
    fields = {
        "workspace_id": UUID("00000000-0000-7000-8000-0000000000fa"),
        "kind": DomainEventKind.ISSUE_ASSIGNED,
        "dedupe_key": "k-1",
        "subject": "ENG-142",
        "summary": "Fix the OAuth callback",
        "path": "/acme/issues/9f1",
        "attempts": 1,
    }

    return DomainEventEntity(**{**fields, **overrides})


def test_a_message_is_one_line_and_a_deep_link():
    assert message_for(event(), base_url="https://app.vector.test") == (
        "Assigned · ENG-142 — Fix the OAuth callback\n"
        "https://app.vector.test/acme/issues/9f1"
    )


def test_a_deployment_with_no_origin_links_nowhere_rather_than_wrongly():
    """A guessed host sends somebody to a login page on a domain that is not
    theirs, which is worse than a message they have to go and find in Vector."""
    assert message_for(event(), base_url=None) == (
        "Assigned · ENG-142 — Fix the OAuth callback"
    )


def test_the_headline_says_which_event_this_is():
    """The verb is what tells a channel apart from a firehose: "Now urgent" and
    "Completed" are different things to do about the same issue."""
    assert message_for(
        event(kind=DomainEventKind.ISSUE_PRIORITY_URGENT), base_url=None
    ).startswith("Now urgent · ")


# --- the project update summary ---------------------------------------


def test_an_update_summary_is_its_first_meaningful_line():
    assert _update_summary("\n\n  Shipped the importer  \nand more") == (
        "Shipped the importer"
    )


def test_an_update_summary_is_bounded():
    assert len(_update_summary("x" * 500)) == UPDATE_SUMMARY_MAX_LENGTH


def test_an_empty_update_body_still_produces_a_summary():
    """`domain_events_summary_length` requires at least one character, so a
    body that is all whitespace would otherwise take down the transaction that
    published a perfectly legitimate update."""
    assert _update_summary("   \n\n ") == UPDATE_SUMMARY_FALLBACK
