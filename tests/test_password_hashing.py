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

import pytest
from argon2 import extract_parameters
from argon2.exceptions import InvalidHashError

from app.services.passwords import Argon2PasswordHasher, _decoy_hash


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
