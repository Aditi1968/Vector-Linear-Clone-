"""Three failure modes that each reached `main` more than once today.

Every check here exists because the same mistake was made independently by
people who could not see each other's work, and because in all three cases
the whole test suite stayed green while the product was broken. They are
integration gates rather than feature tests: none of them is about what a
feature does, all of them are about whether the pieces still add up after a
merge.

The three, and what each one actually cost:

  1. A method named `list` in a class body makes every `list[...]` annotation
     on a method defined *after* it raise TypeError at import. Two agents hit
     this in two different repositories on the same day. Nothing catches it
     unless something imports the module, and a feature nobody has wired up
     yet is exactly the module nothing imports.

  2. A second `class Query(...)` below the `merge_types` call silently
     shadows it -- the name is rebound, and the fields of every feature not
     named in that class vanish from the API. This happened three times, and
     the suite stayed green each time, because a test that asks for its own
     feature's field still finds it.

  3. An entity gains a field and a row mapper does not. The entity and its
     mapper live in different files, so neither diff shows the gap; it
     surfaces as a KeyError or a TypeError on whichever read path happened
     not to be exercised. This reached main twice in one afternoon.

No database, no Docker, no fixtures.
"""

import ast
import importlib
import pkgutil
from dataclasses import fields, is_dataclass
from pathlib import Path

import pytest

import app
from app.graphql.schema import (
    MUTATION_TYPES,
    QUERY_TYPES,
    Mutation,
    Query,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
APP_ROOT = REPOSITORY_ROOT / "app"


def app_modules() -> list[str]:
    """Every importable module under `app/`, by dotted name."""
    return sorted(
        name for _, name, _ in pkgutil.walk_packages(app.__path__, prefix="app.")
    )


# --------------------------------------------------------------------------
# 1. Every module imports
# --------------------------------------------------------------------------


@pytest.mark.parametrize("module_name", app_modules())
def test_every_app_module_imports(module_name):
    """Importing must not raise, for every module in the application.

    This is the cheapest possible test and it would have caught two separate
    import-time TypeErrors this week, both from the same cause: a method
    named `list` shadows the builtin for every annotation evaluated after it
    in the same class body, so

        class Repo:
            async def list(self) -> None: ...
            async def other(self, ids: list[UUID]) -> None: ...

    raises `TypeError: 'function' object is not subscriptable` when the class
    body executes -- at import, before any test runs.

    It is invisible to a feature's own tests while nothing imports the
    feature, which is the state every new module is in until it is wired to
    the schema. Parameterised per module so a failure names the file rather
    than reporting "something under app/ is broken".

    Note that `from __future__ import annotations` hides the runtime half of
    this and not mypy's, so it silences the failure while leaving the
    signature unreadable. It is not the fix; renaming the method is.
    """
    importlib.import_module(module_name)


# --------------------------------------------------------------------------
# 2. The GraphQL root is composed, not shadowed
# --------------------------------------------------------------------------


def root_field_names(type_) -> set[str]:
    return {field.name for field in type_.__strawberry_definition__.fields}


@pytest.mark.parametrize(
    "feature_type",
    QUERY_TYPES,
    ids=[feature.__name__ for feature in QUERY_TYPES],
)
def test_every_query_type_reaches_the_root(feature_type):
    """Each feature's root query fields survive into the merged `Query`.

    `merge_types` warns when two features declare the same field name, which
    is the collision everyone worries about. It cannot see the other one: a
    plain `class Query(IssueQuery, ProjectQuery)` written below the merge
    rebinds the name, `build_schema` picks up whichever executed last, and
    every feature missing from that class disappears from the schema.

    Three separate branches did this. It survives review because the class
    looks like composition, and it survives testing because each feature's
    own tests ask for its own fields -- and the shadowing class is usually
    written by whoever is adding a feature, so theirs are the fields that
    still work.

    Asserted per feature type rather than against a hand-written list of
    field names: a list would have to be edited by the same person making
    the mistake, in the same commit.
    """
    missing = root_field_names(feature_type) - root_field_names(Query)

    assert not missing, (
        f"{feature_type.__name__} declares {sorted(missing)}, which the "
        "schema's root Query does not expose. Either the type is missing "
        "from QUERY_TYPES, or something below the merge_types call in "
        "app/graphql/schema.py rebound the name `Query`."
    )


@pytest.mark.parametrize(
    "feature_type",
    MUTATION_TYPES,
    ids=[feature.__name__ for feature in MUTATION_TYPES],
)
def test_every_mutation_type_reaches_the_root(feature_type):
    """The same claim for mutations; see the query case above."""
    missing = root_field_names(feature_type) - root_field_names(Mutation)

    assert not missing, (
        f"{feature_type.__name__} declares {sorted(missing)}, which the "
        "schema's root Mutation does not expose."
    )


def test_the_root_types_are_defined_exactly_once():
    """No module rebinds `Query` or `Mutation` after the merge.

    The parameterised checks above catch a shadowing class that drops
    fields. They cannot catch one that happens to list every feature, which
    is green today and silently wrong the next time a feature is added --
    the class would then be a second place that has to be edited, and the
    one nobody remembers.

    So this reads the source instead: in `app/graphql/schema.py`, `Query` and
    `Mutation` may each be bound once, by the assignment that calls
    merge_types.
    """
    source = (APP_ROOT / "graphql" / "schema.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    bindings: dict[str, int] = {"Query": 0, "Mutation": 0}

    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name in bindings:
            bindings[node.name] += 1
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in bindings:
                    bindings[target.id] += 1

    assert bindings == {"Query": 1, "Mutation": 1}, (
        f"Query/Mutation are bound {bindings} times in app/graphql/schema.py. "
        "Each must be bound exactly once, by its merge_types call. A second "
        "binding -- a class or an assignment -- shadows the merge."
    )


# --------------------------------------------------------------------------
# 3. Row mappers cover every field of the entity they build
# --------------------------------------------------------------------------


def mapper_definitions() -> list[tuple[str, str, ast.FunctionDef]]:
    """Every `_to_entity`/`_issue_entity` under app/repositories, parsed.

    Found by walking the source rather than by importing and introspecting,
    because what is being checked is which keys the function *reads* -- a
    fact about its body, not about its behaviour on one row.
    """
    found = []

    for path in sorted((APP_ROOT / "repositories").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))

        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue

            if not node.name.endswith("_entity"):
                continue

            returns = node.returns

            if isinstance(returns, ast.Name) and returns.id.endswith("Entity"):
                found.append((path.name, returns.id, node))

    return found


MAPPERS = mapper_definitions()


def test_the_mappers_were_found():
    """A gate that found nothing to check is a gate that always passes.

    This suite's other mapper test is parameterised over whatever the walk
    discovers, so a rename that made the walk match nothing would leave the
    file reporting success with zero assertions.
    """
    assert len(MAPPERS) >= 6, (
        f"only {len(MAPPERS)} row mappers found under app/repositories; the "
        "search in mapper_definitions() has probably gone stale"
    )


@pytest.mark.parametrize(
    ("filename", "entity_name", "definition"),
    MAPPERS,
    ids=[f"{name}:{entity}" for name, entity, _ in MAPPERS],
)
def test_every_mapper_reads_every_field_of_its_entity(
    filename, entity_name, definition
):
    """A mapper must read one row key per field of the entity it returns.

    The failure this prevents is a merge artefact more than a typo. An entity
    gains a field on one branch and a mapper is written on another; the two
    live in different files, so neither diff shows the gap, and the automatic
    merge produces something that imports cleanly and raises on the first row
    it maps. `IssueEntity` gained `cycle_id` and then `project_id` and
    `milestone_id` this way, and a second mapper in relations.py was still
    building the entity from the seven columns it had when that branch was
    written -- nineteen fields later.

    Read keys are collected as `row["name"]` subscripts anywhere in the
    function, so a mapper that computes a value before passing it still
    counts as reading the column.
    """
    module = importlib.import_module(f"app.domain.{_domain_module(entity_name)}")
    entity = getattr(module, entity_name)

    assert is_dataclass(entity), f"{entity_name} is not a dataclass"

    expected = {field.name for field in fields(entity)}

    read = {
        node.slice.value
        for node in ast.walk(definition)
        if isinstance(node, ast.Subscript)
        and isinstance(node.value, ast.Name)
        and node.value.id == "row"
        and isinstance(node.slice, ast.Constant)
        and isinstance(node.slice.value, str)
    }

    missing = expected - read

    assert not missing, (
        f"{filename}:{definition.name} builds {entity_name} but never reads "
        f"{sorted(missing)}. Either the mapper is missing a column the entity "
        "now carries, or the entity gained a field the SELECT above does not "
        "return."
    )


def _domain_module(entity_name: str) -> str:
    """The `app.domain` module an entity class lives in.

    A small table rather than a naming convention, because the convention
    does not hold: `WorkspaceMembershipEntity` lives in `memberships`, and
    guessing would make this gate fail for the wrong reason.
    """
    known = {
        "IssueEntity": "issues",
        "CommentEntity": "comments",
        "LabelEntity": "labels",
        "CycleEntity": "cycles",
        "ProjectEntity": "projects",
        "UserEntity": "auth",
        "SessionEntity": "auth",
        "WorkspaceMembershipEntity": "memberships",
        "WorkspaceMemberEntity": "memberships",
        "WorkspaceInvitationEntity": "memberships",
        "TeamEntity": "teams",
        "WorkflowStateEntity": "teams",
        "SlackInstallationEntity": "slack",
    }

    assert entity_name in known, (
        f"{entity_name} is not in the entity->module table in "
        "tests/test_integration_gates.py; add it"
    )

    return known[entity_name]
