"""Password hashing, and the two things a login path needs it to do.

Hashing is argon2id at argon2-cffi's defaults, which are RFC 9106's
low-memory profile verbatim: 64 MiB, three passes, four lanes, a 16-byte
salt and a 32-byte tag. They are not restated as literals here. Pinning
them in this file would mean a library that improved its defaults left this
service on the old ones, silently, with nothing to notice -- and the encoded
hash carries its own parameters, so an existing row keeps verifying under
the parameters it was written with whatever the defaults become.

Every call runs in a worker thread. argon2 is CPU-bound by construction --
that is what makes it worth using -- so calling it inline would park the
event loop for the whole cost of the hash, and a handful of concurrent
logins would stall every other request in the process. `asyncio.to_thread`
puts it on the default executor instead.

That used to be the whole story, and the paragraph that stood here claimed
the default executor's size was an adequate memory bound. It was wrong, and
the arithmetic is in `MAX_CONCURRENT_HASHES` below: the default executor is
min(32, cpus + 4) threads, which on an ordinary machine is dozens, and every
concurrent argon2id operation allocates 64 MiB. Nothing about reaching that
number requires an account or a correct password -- `verify_decoy` spends
the full cost for an address that matches nothing, which is exactly the
property that makes log-in constant-time and exactly the property that made
it an amplifier. The decoy is correct and stays. What was missing was a bound.
"""

import asyncio
import secrets
import weakref
from functools import cache
from typing import Protocol

from argon2 import PasswordHasher as Argon2Hasher
from argon2.exceptions import VerifyMismatchError


# Length of the throwaway password behind the decoy hash, in bytes of
# entropy. It is never checked against anything, so this only has to be far
# beyond guessing; 32 bytes matches the session token.
DECOY_PASSWORD_BYTES = 32

# How many argon2id operations this process will run at once, and the
# arithmetic behind the number.
#
#   argon2-cffi's default memory_cost is 65536 KiB     = 64 MiB per operation
#   k8s/30-api.yaml sets resources.limits.memory       = 512 MiB per replica
#   k8s/30-api.yaml sets resources.requests.memory     = 192 MiB, which is the
#                                                        deployment's own claim
#                                                        about the resident
#                                                        footprint of a process
#                                                        serving requests
#
#   headroom to the limit                = 512 - 192   = 320 MiB
#   4 concurrent x 64 MiB                              = 256 MiB
#   slack left over                                    =  64 MiB
#
# So: FOUR. That is the worst case this file accepts -- a quarter of a gibibyte
# of argon2 arenas live at once, inside a container that will be OOMKilled at
# half a gibibyte. Five would be 320 MiB and leave nothing; the unbounded
# version this replaces was 26 x 64 MiB = 1.6 GiB on a 22-core machine, which
# is not a tight fit, it is a guaranteed kill an unauthenticated client can
# trigger on demand.
#
# WHICH DEFENCE IS THE REAL ONE. The rate limiter in `app.services.auth` is,
# and this is the backstop. The limiter is what stops an attacker getting
# thousands of hashes started in the first place, and it is shared state so it
# holds across both replicas; but it counts per attempt and cannot bound a
# burst that arrives inside one window, because every one of those attempts is
# individually within budget. The semaphore is what makes that burst queue
# instead of allocating. Neither is sufficient alone: without the limiter this
# only converts an OOM into an unbounded queue of waiters, and without the
# semaphore the limiter's own budget still permits enough simultaneity to fill
# the heap.
#
# The cost is latency under load: at ~100 ms a hash, four at a time is 40
# operations a second per replica, and the 41st waits. That is far above what
# the limiter permits from any one subject and far below what would be needed
# to inconvenience a real sign-in queue.
#
# ponytail: a fixed number tuned against one manifest's memory limit. If the
# limit in k8s/30-api.yaml changes, this changes with it -- there is no
# reader of that file at runtime and inventing one would be a config value
# for something that changes once a year. The upgrade path if it starts
# mattering is to read a MiB budget from Settings and divide by memory_cost.
MAX_CONCURRENT_HASHES = 4

# One gate per event loop, not one per hasher.
#
# Per hasher would be no gate at all: `app.graphql.context` constructs an
# `Argon2PasswordHasher` per REQUEST, so a semaphore held on the instance would
# be a semaphore of one, per caller, which is arithmetic that permits exactly
# the concurrency it is meant to refuse.
#
# Keyed by loop rather than a bare module-level object because an
# `asyncio.Semaphore` binds itself to the first loop that has to WAIT on it and
# refuses every other one afterwards. One process legitimately runs several
# loops -- every `asyncio.run` in the test suite is one -- and the failure would
# appear only in the contended case, which is to say only under the load this
# exists for. Weak keys so a finished loop takes its gate with it rather than
# leaving an entry per test.
_gates: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore]" = (
    weakref.WeakKeyDictionary()
)


def _gate() -> asyncio.Semaphore:
    """The concurrency bound for this loop, built on first use."""
    loop = asyncio.get_running_loop()
    gate = _gates.get(loop)

    if gate is None:
        gate = asyncio.Semaphore(MAX_CONCURRENT_HASHES)
        _gates[loop] = gate

    return gate


class PasswordHasher(Protocol):
    """What AuthService needs of a hasher.

    A protocol rather than the concrete class so that tests can supply a
    fast one. Nothing here returns or accepts a password in any form other
    than the caller's own plaintext argument.
    """

    async def hash(self, password: str) -> str: ...

    async def verify(self, *, password_hash: str, password: str) -> bool: ...

    async def verify_decoy(self, password: str) -> None: ...


class Argon2PasswordHasher:
    """argon2id hashing, off the event loop.

    Stateless and cheap to construct, which matters because the GraphQL
    context builds one per request. The decoy hash it verifies against is
    module-level and computed once per process, for the reason given at
    `_decoy_hash`.
    """

    def __init__(self) -> None:
        self._hasher = Argon2Hasher()

    async def hash(self, password: str) -> str:
        """The encoded argon2id hash of `password`.

        The return value is safe to store and to log the *existence* of.
        The argument is not safe to do anything with except pass here.
        """
        async with _gate():
            return await asyncio.to_thread(self._hasher.hash, password)

    async def verify(self, *, password_hash: str, password: str) -> bool:
        """Whether `password` is the one `password_hash` was made from.

        Only VerifyMismatchError is caught, and only it means "wrong
        password". A stored string argon2 cannot parse raises InvalidHashError
        and is left to propagate: that is a corrupt row or a bug in whatever
        wrote it, and answering "wrong password" to it would turn a data
        integrity failure into a login form that just never works, with
        nothing anywhere saying why.

        `check_needs_rehash` is deliberately not consulted. Upgrading a hash
        on successful login is worth doing and is a write on the login path,
        which needs a transaction boundary and a decision about failure --
        a later change, not a side effect of this one.

        Gated, and this is the gate that matters most. `hash` is reachable
        only from `register`, which writes a row; this one is reachable from
        every log-in attempt, correct or not, and through `verify_decoy` from
        every attempt against an address that has no account at all. An
        unauthenticated flood arrives HERE.
        """
        async with _gate():
            try:
                return await asyncio.to_thread(
                    self._hasher.verify,
                    password_hash,
                    password,
                )
            except VerifyMismatchError:
                return False

    async def verify_decoy(self, password: str) -> None:
        """Spend what a real verification spends, and learn nothing.

        Called when no account matches the submitted address. Without it,
        the unknown-address path returns in microseconds while the
        wrong-password path returns in the ~100ms an argon2 verification
        costs, and the difference is readable from anywhere on the network:
        an attacker with a list of addresses learns which of them have
        accounts here without ever guessing a password.

        Verifying a real hash, rather than sleeping for a fixed interval, is
        what keeps the two paths matched when the cost parameters change.
        The result is discarded because there is nothing to learn from it --
        the decoy is a hash of random bytes nobody holds.

        `_decoy_hash` is fetched through a thread even though it is cached.
        The cached call is a dict lookup, but the *uncached* one computes an
        argon2 hash, and calling it inline would put that on the event loop
        -- which is the one thing this module exists to avoid. `app.main`
        warms it at startup, so in a running deployment this is the dict
        lookup; the thread hop is what makes the cold path harmless anyway.

        Not gated here, because `verify` below is and an `asyncio.Semaphore`
        is not reentrant -- taking it twice on one call would deadlock the
        path this exists to keep fast. So the bound is one permit per decoy
        verification, which is the same permit a real one costs, which is the
        arithmetic the decoy is supposed to preserve.

        The one operation outside the bound is the UNCACHED `_decoy_hash`,
        and it is uncached exactly once per process. `app.main.lifespan`
        awaits `warm_password_hashing` before it yields, so nothing is served
        until it is a dict lookup; a burst that arrived first would find
        `functools.cache` making no promise about concurrent misses and could
        compute it more than once. Warming is what closes that, not this.
        """
        decoy = await asyncio.to_thread(_decoy_hash)

        await self.verify(password_hash=decoy, password=password)


@cache
def _decoy_hash() -> str:
    """An argon2id hash no password will ever match, built once per process.

    Once, and shared, for a timing reason of its own. Computed per hasher
    instance it would be computed per request, so the first unknown-address
    login of every request would pay for two hashes where a wrong-password
    login pays for one -- twice the signal the decoy exists to remove.

    Of random bytes, not a constant, so that the decoy differs between
    processes and no attacker can precompute anything about it. `app.main`
    warms this during startup so that the first request to need it is not
    the one that pays for it.
    """
    return Argon2Hasher().hash(secrets.token_urlsafe(DECOY_PASSWORD_BYTES))


async def warm_password_hashing() -> None:
    """Build the decoy hash now, off the event loop.

    Called from the application lifespan. Argon2 costs what it costs; the
    only question is whether a user's first failed login pays it or startup
    does.
    """
    await asyncio.to_thread(_decoy_hash)
