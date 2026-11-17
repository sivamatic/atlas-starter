"""Rerank: a second, closer look at the candidates retrieval found (Unit 4).

Hybrid search with RRF is good at finding candidates, but it ranks by position in two lists,
not by how well a chunk answers the question. A reranker reads each candidate next to the
question and reorders them. Retrieval fetches a bigger pool (ATLAS_RERANK_POOL, default 20),
the reranker keeps the best top_k.

  off            no reranking: the fused order from week 3
  overlap        deterministic and offline: how many question words the chunk covers, whether its
                 heading matches, and how close together the matches sit, blended with the fused score
  cross-encoder  a small cross-encoder model (optional: pip install sentence-transformers;
                 downloads the model on first use). Better on paraphrases, costs CPU or GPU time.

Measure before you keep it: tools/eval_gate.py --compare runs the golden set with and without.
"""
from . import telemetry
from .retrieval import terms

DEFAULT = "overlap"   # used when ATLAS_RERANK is not set
MODES = ("off", "overlap", "cross-encoder")


def stem(word: str) -> str:
    """A very small stemmer, so "drops" and "dropping" both match "drop"."""
    for suffix in ("ing", "ed", "es", "s"):
        if len(word) > len(suffix) + 3 and word.endswith(suffix):
            word = word[:-len(suffix)]
            if suffix in ("ing", "ed") and len(word) > 3 and word[-1] == word[-2] and word[-1] not in "aeiouls":
                word = word[:-1]   # dropping -> dropp -> drop
            return word
    return word


def overlap_score(query: str, chunk: dict) -> float:
    """How well chunk matches query, from 0.0 (nothing in common) to 1.0.

    q    = the distinct stemmed query terms: {stem(t) for t in terms(query)}. If empty, return 0.0.
    head = stemmed terms of chunk["title"] + " " + chunk["section"]
    body = stemmed terms of chunk["text"], as a list (positions matter)

    coverage  = share of q found in head or body
    heading   = share of q found in head
    proximity = for the query terms that occur in body, the smallest window of body positions that
                contains at least two different ones: 1 / window_length (two adjacent words -> 0.5).
                0.0 if fewer than two different query terms occur in body.

    return 0.6 * coverage + 0.25 * heading + 0.15 * proximity
    """
    q = {stem(t) for t in terms(query)}
    if not q:
        return 0.0
    head = {stem(t) for t in terms(chunk["title"] + " " + chunk["section"])}
    body = [stem(t) for t in terms(chunk["text"])]
    coverage = len(q & (head | set(body))) / len(q)
    heading = len(q & head) / len(q)
    hits = [(i, t) for i, t in enumerate(body) if t in q]
    window = None
    for a in range(len(hits)):
        for b in range(a + 1, len(hits)):
            if hits[b][1] != hits[a][1]:
                size = hits[b][0] - hits[a][0] + 1
                window = size if window is None else min(window, size)
                break
    proximity = 1.0 / window if window else 0.0
    return 0.6 * coverage + 0.25 * heading + 0.15 * proximity


class OverlapReranker:
    name = "overlap"

    def __init__(self, weight: float = 0.7):
        self.weight = weight   # how much the overlap score counts against the fused score

    def rerank(self, query: str, hits: list[dict]) -> list[dict]:
        """Return new dicts, best first, each with "rerank_score" added ("score" stays the fused score).

        rerank_score = weight * overlap_score(query, hit) + (1 - weight) * hit["score"] / best fused score,
        rounded to 6 decimals. Sort by rerank_score, highest first, ties by id A-Z. Empty in, empty out.
        """
        if not hits:
            return []
        top = max(h["score"] for h in hits) or 1.0
        out = [{**h, "rerank_score": round(self.weight * overlap_score(query, h) + (1 - self.weight) * h["score"] / top, 6)}
               for h in hits]
        return sorted(out, key=lambda h: (-h["rerank_score"], h["id"]))


class CrossEncoderReranker:
    """Optional. A cross-encoder reads (question, chunk) together and scores relevance.
    Never required: Atlas runs and tests without it."""
    name = "cross-encoder"

    def __init__(self, model: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"):
        try:
            from sentence_transformers import CrossEncoder
        except ImportError as e:
            raise RuntimeError("ATLAS_RERANK=cross-encoder needs: pip install sentence-transformers") from e
        self.model = CrossEncoder(model)

    def rerank(self, query: str, hits: list[dict]) -> list[dict]:
        if not hits:
            return []
        scores = self.model.predict([(query, h["title"] + ". " + h["section"] + ". " + h["text"]) for h in hits])
        out = [{**h, "rerank_score": round(float(s), 6)} for h, s in zip(hits, scores)]
        return sorted(out, key=lambda h: (-h["rerank_score"], h["id"]))


def from_settings(s):
    mode = s.rerank or DEFAULT
    if mode == "off":
        return None
    if mode == "overlap":
        return OverlapReranker()
    if mode == "cross-encoder":
        return CrossEncoderReranker(s.rerank_model)
    raise ValueError("ATLAS_RERANK must be one of %s, got %r" % (", ".join(MODES), mode))


class RerankingRetriever:
    """Retrieval with an optional rerank stage, and the retrieval and rerank spans.
    Same search() interface as retrieval.Retriever, so ask() doesn't change."""

    def __init__(self, retriever, reranker=None, pool: int = 20):
        self.retriever, self.reranker, self.pool = retriever, reranker, pool

    @property
    def store(self):
        return self.retriever.store

    @property
    def embed(self):
        return self.retriever.embed

    def search(self, query: str, groups, k: int = 4) -> list[dict]:
        want = max(k, self.pool) if self.reranker else k
        with telemetry.span("retrieval", {"atlas.retrieval.k": k, "atlas.retrieval.pool": want,
                                          "atlas.retrieval.store": type(self.store).__name__}) as sp:
            hits = self.retriever.search(query, groups, k=want)
            sp.update({"atlas.retrieval.hits": len(hits)})
        if self.reranker is None or not hits:
            return hits[:k]
        with telemetry.span("rerank", {"atlas.rerank.name": self.reranker.name, "atlas.rerank.candidates": len(hits)}) as sp:
            out = self.reranker.rerank(query, hits)[:k]
            sp.update({"atlas.rerank.kept": len(out), "atlas.rerank.doc_ids": ",".join(dict.fromkeys(h["doc_id"] for h in out))})
        return out


def wrap(retriever, settings):
    return RerankingRetriever(retriever, from_settings(settings), settings.rerank_pool)
