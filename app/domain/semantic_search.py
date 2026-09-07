from dataclasses import dataclass
from typing import Final
from uuid import UUID

from app.domain.issues import IssueEntity


# The width of every vector this application stores, matching `vector(384)` in
# migrations/025_semantic_search.sql.
#
# 384 because that is the output width of the small English sentence encoders
# worth running locally on a CPU -- all-MiniLM-L6-v2 and bge-small-en-v1.5 are
# both 384 -- so installing a real model needs no migration. The deterministic
# fallback in app/services/embeddings.py hashes into the same width for exactly
# that reason.
EMBEDDING_DIMENSIONS: Final = 384

# The constant in reciprocal rank fusion, from Cormack, Clarke & Buettcher
# (2009), who introduced RRF and found 60 to work across every collection they
# tried without tuning.
#
# WHY FUSION BY RANK AT ALL, rather than a weighted sum of the two scores:
# because the two scores are not measured in the same thing and there is no
# honest conversion between them. `ts_rank` is a term-density number whose
# scale depends on the document's length and on how many query terms it
# carries; cosine similarity is an angle. "0.7 lexical" and "0.7 semantic" are
# not comparable quantities, so `0.5 * lexical + 0.5 * semantic` is a formula
# that looks principled and is actually two arbitrary constants plus an
# arbitrary normalisation -- and it changes behaviour whenever either scorer's
# distribution shifts, which `ts_rank`'s does with corpus size.
#
# RRF discards the magnitudes and keeps the only thing both retrievers produce
# meaningfully: the order. A document's contribution from one retriever is
# 1 / (RRF_K + rank), so being first is worth 1/61, second 1/62, and an item
# both retrievers rank highly beats an item either one ranks highest alone --
# which is the entire behaviour wanted from a hybrid.
#
# What K controls is how sharply the top of a list is favoured. Small K makes
# rank 1 overwhelmingly dominant, so the fusion degenerates towards "whichever
# retriever was more confident"; large K flattens the curve until every
# retrieved document counts about the same and the fusion becomes a vote on
# membership rather than on order. 60 sits far enough up that ranks 1 and 2
# differ by under 2%, which is the property that makes the result stable rather
# than jittery when one retriever's ordering shifts slightly.
RRF_K: Final = 60

# How many candidates each retriever contributes to the fusion.
#
# Not the same as the page size, and it must not be: fusion can only re-rank
# what it was given, so drawing `first` candidates from each side would mean a
# document ranked 21st lexically and 1st semantically never reaches the fusion
# at all when `first` is 20 -- which is precisely the document a hybrid search
# exists to surface. Drawing 100 from each makes the fusion's input three to
# five pages deep on either side.
#
# Bounded rather than unbounded, because each side's LIMIT is what keeps the
# work proportional to a page: without it the semantic arm sorts every
# embedding in the workspace and the lexical arm ranks every match, both to
# produce twenty rows.
FUSION_DEPTH: Final = 100

# The floor a duplicate suggestion has to clear.
#
# A cosine similarity is defined for every pair of vectors, so without a floor
# the "most similar issue" is always something -- and an interface that always
# claims to have found a possible duplicate is one people stop reading. 0.6 is
# a product default rather than a measured threshold, and it is deliberately
# high enough that unrelated issues do not clear it under the hashing fallback,
# whose similarity is driven by shared vocabulary.
#
# Callers may not lower it. It is not a parameter for the same reason `first`
# is bounded: a client that could ask for suggestions above 0.0 would be asking
# for the whole workspace sorted by a number it would then present as
# "possible duplicates".
MIN_DUPLICATE_SIMILARITY: Final = 0.6


def embedding_text(title: str, description: str | None) -> str:
    """The one string an issue is embedded from.

    Title and description joined by a blank line, which is what a person would
    see -- an encoder trained on prose does better on "Deploy fails\n\nthe
    build dies at step three" than on either half alone, and the title alone
    would throw away the sentence that says what actually broke.

    Pure application code, and the ONLY place this concatenation is written:
    the text that is embedded and the text a stored embedding claims to
    describe have to be the same text, and two spellings of it would agree
    only until one of them was edited.

    Note that this is deliberately NOT the expression
    `issues.embedding_source_digest` is computed from. That column digests the
    two fields separately and concatenates the digests, precisely so that no
    separator can make two different (title, description) pairs collide; a
    digest over THIS string would have that flaw. The two only need to agree on
    which fields they read, not on how they join them.
    """
    if not description:
        return title

    return f"{title}\n\n{description}"


@dataclass(frozen=True, slots=True)
class EmbeddingSource:
    """One issue that needs an embedding, and the text to build it from.

    Carries `source_digest` -- the digest the row had when it was read -- so
    the write that follows can name it and be refused if the issue was edited
    in between. Without that, a title changed while the model was running would
    be overwritten by an embedding of the title it used to have, and the digest
    stored beside it would say the vector was current.

    Not an `IssueEntity`: the sweep reads four columns and the entity has
    nineteen, and every one of the other fifteen would be a column the
    regeneration query had to fetch for nothing.
    """

    issue_id: UUID
    source_digest: str
    title: str
    description: str | None

    @property
    def text(self) -> str:
        return embedding_text(self.title, self.description)


@dataclass(frozen=True, slots=True)
class DuplicateSuggestion:
    """One issue that MIGHT be the same as the one being written, and how close.

    `similarity` is a similarity and never a probability, a confidence or a
    verdict. It is `1 - cosine_distance` between two embeddings: a statement
    about how close two pieces of text sit in one model's coordinate system,
    under whichever model happened to produce both vectors. It does not know
    what a duplicate is, it has never seen this workspace's issues resolved,
    and 0.9 does not mean "90% likely to be a duplicate" -- it means the two
    texts point in almost the same direction, which for two independently
    written bug reports of the same bug is common and for two unrelated issues
    from the same team is not rare either.

    So the field is named for what it measures. Anything that renders it has to
    present a suggestion the reader judges, never an assertion the product
    makes -- and nothing in this system may act on it: no auto-close, no
    auto-merge, no auto-link. The one thing it is for is putting a candidate in
    front of a person before they press create.
    """

    issue: IssueEntity
    similarity: float
