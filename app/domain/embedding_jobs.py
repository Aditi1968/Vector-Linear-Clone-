from dataclasses import dataclass
from typing import Final
from uuid import UUID

from app.domain.semantic_search import EmbeddingSource


# How many failed attempts an issue gets before the queue stops offering it.
#
# THE BOUND THAT MAKES A POISON ROW STOP. An issue whose text makes the embedder
# raise -- a tokeniser bug, a pathological input, a model that only falls over
# on one string -- is retried forever by an anti-join, which has no memory. This
# is that memory's ceiling, and without it one such row consumes a slot in every
# sweep this installation ever runs.
#
# Five and not three, because the failures this is most likely to see are
# transient: a model file being replaced under a running process, a machine
# briefly out of memory. Five attempts with the backoff below spans about eight
# minutes, which is long enough to ride out a restart and short enough that a
# genuinely broken row is out of the way before it has cost anything.
#
# It lives here rather than in a CHECK constraint in
# migrations/028_embedding_jobs.sql, deliberately: a retry bound is a policy a
# deployment might want to change, and a policy pinned in a constraint can only
# be changed by a migration. What the migration pins is the RANGE.
MAX_ATTEMPTS: Final = 5

# The first retry's delay, and the ceiling the doubling stops at.
#
# Exponential, so a transient fault is retried almost immediately and a
# permanent one stops costing anything: 30s, 1m, 2m, 4m. The cap exists because
# doubling without one reaches days, and a row that is only due again next
# Tuesday is indistinguishable from a row nobody will ever look at -- while
# `MAX_ATTEMPTS` has already made "give up" a state this queue can express
# honestly.
BASE_RETRY_SECONDS: Final = 30.0
MAX_RETRY_SECONDS: Final = 3600.0

# How long a claimed job is invisible to other workers.
#
# THE LEASE, and it is the only thing that stops two workers paying full model
# time for the same vector. A worker claims by pushing `next_attempt_at` this
# far forward and then releases the connection to run the model -- see
# `EmbeddingJobRepository.claim` -- so a worker that crashes mid-batch leaves
# rows that simply become due again, with nothing to clean up and nobody to
# notice.
#
# Five minutes, which is far longer than a batch takes and is meant to be. The
# cost of a lease that is too long is a delay after a crash; the cost of one
# that is too short is two workers embedding the same issue, which is the
# failure this exists to prevent. Between a slow recovery and a wasted GPU
# minute, the slow recovery is the cheaper mistake.
LEASE_SECONDS: Final = 300.0


def retry_delay_seconds(attempts: int) -> float:
    """How long to wait before a job that has now failed `attempts` times is due.

    `attempts` counts the failure just recorded, so the first failure waits
    BASE_RETRY_SECONDS rather than none: a job that failed and is immediately
    due again is a spin, not a retry.

    Pure arithmetic on purpose. The alternative -- computing the interval in the
    UPDATE that records the failure -- would put the backoff policy in SQL,
    where it could only be read by running it.
    """
    # `2.0` and not `2`: an int raised to an int power is only an int when the
    # exponent is non-negative, so mypy types the integer spelling as `Any` and
    # this function stops being checked at all.
    return min(BASE_RETRY_SECONDS * 2.0 ** (attempts - 1), MAX_RETRY_SECONDS)


@dataclass(frozen=True, slots=True)
class ClaimedEmbeddingJob:
    """One issue a worker has leased, with the text it must embed NOW.

    Carries `workspace_id`, which nothing else in the embedding path has to:
    every other read in this feature is issued for a caller whose workspace is
    already known, and a background worker is the one reader in this system with
    no tenant of its own. It claims across every workspace at once, so the
    workspace it then writes an embedding for has to come off the claimed row --
    never from a caller, and never from an ambient default. See
    migrations/028_embedding_jobs.sql on why the composite foreign key is what
    makes that value trustworthy.

    `source` is an `EmbeddingSource` and is deliberately built from the issue's
    LIVE columns rather than from the queue row. The stored
    `embedding_jobs.source_digest` records what the text looked like when the
    job was queued, which is a fact about the queue; what the worker must embed
    is what the issue says now. Reusing the type means the text a worker builds
    is `embedding_text` and not a second spelling of it.

    `attempts` is the count BEFORE this attempt, straight off the row, so a
    worker recording a failure knows which retry it is on without a second read.
    """

    workspace_id: UUID
    attempts: int
    source: EmbeddingSource


@dataclass(frozen=True, slots=True)
class IndexingState:
    """How much of one workspace's semantic index actually exists.

    THE POINT OF THIS TYPE is that "no duplicates found" and "nothing is
    indexed yet" are different sentences, and before this existed a client had
    no way to tell them apart -- `issueDuplicateSuggestions` answers with an
    empty list for both. A fresh install has no embeddings at all, so every
    duplicate check on it was answering a question it had not been able to ask.

    The three counts partition this workspace's live issues exactly:

      * `indexed` -- an embedding exists under the current model AND matches
        the issue's current text. This is the same equality every read in
        `EmbeddingRepository` uses, so this number is what semantic search can
        actually see, not what has ever been written;
      * `pending` -- no current embedding, and not given up on. The index is
        still building for these;
      * `failed` -- no current embedding, and the queue has stopped trying. See
        MAX_ATTEMPTS.

    `enabled` is whether this DEPLOYMENT has an embedder wired at all. It is the
    one place in the search feature that reports that, and doing so is
    deliberate: `suggestDuplicates` and `embeddingsRefresh` keep "no model" and
    "nothing found" indistinguishable, because for those two a client could only
    use the difference to fingerprint the server. Here the difference is the
    entire question being asked -- a UI that cannot tell "still building" from
    "this deployment does not do this" has to guess, and it guesses wrong on
    exactly the fresh installs where it matters.

    All three counts are zero when `enabled` is False, and that is honest rather
    than evasive: freshness is defined against the model doing the asking, and
    with no embedder there is no model to ask about.

    No error text. `embedding_jobs.last_error` is an exception's own message,
    which is precisely the raw internal detail this project's error rules keep
    away from clients; an operator reads it from the table or from the logs.
    """

    indexed: int
    pending: int
    failed: int
    enabled: bool
