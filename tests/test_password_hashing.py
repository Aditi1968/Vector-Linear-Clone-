"""What the password hasher must and must not do.

Three properties, and only the first is obvious. A correct password has to
verify and a wrong one has to fail -- but a hasher that returned True for
everything would also pass a suite that only checked the happy path, so the
rejections are asserted as carefully as the acceptance.

The other two are the ones that decide whether this is a password hash at
all. The stored string must not be the password or anything derived from it
that a reader could reverse, and two hashes of the same password must
differ, because a hasher without a per-row salt turns one cracked password
into every account that shares it.

Real argon2 throughout. A fake would test the wrapper's control flow and
none of the properties above, and the wrapper is not the part that can be
wrong in a way that matters. Each hash costs on the order of 100ms, which is
the price of the algorithm being worth using; the file is kept short for
that reason.
"""

import asyncio
import threading
import time

import pytest
from argon2 import extract_parameters
from argon2.exceptions import InvalidHashError

from app.services.passwords import (
    MAX_CONCURRENT_HASHES,
    Argon2PasswordHasher,
    _decoy_hash,
)


PASSWORD = "correct horse battery staple"

# Differs from PASSWORD in one character, at the end. A verifier that
# compared prefixes, or lengths, or nothing at all would accept it.
NEAR_MISS = "correct horse battery stapl3"

# The prefix migration 003's users_password_hash_argon2id constraint requires.
# Restated here rather than imported, so that a change to either one has to
# be made twice and looked at once.
ARGON2ID_PREFIX = "$argon2id$"


@pytest.fixture
def hasher() -> Argon2PasswordHasher:
    return Argon2PasswordHasher()


async def test_a_password_verifies_against_its_own_hash(hasher):
    stored = await hasher.hash(PASSWORD)

    assert await hasher.verify(password_hash=stored, password=PASSWORD) is True


async def test_a_wrong_password_does_not_verify(hasher):
    stored = await hasher.hash(PASSWORD)

    assert await hasher.verify(password_hash=stored, password=NEAR_MISS) is False


async def test_an_empty_password_does_not_verify_against_a_real_hash(hasher):
    """The failure mode where a missing field reads as a blank one.

    A client that omits its password field entirely must not authenticate;
    the service rejects "" on length before it gets here, and the hasher
    rejects it independently.
    """
    stored = await hasher.hash(PASSWORD)

    assert await hasher.verify(password_hash=stored, password="") is False


async def test_the_stored_hash_does_not_contain_the_password(hasher):
    """The whole point of hashing, asserted rather than assumed."""
    stored = await hasher.hash(PASSWORD)

    assert PASSWORD not in stored

    # Not one word of it either -- a hasher that stored a "hash" of the
    # words plus a digest would pass a naive substring check on the whole
    # phrase and be exactly as broken.
    for word in PASSWORD.split():
        assert word not in stored


async def test_the_stored_hash_is_argon2id(hasher):
    """The algorithm the schema will accept, and the profile RFC 9106 names.

    argon2i and argon2d both produce a hash the same length in the same
    format; only the prefix distinguishes them, and only argon2id resists
    both side-channel and GPU-tradeoff attacks. The constraint in migration
    003 refuses the other two, so a change of default here would fail on
    INSERT -- this is that check, without a database.
    """
    stored = await hasher.hash(PASSWORD)

    assert stored.startswith(ARGON2ID_PREFIX)

    parameters = extract_parameters(stored)

    # RFC 9106's low-memory profile: 64 MiB, three passes, four lanes.
    assert parameters.memory_cost == 65536
    assert parameters.time_cost == 3
    assert parameters.parallelism == 4


async def test_two_hashes_of_one_password_differ(hasher):
    """Evidence of a per-hash salt.

    Without one, identical passwords produce identical rows: a stolen
    database shows at a glance which accounts share a password, and one
    cracked hash unlocks all of them. This is the cheapest test in the file
    and the one that catches the most catastrophic possible mistake.
    """
    first = await hasher.hash(PASSWORD)
    second = await hasher.hash(PASSWORD)

    assert first != second

    # And both are still the same password.
    assert await hasher.verify(password_hash=first, password=PASSWORD) is True
    assert await hasher.verify(password_hash=second, password=PASSWORD) is True


async def test_an_unreadable_stored_hash_is_not_reported_as_a_wrong_password(
    hasher,
):
    """A corrupt row is a fault, not a failed log-in.

    Answering False here would turn "this column holds something that is not
    a hash" into "your password is wrong": the account becomes impossible to
    sign in to, the client is told it is their fault, and nothing anywhere
    says the data is broken. It has to propagate and be masked as an
    internal error like any other defect.
    """
    with pytest.raises(InvalidHashError):
        await hasher.verify(password_hash="not a hash at all", password=PASSWORD)


async def test_verifying_the_decoy_never_succeeds_and_never_raises(hasher):
    """The unknown-address path: spend the time, learn nothing, fail quietly.

    It must not raise, because the caller is on its way to an
    AuthenticationError and an exception escaping here would replace a
    deliberate refusal with an internal error -- which is itself a
    distinguishable response, and therefore the enumeration signal all over
    again.
    """
    assert await hasher.verify_decoy(PASSWORD) is None
    assert await hasher.verify_decoy("") is None


async def test_the_decoy_costs_what_a_real_verification_costs(hasher):
    """The decoy's parameters must track the hasher's, or the timing diverges.

    Comparing parameters rather than wall-clock: a stopwatch assertion on a
    shared CI runner is a flake generator, and the parameters are what
    actually decide the cost. If someone re-tunes the hasher and the decoy is
    still built at the old cost, the unknown-address path becomes measurably
    faster or slower than the wrong-password path and the whole apparatus
    stops working -- silently, because both paths still return the same
    error.
    """
    real = extract_parameters(await hasher.hash(PASSWORD))
    decoy = extract_parameters(await asyncio.to_thread(_decoy_hash))

    assert decoy == real


async def test_the_decoy_is_built_once_per_process():
    """Built per call, it would cost two hashes on the first unknown address.

    That is twice what a wrong-password attempt costs, which is a timing
    difference in the opposite direction and just as readable.
    """
    assert _decoy_hash() is _decoy_hash()


async def test_no_password_matches_the_decoy(hasher):
    """It is a hash of random bytes; nothing anyone can submit will match it."""
    decoy = await asyncio.to_thread(_decoy_hash)

    assert await hasher.verify(password_hash=decoy, password=PASSWORD) is False
    assert await hasher.verify(password_hash=decoy, password="") is False


# --- the concurrency bound ----------------------------------------------
#
# The arithmetic is in `MAX_CONCURRENT_HASHES`: every argon2id operation
# allocates 64 MiB, `asyncio.to_thread` runs on the default executor --
# min(32, cpus + 4) threads -- and k8s/30-api.yaml sets `limits.memory: 512Mi`.
# Unbounded, a handful of concurrent log-ins against an address that does not
# exist is an OOMKill, with no account and no valid password required.
#
# Counted in a real thread with a real lock, because the thing being bounded
# runs in a thread. Each stand-in sleeps rather than hashing, so this section
# costs milliseconds instead of the ~100ms per operation the rest of the file
# pays -- the property under test is the gate, not the algorithm.


class CountingArgon2:
    """A stand-in for `argon2.PasswordHasher` that records concurrency.

    Substituted for the whole inner object rather than patched onto it,
    because argon2-cffi's methods are read-only descriptors -- there is no
    monkeypatching an instance of it.

    `peak` is shared through the `state` dict so several stand-ins can report
    into one tally, which is what the shared-gate test below needs. The lock is
    a `threading.Lock` and not an asyncio one: these methods run in worker
    threads, which is the entire reason there is anything to bound.
    """

    def __init__(self, state, lock, duration=0.05):
        self._state = state
        self._lock = lock
        self._duration = duration

    def _run(self):
        with self._lock:
            self._state["live"] += 1
            self._state["peak"] = max(self._state["peak"], self._state["live"])

        time.sleep(self._duration)

        with self._lock:
            self._state["live"] -= 1

    def hash(self, password):
        self._run()

        return ARGON2ID_PREFIX

    def verify(self, password_hash, password):
        self._run()

        return True


def counting_hasher(state, lock) -> Argon2PasswordHasher:
    """An `Argon2PasswordHasher` whose argon2 work is counted, not performed."""
    hasher = Argon2PasswordHasher()
    hasher._hasher = CountingArgon2(state, lock)

    return hasher


@pytest.mark.parametrize("operation", ["hash", "verify", "verify_decoy"])
async def test_no_more_than_the_budget_of_argon2_operations_run_at_once(operation):
    """THE TEST THAT FAILS IF THE SEMAPHORE IS REMOVED.

    All three entry points, because the one that matters most is the one that
    needs no account: `verify_decoy` spends the full cost of a verification for
    an address that matches nothing, which is exactly the property that makes
    log-in constant-time and exactly the property that made it an amplifier.
    The decoy is correct and stays; what was missing was this bound.

    Four times the budget is submitted at once, so a gate that was absent --
    or that was held per hasher INSTANCE, which would be a semaphore of one per
    caller and permit precisely the concurrency it is meant to refuse -- shows
    up as a peak far above the ceiling.

    The peak is asserted to REACH the budget as well as not to exceed it. A
    bound that never lets four run is a bound that has become a queue, and the
    latency it costs would be invisible in a test that only checked the
    ceiling.
    """
    state = {"live": 0, "peak": 0}
    hasher = counting_hasher(state, threading.Lock())

    # The decoy path goes through `verify`, which is what makes gating `verify`
    # cover it -- and is why all three are asserted rather than only the two
    # with a gate of their own.
    calls = {
        "hash": lambda: hasher.hash(PASSWORD),
        "verify": lambda: hasher.verify(
            password_hash=ARGON2ID_PREFIX,
            password=PASSWORD,
        ),
        "verify_decoy": lambda: hasher.verify_decoy(PASSWORD),
    }
    call = calls[operation]

    await asyncio.gather(*(call() for _ in range(MAX_CONCURRENT_HASHES * 4)))

    assert state["peak"] == MAX_CONCURRENT_HASHES


async def test_the_gate_is_shared_between_hasher_instances():
    """One gate per event loop, not one per hasher -- which is the whole point.

    `app.graphql.context` builds an `Argon2PasswordHasher` per REQUEST, so a
    semaphore held on the instance would be a semaphore of one, per caller.
    That is a bound that reads as correct, passes any test using a single
    hasher, and permits exactly the concurrency it exists to refuse.
    """
    state = {"live": 0, "peak": 0}
    lock = threading.Lock()

    hashers = [counting_hasher(state, lock) for _ in range(MAX_CONCURRENT_HASHES * 4)]

    await asyncio.gather(*(hasher.hash(PASSWORD) for hasher in hashers))

    assert state["peak"] == MAX_CONCURRENT_HASHES


async def test_the_gate_works_on_a_loop_that_did_not_build_it():
    """A module-level `asyncio.Semaphore` would break every loop but the first.

    One binds itself to the first loop that has to WAIT on it and refuses every
    other one afterwards -- and one process legitimately runs several loops,
    since every `asyncio.run` in this suite is one. The failure would appear
    only in the contended case, which is to say only under the load the bound
    exists for.

    This test is therefore its own second loop: the parametrised test above has
    already contended the gate on pytest-asyncio's loop by the time this runs,
    and `asyncio.run` below is a different one.
    """
    state = {"live": 0, "peak": 0}
    lock = threading.Lock()

    async def contend():
        hasher = counting_hasher(state, lock)

        await asyncio.gather(
            *(hasher.hash(PASSWORD) for _ in range(MAX_CONCURRENT_HASHES * 4))
        )

    await asyncio.to_thread(asyncio.run, contend())

    assert state["peak"] == MAX_CONCURRENT_HASHES
