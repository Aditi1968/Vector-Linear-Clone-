"""Tests for the throwaway-PostgreSQL fixture's Docker plumbing.

No Docker. Every `docker` invocation is answered by FakeDocker, because the
cases worth pinning here are the ones a healthy daemon will not produce on
demand: a container whose host port is not published yet, one that died
before publishing anything, and a `docker inspect` that fails outright.

The original bug is the reason this file exists. `_published_port` read
`docker port` once and indexed straight into the split output, so a container
with the port known but no binding recorded -- which `docker port` reports by
exiting 0 and printing nothing -- surfaced as `IndexError: list index out of
range` from inside a fixture, naming neither the container nor its state.
"""

import inspect
import json
import subprocess

import pytest

from tests import conftest
from tests.conftest import CONTAINER_PORT, PUBLISH_SPEC


HOST_PORT = "49731"

# What Docker records once it has actually bound the port. The host port is
# whatever was free at the time, so nothing here may assume a value.
BOUND = {CONTAINER_PORT: [{"HostIp": "127.0.0.1", "HostPort": HOST_PORT}]}

# The shape behind the original IndexError: Docker knows the port and holds no
# binding for it. `docker port` prints nothing and exits 0 on this one.
UNBOUND = {CONTAINER_PORT: []}

# What a container that has published nothing at all reports.
NOTHING = {}


async def _connects_immediately(dsn: str) -> None:
    """Stands in for _accept_connections; the server is not what is on test."""
    return None


def completed(argv, returncode=0, stdout="", stderr=""):
    """A real CompletedProcess, so the fake cannot drift from subprocess."""
    return subprocess.CompletedProcess(list(argv), returncode, stdout, stderr)


class FakeDocker:
    """Stands in for subprocess.run, answering `docker` and recording argv.

    `inspects` is the sequence of (status, ports) answers `docker inspect`
    gives, one per call, with the last repeating forever -- which is how "the
    binding only shows up on the second look" is expressed.
    """

    def __init__(
        self,
        inspects=(("running", BOUND),),
        run_returncode=0,
        run_stderr="",
        inspect_returncode=0,
        logs="FATAL: could not create shared memory segment",
    ):
        self.inspects = list(inspects)
        self.run_returncode = run_returncode
        self.run_stderr = run_stderr
        self.inspect_returncode = inspect_returncode
        self.logs = logs
        self.commands: list[list[str]] = []

    def __call__(self, argv, **kwargs):
        self.commands.append(list(argv))

        assert argv[0] == "docker", f"not a docker command: {argv}"

        command = argv[1]

        if command == "run":
            return completed(argv, self.run_returncode, "deadbeef\n", self.run_stderr)

        if command == "inspect":
            if self.inspect_returncode != 0:
                return completed(
                    argv,
                    self.inspect_returncode,
                    stderr=f"Error: No such object: {argv[2]}",
                )

            status, ports = self._next_inspect()

            return completed(argv, 0, f"{status}\t{json.dumps(ports)}\n")

        if command == "logs":
            return completed(argv, 0, self.logs)

        if command in {"rm", "info"}:
            return completed(argv, 0)

        raise AssertionError(f"unexpected docker command: {argv}")

    def _next_inspect(self):
        return self.inspects.pop(0) if len(self.inspects) > 1 else self.inspects[0]

    def argv(self, command: str) -> list[str]:
        for recorded in self.commands:
            if recorded[1] == command:
                return recorded

        raise AssertionError(f"docker {command} was never run: {self.commands}")

    def count(self, command: str) -> int:
        return sum(1 for recorded in self.commands if recorded[1] == command)

    def container(self) -> str:
        """The name the fixture generated, taken from `docker run --name`."""
        run = self.argv("run")

        return run[run.index("--name") + 1]

    def removed(self) -> list[str]:
        """Container names passed to `docker rm --force`."""
        return [
            recorded[-1]
            for recorded in self.commands
            if recorded[1] == "rm" and "--force" in recorded
        ]


@pytest.fixture
def docker(monkeypatch):
    """Installs a FakeDocker over subprocess.run, with the clock taken out.

    The timeout stays generous so that a test wanting to observe polling can;
    the one test about giving up sets it to zero itself.
    """
    monkeypatch.setattr(conftest, "PORT_POLL_INTERVAL", 0.0)
    monkeypatch.setattr(conftest, "PORT_PUBLISH_TIMEOUT", 5.0)

    def install(fake: FakeDocker = None) -> FakeDocker:
        fake = fake if fake is not None else FakeDocker()
        monkeypatch.setattr(conftest.subprocess, "run", fake)

        return fake

    return install


def start_fixture(monkeypatch, fake: FakeDocker):
    """Drive the real postgres_dsn fixture function against a fake Docker.

    Returns the generator, not the DSN: several tests here are about what the
    fixture does on the way out, which only running its finally will show.
    """
    monkeypatch.setattr(conftest, "docker_available", lambda: True)
    monkeypatch.setattr(conftest.subprocess, "run", fake)
    monkeypatch.setattr(conftest, "_accept_connections", _connects_immediately)

    return inspect.unwrap(conftest.postgres_dsn)()


def test_the_publish_spec_asks_for_an_ephemeral_loopback_port():
    """The point of the spec, asserted on its parts rather than its text.

    A fixed host port would collide with a local PostgreSQL or a concurrent
    run, and a non-loopback host IP would put a trivially-credentialed
    database on the network.
    """
    host_ip, host_port, container_port = PUBLISH_SPEC.split(":")

    assert host_ip == "127.0.0.1"
    assert host_port == "", "a fixed host port must not be hard-coded"
    assert container_port == "5432"


def test_published_port_returns_the_host_port_docker_chose(docker):
    docker(FakeDocker(inspects=[("running", BOUND)]))

    assert conftest._published_port("vector-test-abc") == int(HOST_PORT)


def test_published_port_waits_for_a_binding_that_arrives_late(docker):
    """`docker run --detach` returning is not the instant the binding lands.

    One read is not evidence of absence, which is what the old code treated it
    as.
    """
    fake = docker(
        FakeDocker(
            inspects=[
                ("running", NOTHING),
                ("running", UNBOUND),
                ("running", BOUND),
            ]
        )
    )

    assert conftest._published_port("vector-test-abc") == int(HOST_PORT)
    assert fake.count("inspect") == 3


def test_unpublished_port_raises_a_diagnostic_not_an_indexerror(docker, monkeypatch):
    """The regression test for the reported failure.

    A running container that never gets a binding must produce a message
    naming the container, its state, what Docker did report and the spec that
    was asked for -- not an IndexError from slicing empty output.
    """
    monkeypatch.setattr(conftest, "PORT_PUBLISH_TIMEOUT", 0.0)
    docker(FakeDocker(inspects=[("running", UNBOUND)]))

    with pytest.raises(RuntimeError) as error:
        conftest._published_port("vector-test-abc")

    message = str(error.value)

    assert "vector-test-abc" in message
    assert CONTAINER_PORT in message
    assert "running" in message
    assert PUBLISH_SPEC in message
    assert "none appeared within" in message


def test_the_diagnostic_carries_the_container_output(docker):
    """Why it never published is in the log, and the container is about to go."""
    fake = docker(
        FakeDocker(
            inspects=[("exited", NOTHING)],
            logs="FATAL: database files are incompatible with server",
        )
    )

    with pytest.raises(RuntimeError) as error:
        conftest._published_port("vector-test-abc")

    assert "database files are incompatible" in str(error.value)
    assert fake.argv("logs")[2:4] == ["--tail", str(conftest.LOG_TAIL_LINES)]


def test_a_stopped_container_is_reported_without_waiting(docker):
    """Nothing is coming, so the timeout would only delay the report."""
    fake = docker(FakeDocker(inspects=[("exited", NOTHING)]))

    with pytest.raises(RuntimeError) as error:
        conftest._published_port("vector-test-abc")

    assert "stopped before Docker recorded one" in str(error.value)
    assert fake.count("inspect") == 1


def test_a_failing_docker_inspect_is_reported_as_itself(docker):
    """Distinct from "no binding yet": no amount of polling fixes this one."""
    docker(FakeDocker(inspect_returncode=1))

    with pytest.raises(RuntimeError) as error:
        conftest._published_port("vector-test-abc")

    message = str(error.value)

    assert "docker inspect" in message
    assert "No such object" in message


def test_unreadable_inspect_output_is_reported_as_itself(docker):
    """A JSON parse failure must not read as a missing port either."""

    class TruncatedDocker(FakeDocker):
        def __call__(self, argv, **kwargs):
            if argv[1] == "inspect":
                self.commands.append(list(argv))

                return completed(argv, 0, 'running\t{"5432/tcp": [')

            return super().__call__(argv, **kwargs)

    docker(TruncatedDocker())

    with pytest.raises(RuntimeError) as error:
        conftest._published_port("vector-test-abc")

    assert "unreadable port map" in str(error.value)


def test_an_ipv4_binding_is_preferred_over_an_ipv6_one(docker):
    """The DSN dials 127.0.0.1, so a v6 port would have nothing listening."""
    docker(
        FakeDocker(
            inspects=[
                (
                    "running",
                    {
                        CONTAINER_PORT: [
                            {"HostIp": "::1", "HostPort": "60000"},
                            {"HostIp": "127.0.0.1", "HostPort": HOST_PORT},
                        ]
                    },
                )
            ]
        )
    )

    assert conftest._published_port("vector-test-abc") == int(HOST_PORT)


def test_the_fixture_publishes_the_container_to_an_ephemeral_loopback_port(
    monkeypatch,
):
    fake = FakeDocker(inspects=[("running", BOUND)])
    generator = start_fixture(monkeypatch, fake)

    try:
        dsn = next(generator)
    finally:
        generator.close()

    run = fake.argv("run")

    assert "--publish" in run
    assert run[run.index("--publish") + 1] == PUBLISH_SPEC

    # The DSN follows the port Docker handed out, wherever it landed.
    assert dsn.endswith(f"@127.0.0.1:{HOST_PORT}/{conftest.POSTGRES_DB}")


def test_the_fixture_removes_the_container_it_named(monkeypatch):
    fake = FakeDocker(inspects=[("running", BOUND)])
    generator = start_fixture(monkeypatch, fake)

    try:
        next(generator)
    finally:
        generator.close()

    assert fake.container().startswith("vector-test-")
    assert fake.removed() == [fake.container()]


def test_the_container_is_removed_when_the_port_never_publishes(monkeypatch):
    """The reported failure must not also leak a container.

    `_published_port` raising has to leave the daemon as it found it, or a day
    of debugging accumulates dead vector-test-* containers.
    """
    monkeypatch.setattr(conftest, "PORT_POLL_INTERVAL", 0.0)
    monkeypatch.setattr(conftest, "PORT_PUBLISH_TIMEOUT", 0.0)

    fake = FakeDocker(inspects=[("running", UNBOUND)])
    generator = start_fixture(monkeypatch, fake)

    with pytest.raises(RuntimeError):
        next(generator)

    assert fake.removed() == [fake.container()]


def test_the_container_is_removed_when_docker_run_times_out(monkeypatch):
    """A pull slow enough to hit the timeout still leaves a container behind.

    subprocess raises before `docker run` ever returns an id, so the name the
    fixture generated is the only handle anything has on it.
    """

    class SlowPull(FakeDocker):
        def __call__(self, argv, **kwargs):
            self.commands.append(list(argv))

            if argv[1] == "run":
                raise subprocess.TimeoutExpired(list(argv), 300.0)

            return completed(argv, 0)

    fake = SlowPull()
    generator = start_fixture(monkeypatch, fake)

    with pytest.raises(subprocess.TimeoutExpired):
        next(generator)

    assert fake.removed() == [fake.container()]


def test_a_failed_docker_run_skips_and_leaves_nothing_behind(monkeypatch):
    """An unstartable container is an environment fact, so it skips.

    Cleanup still runs: `docker run` can fail with the container created.
    """
    fake = FakeDocker(run_returncode=125, run_stderr="port is already allocated")
    generator = start_fixture(monkeypatch, fake)

    with pytest.raises(pytest.skip.Exception) as error:
        next(generator)

    assert "port is already allocated" in str(error.value)
    assert fake.removed() == [fake.container()]
