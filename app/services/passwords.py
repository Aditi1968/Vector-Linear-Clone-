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
puts it on the default executor instead, which is bounded (min(32, cpus+4)
workers), so the number of hashes running at once is bounded too. That bound
is also a memory bound worth knowing about: each concurrent hash holds
64 MiB while it runs.
"""

import asyncio
import secrets
from functools import cache
from typing import Protocol

from argon2 import PasswordHasher as Argon2Hasher
from argon2.exceptions import VerifyMismatchError


# Length of the throwaway password behind the decoy hash, in bytes of
# entropy. It is never checked against anything, so this only has to be far
# beyond guessing; 32 bytes matches the session token.
DECOY_PASSWORD_BYTES = 32


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
        """
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
