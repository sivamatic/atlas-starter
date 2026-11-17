"""Hybrid retrieval: keyword search + vector search, fused with Reciprocal Rank Fusion.

Two stores with the same interface:
  MemoryStore  in-process BM25 + cosine. For tests and local dev.
  PgStore      Postgres: full-text search (tsvector) + pgvector, in one database you
               already know how to back up, secure and monitor.

Both apply the permission filter INSIDE the search, before ranking. Filtering after you
take the top k can return nothing (or leak counts); see Unit 3 lesson 4.
"""
import math
import re
from collections import Counter

from .embed import tokens

STOP = set("a an and are as at be but by can do does for from how i if in is it its me my of on or our so "
           "that the their then there this to was we what when where which who why will with you your".split())


def terms(text: str) -> list[str]:
    return [t for t in tokens(text) if t not in STOP]


def allowed(chunk_acl, groups) -> bool:
    return bool(set(chunk_acl) & set(groups))


# ---------------------------------------------------------------- fusion

def rrf(rankings: list[list[str]], k: int = 60) -> list[tuple[str, float]]:
    """Reciprocal Rank Fusion.

    rankings: lists of ids, best first (e.g. [keyword_ids, vector_ids]).
    An id at 1-based position r in a list earns 1 / (k + r) from that list; sum across lists.
    If an id repeats inside one list, only its first (best) position counts.
    Return [(id, score)] sorted by score, highest first; break ties by id A-Z so results are stable.
    """
    scores: dict[str, float] = {}
    for ranking in rankings:
        seen = set()
        for rank, cid in enumerate(ranking, start=1):
            if cid in seen:
                continue
            seen.add(cid)
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))


class Retriever:
    def __init__(self, store, embed):
        self.store, self.embed = store, embed

    def search(self, query: str, groups, k: int = 4, n: int = 20) -> list[dict]:
        """Return up to k chunks the user may read, best first, each with a "score" (the RRF score).

        1. No groups means nothing is readable: return [] without searching.
        2. Embed the query once: self.embed([query])[0].
        3. Ask the store for the top n by keyword and the top n by vector, both filtered by groups.
        4. Fuse the two id lists with rrf() and keep the best k.
        5. Fetch the chunks with self.store.get(ids) and return them in fused order with "score".
           Defence in depth: drop any chunk whose acl the user can't read, even though the store
           already filtered. A bug in one layer shouldn't become a data leak.
        """
        if not groups:
            return []
        qv = self.embed([query])[0]
        kw = self.store.keyword(query, groups, n)
        vec = self.store.vector(qv, groups, n)
        fused = rrf([kw, vec])[:k]
        chunks = self.store.get([cid for cid, _ in fused])
        return [{**chunks[cid], "score": round(score, 6)} for cid, score in fused
                if cid in chunks and allowed(chunks[cid]["acl"], groups)]


# ---------------------------------------------------------------- keeping the index fresh

def sync(store, embed, chunks: list[dict]) -> dict:
    """Bring the store in line with the knowledge base without rebuilding it (Unit 3 lesson 3).
    Only new or changed chunks are embedded; chunks that disappeared are deleted."""
    have = store.hashes()
    incoming = {c["id"]: c for c in chunks}
    changed = [c for c in chunks if have.get(c["id"]) != c["hash"]]
    gone = sorted(set(have) - set(incoming))
    if changed:
        store.upsert(changed, embed([c["title"] + ". " + c["section"] + ". " + c["text"] for c in changed]))
    if gone:
        store.delete(gone)
    added = sum(1 for c in changed if c["id"] not in have)
    return {"added": added, "updated": len(changed) - added, "deleted": len(gone),
            "unchanged": len(chunks) - len(changed)}


# ---------------------------------------------------------------- stores

class MemoryStore:
    """BM25 + cosine in memory. Fine for a few thousand chunks and for tests."""

    def __init__(self):
        self.chunks, self.vectors, self.tf = {}, {}, {}

    def hashes(self):
        return {cid: c["hash"] for cid, c in self.chunks.items()}

    def upsert(self, chunks, vectors):
        for c, v in zip(chunks, vectors):
            self.chunks[c["id"]] = c
            n = math.sqrt(sum(x * x for x in v)) or 1.0
            self.vectors[c["id"]] = [x / n for x in v]
            self.tf[c["id"]] = Counter(terms(c["title"] + " " + c["section"] + " " + c["text"]))

    def delete(self, ids):
        for cid in ids:
            for d in (self.chunks, self.vectors, self.tf):
                d.pop(cid, None)

    def count(self):
        return len(self.chunks)

    def get(self, ids):
        return {cid: dict(self.chunks[cid]) for cid in ids if cid in self.chunks}

    def keyword(self, query, groups, n):
        q = set(terms(query))
        if not q:
            return []
        N = len(self.tf) or 1
        avg = sum(sum(tf.values()) for tf in self.tf.values()) / N
        df = Counter(t for tf in self.tf.values() for t in set(tf))
        scored = []
        for cid, tf in self.tf.items():
            if not allowed(self.chunks[cid]["acl"], groups):
                continue
            dl = sum(tf.values())
            s = sum(math.log(1 + (N - df[t] + 0.5) / (df[t] + 0.5)) * tf[t] * 2.2 / (tf[t] + 1.2 * (0.25 + 0.75 * dl / avg))
                    for t in q if tf[t])
            if s > 0:
                scored.append((s, cid))
        return [cid for _, cid in sorted(scored, key=lambda x: (-x[0], x[1]))[:n]]

    def vector(self, qvec, groups, n):
        n_ = math.sqrt(sum(x * x for x in qvec)) or 1.0
        q = [x / n_ for x in qvec]
        scored = [(sum(a * b for a, b in zip(q, v)), cid) for cid, v in self.vectors.items()
                  if allowed(self.chunks[cid]["acl"], groups)]
        return [cid for s, cid in sorted(scored, key=lambda x: (-x[0], x[1])) if s > 0][:n]


class PgStore:
    """Postgres full-text search + pgvector (0.8 or later). One table per embedding model,
    because vectors from different models can't be compared: switching models means re-embedding."""

    def __init__(self, url: str, dim: int, model_name: str):
        import psycopg  # only needed when you use Postgres
        self.conn = psycopg.connect(url, autocommit=True)
        self.table = "kb_chunks_" + re.sub(r"[^a-z0-9]+", "_", model_name.lower()).strip("_")
        t = self.table
        with self.conn.transaction():
            self.conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
            self.conn.execute(f"""CREATE TABLE IF NOT EXISTS {t} (
                id text PRIMARY KEY, doc_id text NOT NULL, title text NOT NULL, section text NOT NULL,
                body text NOT NULL, url text NOT NULL, acl text[] NOT NULL, hash text NOT NULL,
                embedding vector({int(dim)}) NOT NULL,
                tsv tsvector GENERATED ALWAYS AS (to_tsvector('english', title || ' ' || section || ' ' || body)) STORED)""")
            self.conn.execute(f"CREATE INDEX IF NOT EXISTS {t}_tsv ON {t} USING gin (tsv)")
            self.conn.execute(f"CREATE INDEX IF NOT EXISTS {t}_acl ON {t} USING gin (acl)")
            self.conn.execute(f"CREATE INDEX IF NOT EXISTS {t}_vec ON {t} USING hnsw (embedding vector_cosine_ops)")

    @staticmethod
    def _vec(v):
        return "[" + ",".join("%.6f" % x for x in v) + "]"

    def hashes(self):
        return dict(self.conn.execute(f"SELECT id, hash FROM {self.table}").fetchall())

    def upsert(self, chunks, vectors):
        with self.conn.transaction():
            for c, v in zip(chunks, vectors):
                self.conn.execute(
                    f"""INSERT INTO {self.table} (id, doc_id, title, section, body, url, acl, hash, embedding)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::vector)
                        ON CONFLICT (id) DO UPDATE SET doc_id = EXCLUDED.doc_id, title = EXCLUDED.title,
                          section = EXCLUDED.section, body = EXCLUDED.body, url = EXCLUDED.url,
                          acl = EXCLUDED.acl, hash = EXCLUDED.hash, embedding = EXCLUDED.embedding""",
                    (c["id"], c["doc_id"], c["title"], c["section"], c["text"], c["url"], list(c["acl"]), c["hash"], self._vec(v)))

    def delete(self, ids):
        self.conn.execute(f"DELETE FROM {self.table} WHERE id = ANY(%s)", (list(ids),))

    def count(self):
        return self.conn.execute(f"SELECT count(*) FROM {self.table}").fetchone()[0]

    def get(self, ids):
        rows = self.conn.execute(f"SELECT id, doc_id, title, section, body, url, acl, hash FROM {self.table} WHERE id = ANY(%s)",
                                 (list(ids),)).fetchall()
        return {r[0]: {"id": r[0], "doc_id": r[1], "title": r[2], "section": r[3], "text": r[4], "url": r[5],
                       "acl": list(r[6]), "hash": r[7]} for r in rows}

    def keyword(self, query, groups, n):
        words = [w for w in re.findall(r"[a-z0-9]+", query.lower()) if w not in STOP]
        if not words:
            return []
        tsq = " | ".join(words)
        rows = self.conn.execute(
            f"""SELECT id FROM {self.table}
                WHERE acl && %s::text[] AND tsv @@ to_tsquery('english', %s)
                ORDER BY ts_rank_cd(tsv, to_tsquery('english', %s)) DESC, id LIMIT %s""",
            (list(groups), tsq, tsq, n)).fetchall()
        return [r[0] for r in rows]

    def vector(self, qvec, groups, n):
        with self.conn.transaction():
            # Without iterative scans, HNSW finds the nearest rows first and THEN applies the acl
            # filter, so a user with narrow access can get fewer than n results, or none.
            self.conn.execute("SET LOCAL hnsw.iterative_scan = relaxed_order")
            rows = self.conn.execute(
                f"""SELECT id FROM {self.table} WHERE acl && %s::text[]
                    ORDER BY embedding <=> %s::vector, id LIMIT %s""",
                (list(groups), self._vec(qvec), n)).fetchall()
        return [r[0] for r in rows]


def build(settings, embed):
    """The store for these settings, synced with <data_dir>/kb (data/kb unless ATLAS_DATA_DIR is set)."""
    from . import kb
    if settings.db_url.startswith("postgres"):
        # One table per dataset as well as per model, so two datasets never overwrite each other.
        name = embed.name if settings.tenant == "meridian" else embed.name + "_" + settings.tenant
        store = PgStore(settings.db_url, embed.dim, name)
    else:
        store = MemoryStore()
    sync(store, embed, kb.load(settings.data_dir / "kb"))
    return store
