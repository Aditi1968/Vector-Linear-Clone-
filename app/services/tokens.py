"""Session tokens: how one is minted, and how it is turned into a lookup key.

Two functions, deliberately apart from AuthService, because between them
they define the entire relationship between the string in a user's cookie
and the bytes in the database -- and that relationship is the thing an
auditor needs to be able to read in one screen.

Opaque random tokens, not JWTs. A JWT would put the session's contents in
the client's hands and make revocation a matter of maintaining a deny list
that has to be consulted anyway; a random token has no contents, so
revocation is a DELETE and expiry is a column.
"""

import hashlib
import secrets


# Bytes of entropy behind each token. `secrets.token_urlsafe` renders them
# base64url, so 32 bytes is a 43-character string.
#
# 32 rather than 16 because this value alone authenticates a request: there
# is no second factor behind it and no password to also guess. At 256 bits
# there is nothing to say about brute force beyond that it is not a thing
# that happens, which is the property the SHA-256 digest below relies on.
TOKEN_ENTROPY_BYTES = 32

# Length of the digest `hash_session_token` produces, so that callers and
# the sessions_token_hash_length constraint agree on one number.
TOKEN_HASH_BYTES = 32


def generate_session_token() -> str:
    """A fresh session token.

    `secrets`, never `random`: the latter is a Mersenne Twister seeded from
    the clock, and 624 observed outputs are enough to reconstruct its state
    and predict every token it will ever produce afterwards.
    """
    return secrets.token_urlsafe(TOKEN_ENTROPY_BYTES)


def hash_session_token(token: str) -> bytes:
    """The digest stored in `sessions.token_hash` for `token`.

    SHA-256, unsalted, and both of those are the point.

    Unsalted because the digest has to be looked up by equality: a salted
    hash would have to be recomputed per candidate row, turning every
    authenticated request into a scan of the sessions table. That is
    affordable here for the same reason argon2 is unnecessary -- the input
    is 256 bits of CSPRNG output, so there is no dictionary to attack and no
    rainbow table that could ever be built. Salting defends low-entropy
    inputs; this input has no low-entropy space to defend.

    What the digest does buy is that a copy of the database is not a set of
    live sessions. An attacker holding these bytes can identify a session
    but cannot present one, because SHA-256 is not invertible and the
    preimage is unguessable.

    Raw bytes rather than hex, matching the BYTEA column: half the storage
    and index size, and no chance of two spellings of the same digest
    (upper- and lowercase hex) being stored as different rows.
    """
    return hashlib.sha256(token.encode("utf-8")).digest()
