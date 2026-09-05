"""Adversarial guards on the Phase 1a-2 engineering gates themselves.

Every other test in this suite checks the application. These check the
gates, because a gate is the one thing in the repository that can be
wrong without anything going red: ruff, mypy, pre-commit, gitleaks and CI
all exit 0 when they are configured to check nothing, and exit 0 is what
everyone reads as "fine".

So the assertions here are deliberately about *configuration*, not about
behaviour:

  * that ruff still selects the rule families that were agreed, and still
    walks every Python file in the repository rather than a subset;
  * that mypy's strictness flags are still on and its one library override
    is still the only one;
  * that pre-commit's file-rewriting hooks still skip `migrations/`, which
    is what keeps an already-applied migration's checksum stable;
  * that both dependency locks are still fully pinned and hashed, and the
    dev lock is still a superset of the runtime one;
  * that the image bakes in no environment, ships no secret and starts
    through the factory;
  * that CI grants only read scope, contains no fail-open construct, and
    that its "the db suite must not have skipped" guard actually rejects a
    report in which everything skipped;
  * that the gitleaks allowlist stays scoped to one rule and matched
    against the captured secret, so it can never blanket-exclude a source
    directory.

The mutation evidence for each gate lives in the review that produced this
file; what is kept here is the invariant, so that weakening a gate is a
test failure rather than a quiet edit to a config file nobody re-reads.

One test in this file is an expected failure and says so: see
`test_mypy_also_checks_the_migration_runner`.
"""

import hashlib
import re
import subprocess
import sys
import tomllib
import xml.etree.ElementTree as ElementTree
from pathlib import Path

import pytest

from scripts.apply_migration import compute_checksum, read_migration


REPO_ROOT = Path(__file__).resolve().parents[1]

MIGRATIONS_DIR = REPO_ROOT / "migrations"
INITIAL_MIGRATION = MIGRATIONS_DIR / "001_issues.sql"
TENANCY_MIGRATION = MIGRATIONS_DIR / "002_tenancy.sql"

# Every migration in the repository, in ledger order. Asserted as an exact
# list rather than a subset; see `test_no_second_migration_appeared`.
EXPECTED_MIGRATIONS = ["001_issues.sql", "002_tenancy.sql"]

# The checksum `scripts/apply_migration.py` records in the ledger, over the
# migration's text. 001 is applied in production, so this value is a fact
# about the database and not a preference: a tool that rewrites the file --
# a formatter, a whitespace fixer, an editor adding a final newline --
# changes it, and the next `--status` reports the ledger as tampered with.
INITIAL_MIGRATION_CHECKSUM = (
    "a6f6c3aacae861255c1685f0c3e9444fd286dc01aab4d073f4512162e1b46879"
)

# The same pin for 002, and it means something different, which is worth being
# exact about. 002 has been applied to no database: this value is not a fact
# about production, it is this repository's own discipline, written down while
# 002 is still trivially checkable.
#
# It is here *before* the apply rather than after it because there is no moment
# afterwards at which it can be added honestly. The instant 002 reaches a real
# database its text becomes immutable, and that instant will not be one in
# which anyone thinks to edit this file -- which is how 001 came to have its
# checksum recovered from a file nobody could prove had not already drifted.
#
# So a legitimate pre-apply edit to 002 does fail here, on purpose: updating
# this constant is the one moment left to look twice at a file that is about
# to stop being editable at all.
TENANCY_MIGRATION_CHECKSUM = (
    "a84e0de9607bdb3c4d1ad6903dbe527007fd8944c4efa527b1d466be14bfdc71"
)

PYPROJECT = REPO_ROOT / "pyproject.toml"
PRE_COMMIT_CONFIG = REPO_ROOT / ".pre-commit-config.yaml"
GITLEAKS_CONFIG = REPO_ROOT / ".gitleaks.toml"
DOCKERFILE = REPO_ROOT / "Dockerfile"
DOCKERIGNORE = REPO_ROOT / ".dockerignore"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

RUNTIME_LOCK = REPO_ROOT / "requirements.txt"
DEV_LOCK = REPO_ROOT / "requirements-dev.txt"

# Every family the lint gate is supposed to enforce. Dropping one is how a
# red gate is made green without fixing anything, so the set is asserted
# exactly rather than as a subset.
EXPECTED_RUFF_SELECT = {"E4", "E7", "E9", "F", "I", "B", "ASYNC"}

# Progressive strictness: report mistakes in the annotations that exist,
# without demanding annotations that do not. Each of these buys a specific
# class of defect, and each was confirmed to fire on an injected fault.
EXPECTED_MYPY_FLAGS = (
    "check_untyped_defs",
    "disallow_incomplete_defs",
    "no_implicit_optional",
    "strict_equality",
    "extra_checks",
    "warn_redundant_casts",
    "warn_unused_ignores",
    "warn_unused_configs",
    "warn_return_any",
    "warn_unreachable",
)

# asyncpg ships neither stubs nor a py.typed marker. It is the only library
# mypy is allowed to be blind to; a second entry here is how "mypy passes"
# stops meaning anything.
EXPECTED_MYPY_OVERRIDE_MODULES = ["asyncpg.*"]

# `int(object)` has no matching overload. This is the one suppression in
# the tree, and it is narrowed to a single rule on a single line.
EXPECTED_TYPE_IGNORES = {
    ("app/graphql/limits.py", "# type: ignore[call-overload]"),
}

# Constructs that turn a failing step into a passing job. `if: always()` is
# not inherently fail-open, but on a *gate* step it is the usual way one is
# introduced, so it is refused here rather than argued about later.
FAIL_OPEN_PATTERNS = (
    r"continue-on-error",
    r"\|\|\s*true",
    r"\|\|\s*:",
    r"set\s+\+e",
    r"if:\s*always\(\)",
    r"exit\s+0\s*$",
)


def _pyproject() -> dict:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


def _workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _uncommented(text: str) -> str:
    """The file with `#` comment lines dropped.

    Both of these configurations document their own decisions at length,
    and several of the strings this module refuses to find -- `|| true`,
    `secrets.`, a Neon hostname -- appear in that prose explaining why they
    are absent. Matching against the prose would make the guard assert the
    comments rather than the configuration.
    """
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )


def _lock_pins(path: Path) -> dict[str, str]:
    """Every `name==version` pinned in a compiled lock."""
    pattern = re.compile(r"(?m)^([A-Za-z0-9._-]+)==(\S+?)(?:\s|\\|$)")

    return {
        name.lower().replace("_", "-"): version
        for name, version in pattern.findall(path.read_text(encoding="utf-8"))
    }


def _lock_blocks(path: Path) -> dict[str, str]:
    """Each pinned requirement together with its continuation lines."""
    text = path.read_text(encoding="utf-8")
    blocks = {}

    for block in re.split(r"(?m)^(?=[A-Za-z0-9._-]+==)", text):
        match = re.match(r"^([A-Za-z0-9._-]+)==", block)

        if match is not None:
            blocks[match.group(1).lower().replace("_", "-")] = block

    return blocks


def _tracked_python_files() -> set[str]:
    """Python files git knows about, ignored ones excluded.

    Asked of git rather than of the filesystem so that `.venv`, caches and
    build output cannot inflate the expected set and make the coverage
    assertion below pass for the wrong reason.
    """
    listed = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "*.py"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )

    return {line.strip() for line in listed.stdout.splitlines() if line.strip()}


# --------------------------------------------------------------------------
# The migration that must not move
# --------------------------------------------------------------------------


def test_the_applied_migration_still_hashes_to_what_the_ledger_recorded():
    assert compute_checksum(read_migration(INITIAL_MIGRATION)) == (
        INITIAL_MIGRATION_CHECKSUM
    )


def test_the_tenancy_migration_still_hashes_to_what_was_reviewed():
    """002 is pinned too, for the reason stated at its constant above.

    Not yet a claim about any database -- 002 is unapplied. It is the pin
    put in place while putting it in place is still cheap.
    """
    assert compute_checksum(read_migration(TENANCY_MIGRATION)) == (
        TENANCY_MIGRATION_CHECKSUM
    )


def test_no_second_migration_appeared():
    """A migration must not arrive without someone noticing it arrive.

    A `.sql` file dropped into `migrations/` is picked up by the runner, by
    the lint suite's glob and by CI, all of them silently: nothing else in
    the repository has to change for a new migration to become part of the
    schema. That is the right behaviour for a runner and the wrong
    behaviour for a review, so this is the one place the set is stated by
    hand. Adding a migration means editing this list, which means the
    diff of any branch that adds one says so on a line a reviewer reads.

    Exact equality, not `<=`. A subset check only catches a *deleted*
    migration -- the one failure the ledger already catches on its own,
    since a version it has applied with no file on disk is reported as
    "no file" by `--status`. The failure worth catching here is the
    opposite one, an unreviewed file appearing, and a subset check passes
    for every one of those.

    The name is now historical: the second migration has appeared, and
    was reviewed. What the test guards is the third.
    """
    versions = sorted(path.name for path in MIGRATIONS_DIR.glob("*.sql"))

    assert versions == EXPECTED_MIGRATIONS


def test_the_migration_would_be_rewritten_without_the_pre_commit_exclusion():
    """The exclusion below is load-bearing, and this is why.

    The file ends without a final newline, so `end-of-file-fixer` appends
    one the moment it is allowed near it -- and that single byte changes the
    checksum of a migration already applied in production, which the ledger
    then reports as a mismatch.

    It carries no trailing whitespace, so `trailing-whitespace` would leave
    it alone today. That is a fact about the file's current contents rather
    than a property of it, which is exactly why the exclusion names both
    hooks: the next migration need only end one line with a space for the
    second hook to become load-bearing too, and discovering that from a
    checksum mismatch in production is discovering it too late.
    """
    raw = INITIAL_MIGRATION.read_bytes().decode("utf-8")

    assert not raw.endswith("\n"), "no final newline: end-of-file-fixer would add one"


def test_pre_commit_keeps_file_rewriting_hooks_away_from_migrations():
    config = PRE_COMMIT_CONFIG.read_text(encoding="utf-8")

    for hook in ("trailing-whitespace", "end-of-file-fixer"):
        block = config.split(f"- id: {hook}", 1)

        assert len(block) == 2, f"{hook} is no longer configured"
        assert block[1].lstrip().startswith("exclude: ^migrations/"), (
            f"{hook} no longer excludes migrations/; it would rewrite an "
            "already-applied migration and break its checksum"
        )


# --------------------------------------------------------------------------
# Ruff: the families it enforces, and the files it walks
# --------------------------------------------------------------------------


def test_ruff_still_selects_every_agreed_rule_family():
    lint = _pyproject()["tool"]["ruff"]["lint"]

    assert set(lint["select"]) == EXPECTED_RUFF_SELECT


def test_ruff_disables_nothing_it_selected():
    """No `ignore`, no `per-file-ignores`, no `exclude`.

    Each of the three is a way to keep the gate green by narrowing what it
    looks at, and none of them is needed today: the tree is clean under the
    selected families as they stand.
    """
    ruff = _pyproject()["tool"]["ruff"]
    lint = ruff["lint"]

    assert "ignore" not in lint
    assert "extend-ignore" not in lint
    assert "per-file-ignores" not in lint
    assert "exclude" not in ruff
    assert "extend-exclude" not in ruff


def test_ruff_walks_every_python_file_in_the_repository():
    """Configuration can exclude a directory without saying `exclude`.

    A `src` layout setting, a stray `.ruff.toml`, an entry in `.gitignore`
    that swallows a source tree -- all of them narrow the gate silently. So
    this asks ruff itself which files it would check and compares that with
    what git says exists.
    """
    listed = subprocess.run(
        [sys.executable, "-m", "ruff", "check", "--no-cache", "--show-files", "."],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )

    checked = {
        Path(line.strip()).resolve().relative_to(REPO_ROOT).as_posix()
        for line in listed.stdout.splitlines()
        if line.strip().endswith(".py")
    }

    missing = _tracked_python_files() - checked

    assert not missing, f"ruff does not check {sorted(missing)}"


def test_ruff_covers_the_directories_outside_the_application_package():
    """`scripts/`, `tests/` and `check_db.py` are code too.

    Named individually rather than left to the comparison above, because
    this is the specific narrowing worth refusing: the application package
    is the obvious thing to point a linter at, and the migration runner is
    the file where a defect is most expensive.
    """
    checked = _tracked_python_files()

    assert "check_db.py" in checked
    assert any(path.startswith("scripts/") for path in checked)
    assert any(path.startswith("tests/") for path in checked)


# --------------------------------------------------------------------------
# mypy: strictness, scope, and the suppressions that were not used
# --------------------------------------------------------------------------


def test_mypy_keeps_every_strictness_flag_on():
    mypy = _pyproject()["tool"]["mypy"]
    off = [flag for flag in EXPECTED_MYPY_FLAGS if mypy.get(flag) is not True]

    assert not off, f"mypy strictness flags turned off: {off}"


def test_mypy_has_exactly_one_library_override():
    overrides = _pyproject()["tool"]["mypy"].get("overrides", [])

    assert len(overrides) == 1, f"unexpected mypy overrides: {overrides}"

    only = overrides[0]

    assert only["module"] == EXPECTED_MYPY_OVERRIDE_MODULES
    assert set(only) == {"module", "ignore_missing_imports"}


def test_mypy_silences_no_error_code_globally():
    mypy = _pyproject()["tool"]["mypy"]

    assert not mypy.get("disable_error_code")
    assert mypy.get("ignore_errors") is not True
    assert mypy.get("follow_imports", "normal") == "normal"
    assert not mypy.get("exclude")


def test_the_tree_carries_exactly_one_type_ignore():
    """Broad suppressions are the other way to make a type gate green.

    `warn_unused_ignores` already deletes one that stops being needed; this
    refuses one that is added. The comparison is exact, so a new
    suppression fails here and has to be argued for rather than merged.
    """
    found = set()

    for path in REPO_ROOT.rglob("*.py"):
        relative = path.relative_to(REPO_ROOT).as_posix()

        if relative.startswith((".venv/", "build/", "dist/")):
            continue

        # This file is the scanner, so it necessarily spells out the very
        # comment it hunts for. Counting itself would make the census report
        # its own regex as a suppression in the tree.
        if path == Path(__file__).resolve():
            continue

        for line in path.read_text(encoding="utf-8").splitlines():
            match = re.search(r"#\s*type:\s*ignore(\[[^\]]*\])?", line)

            if match is not None:
                found.add((relative, match.group(0)))

    assert found == EXPECTED_TYPE_IGNORES, f"type: ignore comments changed: {found}"


def test_no_type_ignore_is_a_bare_one():
    """A bare `# type: ignore` suppresses every error on its line.

    The one suppression in the tree names `call-overload`, so a *different*
    error appearing on that line is still reported.
    """
    for _, ignore in EXPECTED_TYPE_IGNORES:
        assert "[" in ignore, f"bare suppression: {ignore}"


@pytest.mark.xfail(
    strict=True,
    reason=(
        "KNOWN GAP, DELIBERATELY RECORDED AS A FAILING EXPECTATION. "
        "mypy is scoped to `files = ['app']` and CI runs `mypy app/`, so "
        "scripts/apply_migration.py -- the migration ledger, ~800 lines, the "
        "code with the most expensive failure mode in the repository -- and "
        "check_db.py are type-checked by nothing. Verified by injecting "
        "`_bad: int = 'not an int'` into scripts/apply_migration.py: mypy "
        "still reported 'Success: no issues found in 32 source files'. "
        "Widen the scope, then delete this marker."
    ),
)
def test_mypy_also_checks_the_migration_runner():
    mypy = _pyproject()["tool"]["mypy"]
    files = set(mypy.get("files", []))

    assert {"scripts", "check_db.py"} <= files


# --------------------------------------------------------------------------
# Dependency locks
# --------------------------------------------------------------------------


@pytest.mark.parametrize("lock", [RUNTIME_LOCK, DEV_LOCK], ids=["runtime", "dev"])
def test_every_pinned_requirement_carries_hashes(lock):
    unhashed = [
        name for name, block in _lock_blocks(lock).items() if "hash=sha256" not in block
    ]

    assert not unhashed, f"{lock.name} pins without hashes: {unhashed}"


@pytest.mark.parametrize("lock", [RUNTIME_LOCK, DEV_LOCK], ids=["runtime", "dev"])
def test_a_lock_states_no_range_and_no_bare_requirement(lock):
    """`==` on every line, or the lock is a suggestion.

    A `>=` or a bare name in a compiled lock means the resolver still gets
    a say at install time, which is the thing pinning exists to remove.
    """
    loose = [
        line.strip()
        for line in lock.read_text(encoding="utf-8").splitlines()
        if re.match(r"^[A-Za-z0-9._-]+\s*(>=|<=|~=|>|<|!=|\s*$)", line)
        and "==" not in line
    ]

    assert not loose, f"{lock.name} is not fully pinned: {loose}"


def test_the_dev_lock_is_a_superset_of_the_runtime_lock():
    runtime = _lock_pins(RUNTIME_LOCK)
    dev = _lock_pins(DEV_LOCK)

    missing = sorted(name for name in runtime if name not in dev)
    disagreeing = sorted(
        f"{name}: runtime {version} vs dev {dev[name]}"
        for name, version in runtime.items()
        if name in dev and dev[name] != version
    )

    assert not missing, f"runtime requirements absent from the dev lock: {missing}"
    assert not disagreeing, f"the two locks pin different versions: {disagreeing}"


def test_the_lock_tool_itself_is_pinned():
    """`uv` regenerates both locks, so an unpinned uv makes them irreproducible."""
    assert "uv" in _lock_pins(DEV_LOCK)


# --------------------------------------------------------------------------
# The runtime image
# --------------------------------------------------------------------------


def test_the_build_context_excludes_the_local_environment_file():
    """`.dockerignore` is an allowlist; `.env` must not be added back."""
    lines = [
        line.strip()
        for line in DOCKERIGNORE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]

    assert lines[0] == "*", ".dockerignore is no longer allowlist-first"

    allowed = {line[1:] for line in lines if line.startswith("!")}

    assert allowed == {"app", "requirements.txt"}, (
        f"the build context gained paths: {sorted(allowed)}"
    )


def test_the_image_bakes_in_no_configuration():
    """A default ENVIRONMENT in the image is a production that starts anyway.

    `app/config.py` makes ENVIRONMENT and DATABASE_URL mandatory precisely
    so a deployment that forgets one fails instead of quietly serving
    GraphiQL and introspection. An `ENV` line here would hand that decision
    back to whoever built the image.
    """
    dockerfile = _uncommented(DOCKERFILE.read_text(encoding="utf-8"))

    assert not re.search(r"(?m)^\s*ENV\s+.*\bENVIRONMENT\b", dockerfile)
    assert not re.search(r"(?m)^\s*ENV\s+.*\bDATABASE_URL\b", dockerfile)
    assert not re.search(r"(?m)^\s*ARG\s+.*\bDATABASE_URL\b", dockerfile)


def test_the_image_drops_root_and_starts_through_the_factory():
    dockerfile = _uncommented(DOCKERFILE.read_text(encoding="utf-8"))

    assert re.search(r"(?m)^\s*USER\s+vector\s*$", dockerfile), "image runs as root"

    # `app.main:app` does not exist: the module-level application was
    # removed so importing the composition root resolves no settings. The
    # factory form is required, not stylistic.
    assert '"--factory"' in dockerfile
    assert '"app.main:create_app"' in dockerfile


def test_the_image_installs_the_runtime_lock_only():
    """pytest, ruff, mypy and uv have no business in a production image."""
    dockerfile = _uncommented(DOCKERFILE.read_text(encoding="utf-8"))

    assert "requirements-dev.txt" not in dockerfile
    assert "--require-hashes" in dockerfile


# --------------------------------------------------------------------------
# CI: least privilege, fail-closed, and no reach for a real database
# --------------------------------------------------------------------------


def test_the_workflow_grants_only_read_scope():
    workflow = _workflow_text()
    block = re.search(r"(?m)^permissions:\n((?:\s+\S+:.*\n)+)", workflow)

    assert block is not None, "the workflow declares no permissions block"

    scopes = dict(re.findall(r"\s+(\S+):\s*(\S+)", block.group(1)))

    assert scopes == {"contents": "read"}, f"unexpected token scope: {scopes}"


def test_the_workflow_contains_no_fail_open_construct():
    workflow = _uncommented(_workflow_text())
    found = [
        pattern
        for pattern in FAIL_OPEN_PATTERNS
        if re.search(pattern, workflow, re.IGNORECASE | re.MULTILINE)
    ]

    assert not found, f"fail-open constructs in ci.yml: {found}"


def test_the_workflow_reaches_for_no_secret_and_no_managed_database():
    """CI must run on a checkout alone.

    No repository secret, no Neon hostname, no DATABASE_URL in the
    environment: the unit suite opens no connection and the db-marked suite
    builds its DSN from the throwaway container it starts itself. A managed
    database in CI would mean tests that mutate the development data and a
    gate that fails when a third party is down.
    """
    workflow = _uncommented(_workflow_text())

    assert "secrets." not in workflow
    assert not re.search(r"(?m)^\s*DATABASE_URL\s*:", workflow)

    # The secret-scan job plants a canary DSN so that a clean gitleaks run is
    # distinguishable from a run that scanned nothing, and a DSN has to look
    # like the real thing to match the rule. It is written to a temp file and
    # never dialled, and `.example.` is reserved by RFC 2606 precisely so a
    # hostname can be spelled out without resolving anywhere.
    #
    # So the invariant is not "the string never appears" -- that would forbid
    # testing the scanner -- it is that every managed-database hostname in the
    # workflow is a documentation one.
    hostnames = re.findall(r"[A-Za-z0-9.-]*neon\.tech", workflow)

    assert all(host.endswith(".example.neon.tech") for host in hostnames), (
        f"a non-documentation Neon hostname appears in CI: {hostnames}"
    )


def test_the_workflow_pins_the_scanner_by_digest():
    workflow = _workflow_text()
    digest = re.search(r'GITLEAKS_SHA256:\s*"([0-9a-f]{64})"', workflow)

    assert digest is not None, "gitleaks is no longer pinned to a sha256"
    assert "sha256sum --check --strict" in workflow


def test_the_integration_job_pins_postgres_18():
    """uuidv7() in 001_issues.sql is native to 18; 16 and 17 reject it.

    The major version is part of what is under test, so the tag the
    workflow warms has to be the tag the fixture starts.
    """
    from tests.conftest import POSTGRES_IMAGE

    assert POSTGRES_IMAGE == "postgres:18"
    assert f"docker pull {POSTGRES_IMAGE}" in _workflow_text()


def _skip_guard_source() -> str:
    """The guard script as it is actually written in ci.yml.

    Extracted from the workflow rather than copied into this file, so that
    editing the workflow's copy cannot leave a stale duplicate passing here.
    """
    workflow = _workflow_text()
    body = workflow.split("<<'PY'\n", 1)

    assert len(body) == 2, "the skip guard heredoc is no longer in ci.yml"

    lines = []

    for line in body[1].splitlines():
        if line.strip() == "PY":
            break

        lines.append(line[10:] if line.startswith(" " * 10) else line.lstrip())

    return "\n".join(lines)


def _junit_report(tmp_path: Path, *, tests: int, skipped: int) -> Path:
    report = tmp_path / "report.xml"
    report.write_text(
        '<?xml version="1.0" encoding="utf-8"?>'
        '<testsuites name="pytest tests">'
        f'<testsuite name="pytest" errors="0" failures="0" '
        f'skipped="{skipped}" tests="{tests}"/>'
        "</testsuites>",
        encoding="utf-8",
    )

    return report


def _run_skip_guard(tmp_path: Path, report: Path) -> subprocess.CompletedProcess:
    script = tmp_path / "guard.py"
    script.write_text(_skip_guard_source(), encoding="utf-8")

    return subprocess.run(
        [sys.executable, str(script), str(report)],
        capture_output=True,
        text=True,
    )


def test_the_skip_guard_accepts_a_run_in_which_the_tests_ran(tmp_path):
    result = _run_skip_guard(tmp_path, _junit_report(tmp_path, tests=12, skipped=0))

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("tests", "skipped", "why"),
    [
        (12, 12, "the fixture skipped every db test"),
        (12, 1, "one db test skipped"),
        (0, 0, "nothing was collected"),
    ],
)
def test_the_skip_guard_rejects_a_run_that_tested_nothing(
    tmp_path, tests, skipped, why
):
    """`pytest -m db` exits 0 when every test skips.

    That is right on a laptop without Docker and wrong in CI, where a
    skipped suite is a green job that verified nothing. The guard is the
    only thing standing between those two readings.
    """
    result = _run_skip_guard(
        tmp_path, _junit_report(tmp_path, tests=tests, skipped=skipped)
    )

    assert result.returncode != 0, f"the guard passed a run where {why}"
    assert "did not run" in result.stderr


# --------------------------------------------------------------------------
# Secret scanning
# --------------------------------------------------------------------------


def test_the_scanner_keeps_the_upstream_ruleset():
    config = GITLEAKS_CONFIG.read_text(encoding="utf-8")

    assert re.search(r"(?m)^useDefault\s*=\s*true", config), (
        "the default gitleaks rules are no longer extended; vendor "
        "credentials would stop being detected"
    )


def test_the_allowlist_is_scoped_to_one_rule_and_matched_against_the_secret():
    """The difference between a narrow allowlist and a blind spot.

    A top-level `[allowlist]` suppresses findings from *every* rule, and a
    `paths` allowlist suppresses everything in a directory -- including a
    real credential committed there by accident. Scoping the allowlist to
    this repository's own rule and matching it against the captured
    password means a known-harmless value is skipped wherever it appears
    and nothing else is.
    """
    config = GITLEAKS_CONFIG.read_text(encoding="utf-8")

    assert "[[rules.allowlists]]" in config
    assert not re.search(r"(?m)^\[allowlist\]", config), "top-level allowlist added"
    assert not re.search(r"(?m)^\[\[allowlist", config), "top-level allowlist added"
    assert 'regexTarget = "secret"' in config
    assert "paths" not in config, "a path-scoped allowlist hides real credentials too"


def test_the_allowlist_holds_only_anchored_placeholder_values():
    """Every entry is an exact value, so it cannot widen into a wildcard.

    `^vector$` skips one known container password. `vector` unanchored
    would skip every DSN password containing the project's own name.
    """
    config = GITLEAKS_CONFIG.read_text(encoding="utf-8")
    block = config.split("regexes = [", 1)[1].split("]", 1)[0]
    values = re.findall(r"'''(.*?)'''", block, re.DOTALL)

    assert values, "the allowlist regexes could not be read"

    for value in values:
        assert value.startswith("^") and value.endswith("$"), (
            f"unanchored allowlist entry: {value!r}"
        )
        assert not re.search(r"[.*+?\[\](){}|\\]", value[1:-1]), (
            f"allowlist entry is a pattern, not a literal value: {value!r}"
        )


def test_the_local_environment_file_is_not_tracked():
    """.env holds the only real credential this repository has.

    Asked of git rather than of the filesystem: the file existing is
    normal, the file being tracked is the leak.
    """
    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", ".env"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )

    assert tracked.returncode != 0, ".env is tracked by git"

    ignored = subprocess.run(
        ["git", "check-ignore", ".env"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )

    assert ignored.returncode == 0, ".env is not gitignored"


def test_no_committed_file_carries_a_credentialed_dsn():
    """The rule gitleaks adds, applied here so the suite catches it too.

    Same regex as `.gitleaks.toml`, same allowlisted placeholders. This is
    not a replacement for the scanner -- it walks no history and knows no
    vendor formats -- but it fails in a developer's own test run rather
    than only on a pushed branch.
    """
    dsn = re.compile(r"(?i)\bpostgres(?:ql)?://[^\s:@/]{1,64}:([^\s:@/]{4,128})@")
    # The same three values `.gitleaks.toml` allowlists, and only those.
    placeholders = {"password", "vector", "PLACEHOLDER"}

    listed = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )

    findings = []

    for name in listed.stdout.splitlines():
        path = REPO_ROOT / name.strip()

        if not path.is_file():
            continue

        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue

        for secret in dsn.findall(text):
            if secret not in placeholders:
                findings.append(
                    f"{name}: {hashlib.sha256(secret.encode()).hexdigest()[:12]}"
                )

    assert not findings, f"credentialed DSN in a committed file: {findings}"


# --------------------------------------------------------------------------
# The suites themselves
# --------------------------------------------------------------------------


def test_the_database_suite_is_deselected_by_default():
    """`-m 'not db'` is what lets `pytest -q` run without Docker.

    It is also what makes the CI integration job's skip guard necessary,
    so the two have to stay described together.
    """
    pytest_config = _pyproject()["tool"]["pytest"]["ini_options"]

    assert pytest_config["addopts"] == "-m 'not db'"
    assert any(marker.startswith("db:") for marker in pytest_config["markers"])


def test_the_junit_report_shape_the_guard_reads_is_the_one_pytest_writes():
    """The guard reads `testsuites/testsuite`; pytest writes exactly that.

    Asserted against a report this suite generates rather than against a
    fixture, so a pytest upgrade that changed the shape would be caught
    here instead of by a green integration job that checked nothing.
    """
    report = ElementTree.fromstring(
        '<testsuites name="pytest tests">'
        '<testsuite name="pytest" errors="0" failures="0" skipped="0" tests="1"/>'
        "</testsuites>"
    )

    assert report.tag == "testsuites"
    assert report.find("testsuite") is not None
