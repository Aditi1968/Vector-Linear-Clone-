import functools
import hashlib
import importlib
import importlib.util
import math
import re
from collections.abc import Sequence
from typing import Final, Protocol

from app.domain.semantic_search import EMBEDDING_DIMENSIONS


class Embedder(Protocol):
    """Whatever turns text into vectors, for as long as this process runs.

    Two implementations exist and both are local, free and offline: a real
    sentence encoder if one happens to be installed, and a deterministic
    hashing fallback if not. Nothing here calls a paid API and nothing here
    opens a socket -- an embedding that depended on a third party would put a
    network round trip on the path of every search and a vendor outage on the
    path of every issue created.

    `name` is stored in `issue_embeddings.model` beside every vector this
    object produces, and is the mechanism that makes swapping implementations
    safe: two embedders embed into two different coordinate systems, a cosine
    distance between them is a meaningless number rather than an error, and
    every read filters on the name of the embedder doing the asking. Change the
    name and every stored vector stops matching, which re-queues the workspace
    rather than serving it nonsense.
    """

    @property
    def name(self) -> str: ...

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """One unit vector of EMBEDDING_DIMENSIONS floats per input, in order.

        A batch rather than a single string, because a real model's cost is
        dominated by the per-call overhead and the refresh sweep embeds
        hundreds of issues at a time.
        """
        ...


# Words, lowercased. Two characters minimum: single letters carry no signal and
# would spend buckets on "a" and "I".
_TOKEN = re.compile(r"[a-z0-9]{2,}")

# The prefix length the hashing embedder also emits per token, as a stand-in
# for stemming. "deploy", "deploys", "deployed" and "deploying" all share
# "deploy" at six characters, so they land on a common bucket and score against
# one another -- which is the one thing a bag of exact words cannot do.
_STEM_LENGTH: Final = 6

# The sentence encoder used if the library is installed. Small, English,
# CPU-runnable, permissively licensed, and 384-dimensional -- which is what
# EMBEDDING_DIMENSIONS and `vector(384)` in migration 025 are sized for.
SENTENCE_TRANSFORMER_MODEL: Final = "sentence-transformers/all-MiniLM-L6-v2"

# What the fallback calls itself. Versioned in the name, because the bucket
# assignment IS the coordinate system: changing _STEM_LENGTH, the tokeniser or
# the digest moves every issue to a different point, and vectors written before
# and after such a change must never be compared. Bump this whenever any of
# those change, and the stored rows re-queue themselves.
HASHING_EMBEDDER_NAME: Final = "hashing-v1"


def vector_literal(values: Sequence[float]) -> str:
    """A vector in pgvector's text input format: `[1.0,2.0,3.0]`.

    Bound as a parameter and cast with `$n::vector` on the SQL side rather than
    registered as an asyncpg codec. A codec would be the tidier answer and
    needs the extension's OID, which means a catalog lookup per connection and
    a pool whose connections are only usable after it -- for a value this
    application writes in exactly one statement and reads in three.

    `repr` and not `str(round(...))`: this text is parsed straight back into
    the float it came from, and rounding here would quietly change the stored
    vector from the one the model produced.
    """
    return "[" + ",".join(repr(float(value)) for value in values) + "]"


class HashingEmbedder:
    """A deterministic, dependency-free embedding of text into 384 dimensions.

    WHAT THIS IS, said plainly so nobody mistakes it for what it is not: it is
    the hashing trick -- signed feature hashing over tokens and six-character
    prefixes, L2-normalised. It has never seen a corpus and knows no synonyms,
    so "the build keeps dying" and "CI fails intermittently" are as far apart to
    it as any two unrelated sentences. It is a bag of words in a smaller space,
    NOT a semantic model.

    Why ship it anyway. It makes the feature work end to end with zero
    dependencies -- which matters here because both dependency locks are fully
    pinned AND hashed and `tests/test_phase1a2_gates.py` enforces that, so a
    130MB torch dependency is not a line anybody adds casually. It is also
    genuinely useful on its own: cosine over hashed tokens tolerates word
    order, length and partial overlap in a way `websearch_to_tsquery`'s AND of
    every term does not, so "deploy pipeline flaky retries" finds "Retries on
    the flaky deploy pipeline" at a high similarity while the lexical arm needs
    all four stems present.

    Install `sentence-transformers` and `load_embedder` picks it up instead,
    the model name changes, every stored vector stops matching and the next
    refresh sweep rebuilds them. No migration, no backfill script, no flag.

    Deterministic across processes and machines: `blake2b`, not the built-in
    `hash()`, whose string seed is randomised per process. A per-process
    coordinate system would mean a vector written by one worker being compared
    against a query embedded by another, which is the same class of error the
    `model` column exists to prevent and would be invisible.
    """

    def __init__(self, dimensions: int = EMBEDDING_DIMENSIONS):
        self._dimensions = dimensions

    @property
    def name(self) -> str:
        return HASHING_EMBEDDER_NAME

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._embed_one(text) for text in texts]

    def _embed_one(self, text: str) -> list[float]:
        vector = [0.0] * self._dimensions

        for feature in self._features(text):
            # One digest, read twice: the low bits pick the bucket and one
            # further bit picks the sign. A signed hashing scheme is what keeps
            # collisions from being purely additive -- two unrelated features
            # sharing a bucket cancel about half the time instead of always
            # reinforcing each other, so a collision costs a little accuracy
            # rather than inventing a similarity.
            digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=5).digest()
            bucket = int.from_bytes(digest[:4], "big") % self._dimensions
            sign = 1.0 if digest[4] & 1 else -1.0

            vector[bucket] += sign

        norm = math.sqrt(sum(value * value for value in vector))

        if norm == 0.0:
            # Reachable two ways: text with no token of two characters ("a b"),
            # and -- vanishingly rarely -- a set of features whose signs cancel
            # exactly. Both would otherwise store the zero vector, whose cosine
            # distance to anything is a division by zero that pgvector answers
            # with NaN, and NaN sorts unpredictably rather than raising. A
            # single feature over the whole raw string gives a unit vector that
            # is at least honest: it matches other text exactly like it and
            # nothing else.
            digest = hashlib.blake2b(text.encode("utf-8"), digest_size=4).digest()

            vector[int.from_bytes(digest, "big") % self._dimensions] = 1.0

            return vector

        return [value / norm for value in vector]

    @staticmethod
    def _features(text: str) -> list[str]:
        """Each token, plus a prefix of it standing in for a stem.

        The prefix is emitted as a distinct feature rather than instead of the
        token, so an exact word match still scores above a shared-prefix match:
        "deploy" against "deploy" hits both buckets, "deploy" against
        "deployment" hits only the shared one.
        """
        features = []

        for token in _TOKEN.findall(text.lower()):
            features.append(token)

            if len(token) > _STEM_LENGTH:
                features.append(token[:_STEM_LENGTH])

        return features


class SentenceTransformerEmbedder:
    """A real sentence encoder, if this deployment happens to have one.

    Constructed only through `load_embedder`, which is the one place that
    decides whether the library is present. The import is done with
    `importlib.import_module` rather than a top-level `import
    sentence_transformers` for two reasons that both matter: a module-level
    import of an absent package is an ImportError at application startup --
    which is exactly the failure this whole design exists to avoid -- and mypy
    is configured with a single library override (`asyncpg.*`) that
    `tests/test_phase1a2_gates.py` asserts is the only one, so a static import
    of a package with no stubs would need a second override or a `type: ignore`
    and the gate refuses both.

    The model is loaded once, at construction. `encode` then runs on the CPU
    with no network access -- the weights are already on disk by the time this
    class exists, because `SentenceTransformer(...)` is what downloaded them,
    and that happens during `load_embedder` at startup rather than inside a
    request.
    """

    def __init__(self, model_name: str = SENTENCE_TRANSFORMER_MODEL):
        module = importlib.import_module("sentence_transformers")

        self._model_name = model_name
        self._model = module.SentenceTransformer(model_name)

    @property
    def name(self) -> str:
        return self._model_name

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        # `normalize_embeddings=True` because the whole schema assumes unit
        # vectors: migration 025 argues for `<=>` on the grounds that it is
        # invariant to magnitude, and `1 - (a <=> b)` is only a cosine in
        # [-1, 1] when both operands are normalised.
        encoded = self._model.encode(
            list(texts),
            normalize_embeddings=True,
            convert_to_numpy=False,
        )

        # Converted element by element rather than handed back as whatever the
        # library returned. `encode` answers with tensors or arrays depending
        # on its arguments and its version, and asyncpg would have to bind
        # whatever that is; this makes the boundary a list of floats and makes
        # a shape surprise fail here rather than in a repository.
        return [[float(value) for value in row] for row in encoded]


@functools.cache
def load_embedder() -> Embedder:
    """The best embedder this deployment can build, without ever failing.

    Cached, and that is load-bearing rather than an optimisation:
    `app.graphql.context.get_context` runs per request, and a
    SentenceTransformer constructed per request would load a 90MB model from
    disk on every GraphQL call. One embedder per process, built on the first
    request that asks -- not at import, so this module stays importable, and a
    deployment that never searches never pays for a model.

    Tries the real model first and falls back to hashing, so the application
    starts either way. That is the requirement this function exists for: a
    missing optional dependency must not be a startup error, and semantic
    search must degrade rather than break.

    The failure modes caught are the ones a missing or broken install actually
    produces -- ImportError for an absent package or a broken transitive
    dependency, OSError for weights that cannot be downloaded or read,
    RuntimeError and ValueError for a model that loads and then refuses (no
    compatible backend, an incompatible checkpoint). They are named rather than
    caught as `Exception`, so a genuine bug in this module still surfaces as a
    crash instead of being silently downgraded to "no model installed".

    Note what this does NOT decide: whether semantic search runs at all. That
    is `SearchService`'s, and it turns on whether an embedder was PASSED to it
    -- see its constructor. A deployment wanting lexical-only search wires None
    rather than making this function lie about what it found.
    """
    if importlib.util.find_spec("sentence_transformers") is None:
        return HashingEmbedder()

    try:
        return SentenceTransformerEmbedder()
    except (ImportError, OSError, RuntimeError, ValueError):
        return HashingEmbedder()
