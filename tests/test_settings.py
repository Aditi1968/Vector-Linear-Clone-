"""Configuration is mandatory, and demanded late enough to stay importable.

Two properties pull against each other here, and satisfying either one
alone is easy and wrong. Settings must be required -- a deployment that
configures nothing has to fail, not run on defaults nobody chose. And they
must not be resolved at import, or every test, linter and `--help` in a
container fails for want of a database it never touches.

The environment setting is the one that makes this more than bookkeeping:
it decides whether GraphiQL is served and whether the schema answers
introspection, so a default for it is a decision about production made by
whoever forgot to set it.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Environment, Settings, get_settings


ALL_ENVIRONMENTS: list[Environment] = ["development", "test", "production"]

# Obviously fake, and never dialled: nothing in this module connects.
PLACEHOLDER_DSN = "postgresql://user:password@localhost:5432/vector"

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent

# Derived rather than listed, so a settings field added later is cleared by
# these tests instead of quietly falling through to the developer's shell.
SETTINGS_VARIABLES = frozenset(name.upper() for name in Settings.model_fields)

IMPORT_PROBE_TIMEOUT = 120.0

# Imports only, in an interpreter given no configuration at all. The
# get_settings() call is the control: if it returns a Settings object then
# something configured this process after all, and the imports below would
# prove nothing.
IMPORT_PROBE = """
from pydantic import ValidationError

from app.config import get_settings

try:
    get_settings()
except ValidationError:
    pass
else:
    raise SystemExit("configuration was available; this probe proves nothing")

import app.config
import app.db
import app.graphql.router
import app.graphql.schema
import app.main

print("imported")
"""


def use_environment(monkeypatch, tmp_path, **variables: str) -> None:
    """Replace every configuration source this process can read.

    Three things have to happen together or the isolation is a fiction.
    Every variable Settings knows how to read is cleared, so a value in the
    developer's shell cannot decide the outcome of a test. The working
    directory moves, because `env_file=".env"` is resolved relative to it
    and the repository root holds a real one. And the cache is dropped,
    because get_settings is lru_cached and would otherwise keep answering
    with settings built from somebody else's environment.
    """
    for name in SETTINGS_VARIABLES:
        monkeypatch.delenv(name, raising=False)

    for name, value in variables.items():
        monkeypatch.setenv(name, value)

    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()


def missing_fields(error: ValidationError) -> set[str]:
    return {
        str(detail["loc"][0])
        for detail in error.errors()
        if detail["type"] == "missing"
    }


@pytest.fixture(autouse=True)
def clear_settings_cache():
    """Never let a test's synthetic environment outlive it.

    get_settings caches for the process, not the test, so a cached object
    built from a deliberately broken environment would be handed to
    whatever ran next.
    """
    get_settings.cache_clear()

    yield

    get_settings.cache_clear()


def test_environment_has_no_default(monkeypatch, tmp_path):
    """A deployment that forgets ENVIRONMENT must not have one chosen for it."""
    use_environment(monkeypatch, tmp_path, DATABASE_URL=PLACEHOLDER_DSN)

    with pytest.raises(ValidationError) as raised:
        get_settings()

    assert missing_fields(raised.value) == {"environment"}


@pytest.mark.parametrize("environment", ALL_ENVIRONMENTS)
def test_each_environment_is_accepted(monkeypatch, tmp_path, environment: Environment):
    use_environment(
        monkeypatch,
        tmp_path,
        DATABASE_URL=PLACEHOLDER_DSN,
        ENVIRONMENT=environment,
    )

    assert get_settings().environment == environment


def test_an_unrecognised_environment_is_rejected(monkeypatch, tmp_path):
    """The environment is a closed set, not a label.

    Every protection gated on it compares against "production"; a typo
    that was merely accepted would read as not-production and disable them
    all.
    """
    use_environment(
        monkeypatch,
        tmp_path,
        DATABASE_URL=PLACEHOLDER_DSN,
        ENVIRONMENT="prod",
    )

    with pytest.raises(ValidationError) as raised:
        get_settings()

    # The rejection has to be about this value and not, say, a DSN that
    # went missing along the way: a bare `raises` here would pass on any
    # settings failure at all.
    assert [detail["loc"] for detail in raised.value.errors()] == [("environment",)]
    assert missing_fields(raised.value) == set()


def test_modules_import_without_any_configuration(tmp_path):
    """Importing the application must not require a runtime environment.

    Run in a fresh interpreter rather than through importlib: these modules
    are already imported in the test process, so an in-process check would
    exercise reloading rather than importing -- and app.main is precisely
    the module whose import used to build an application as a side effect.
    """
    probe = tmp_path / "probe.py"
    probe.write_text(IMPORT_PROBE, encoding="utf-8")

    environment = dict(os.environ)

    for name in SETTINGS_VARIABLES:
        environment.pop(name, None)

    environment["PYTHONPATH"] = str(REPOSITORY_ROOT)

    completed = subprocess.run(
        [sys.executable, str(probe)],
        capture_output=True,
        text=True,
        # Run from tmp_path so the repository .env is out of reach, for the
        # same reason use_environment chdirs.
        cwd=tmp_path,
        env=environment,
        timeout=IMPORT_PROBE_TIMEOUT,
    )

    assert completed.returncode == 0, completed.stderr
    assert "imported" in completed.stdout
