"""The two halves of a session token: minting one, and reducing it to a key.

These are eight lines of code and they decide whether a session can be
guessed or read out of a database dump, so they are tested as if they were
larger. What is asserted here is not that hashlib works -- it does -- but
that the *contract* between the cookie and the column holds: the digest is
the fixed width the schema constrains, one token always reduces to one key
so a lookup can find it, and nothing about the token survives into the value
that gets stored.
"""

import hashlib

from app.services.tokens import (
    TOKEN_ENTROPY_BYTES,
    TOKEN_HASH_BYTES,
    generate_session_token,
    hash_session_token,
)


# Enough draws that a generator repeating itself, or keying off a
# second-resolution clock, shows up as a collision here. Cheap: token
# generation is a read from the OS CSPRNG.
SAMPLE_SIZE = 1000

# base64url packs 3 bytes into 4 characters, unpadded by token_urlsafe.
EXPECTED_TOKEN_LENGTH = -(-TOKEN_ENTROPY_BYTES * 4 // 3)


def test_every_generated_token_is_different():
    """A repeat would be one user handed another user's session.

    sessions_token_hash_key would refuse the second INSERT, so the failure
    would surface as a log-in that errors rather than as a breach -- but the
    generator is the thing that must not repeat, and the constraint is the
    backstop, not the guarantee.
    """
    tokens = {generate_session_token() for _ in range(SAMPLE_SIZE)}

    assert len(tokens) == SAMPLE_SIZE


def test_a_token_carries_the_entropy_it_claims_to():
    """256 bits, rendered base64url.

    Asserted against the constant rather than a literal, so that lowering
    TOKEN_ENTROPY_BYTES is a change to a documented number and not a quiet
    weakening of every session the service issues.
    """
    assert TOKEN_ENTROPY_BYTES == 32
    assert len(generate_session_token()) == EXPECTED_TOKEN_LENGTH


def test_a_token_is_url_and_cookie_safe():
    """It travels in a Set-Cookie header, which forbids most punctuation.

    A generator that emitted ';' or ',' would produce tokens the browser
    truncates or splits, so a session would work for some users and not
    others depending on what the CSPRNG handed out.
    """
    alphabet = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")

    for _ in range(SAMPLE_SIZE):
        assert set(generate_session_token()) <= alphabet


def test_hashing_is_deterministic():
    """The property every lookup depends on.

    A salted or otherwise non-deterministic digest could not be found by
    equality at all, and the session would be unusable from the moment it
    was issued.
    """
    token = generate_session_token()

    assert hash_session_token(token) == hash_session_token(token)


def test_different_tokens_hash_differently():
    digests = {hash_session_token(generate_session_token()) for _ in range(SAMPLE_SIZE)}

    assert len(digests) == SAMPLE_SIZE


def test_the_digest_is_the_width_the_schema_constrains():
    """sessions_token_hash_length rejects anything but 32 bytes.

    Bytes, not a hex string: `.digest()` and `.hexdigest()` are one method
    call apart, and the hex form is 64 characters, which the constraint
    would refuse on every insert.
    """
    digest = hash_session_token(generate_session_token())

    assert isinstance(digest, bytes)
    assert len(digest) == TOKEN_HASH_BYTES == 32


def test_the_digest_is_sha256_of_the_token():
    """Pinned against hashlib directly, not against itself.

    A test that only compared `hash_session_token` to its own output would
    pass for any function at all, including one that returned the token.
    """
    token = generate_session_token()

    assert hash_session_token(token) == hashlib.sha256(token.encode("utf-8")).digest()


def test_the_stored_digest_contains_nothing_of_the_token():
    """What a database dump does and does not reveal.

    The token is ASCII and the digest is bytes, so this is close to
    tautological -- and it is the tautology that would stop being true if
    anyone ever "simplified" the column to store the token itself, which is
    a change that would break no other test in this suite.
    """
    token = generate_session_token()
    digest = hash_session_token(token)

    assert token.encode("utf-8") not in digest
    assert token not in digest.hex()
