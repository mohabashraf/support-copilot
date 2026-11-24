"""Phase 2 follow-up (inside the app container on the test VM).
(1) Quality: the 26 real chunks + 1,000 same-topic SOPs already indexed by scale_kb.py, searched
    (a) unscoped and (b) scoped to customer-facing help pages (SOPs excluded).
(2) Speed: HNSW search over N random 1,024-d vectors (N = 10k, 100k). Content doesn't matter for
    speed, so no embedding is needed.
"""

import json
import time
from statistics import median

import numpy as np

from copilot.config import get_settings
from copilot.db import open_pool
from copilot.eval import retrieval as reval
from copilot.kb.models import Chunk
from copilot.retrieval.embeddings import get_embedder
from copilot.retrieval.index import PgVectorIndex
from copilot.retrieval.search import Retriever


class ScopedIndex(PgVectorIndex):
    """Customer replies: help pages only. Internal SOPs (chunk ids sop#...) are agent guidance."""

    def search(self, query, k, exclude_flags=()):
        q = np.asarray(query, dtype=np.float32)
        with self.pool.connection() as conn:
            rows = conn.execute(
                f"""SELECT c.data, 1 - (v.embedding <=> %s) AS score
                    FROM {self.table} v JOIN {self.chunks_table} c
                      ON c.chunk_id = v.chunk_id AND c.content_hash = v.content_hash
                    WHERE NOT (v.flags && %s::text[]) AND v.chunk_id NOT LIKE 'sop#%%'
                    ORDER BY v.embedding <=> %s LIMIT %s""",
                (q, list(exclude_flags), q, k),
            ).fetchall()
        return [(Chunk.model_validate(data), float(score)) for data, score in rows]


def main() -> None:
    pool = open_pool(get_settings())
    emb = get_embedder("bge-m3")
    out = {"quality_at_1026_chunks": {}, "speed": []}
    for name, cls in (("unscoped", PgVectorIndex), ("scoped_help_pages_only", ScopedIndex)):
        idx = cls(pool, "bge-m3-scale", emb.dim, chunks_table="scale_kb_chunks")
        r = reval.run(Retriever(emb, idx), "bge-m3", name, "scale")
        out["quality_at_1026_chunks"][name] = {
            "queries": {k: r["queries"][k] for k in ("r1", "r3", "hit3", "mrr", "n")},
            "tickets": {k: r["tickets"][k] for k in ("r3", "hit3", "mrr", "n")},
        }
        print(name, json.dumps(out["quality_at_1026_chunks"][name]), flush=True)

    rng = np.random.default_rng(0)
    with pool.connection() as conn:
        conn.execute("SET maintenance_work_mem = '2GB'")
        for n in (10_000, 100_000):
            t = f"speed_vectors_{n}"
            conn.execute(f"DROP TABLE IF EXISTS {t}")
            conn.execute(f"CREATE TABLE {t} (id int, embedding vector(1024))")
            t0 = time.perf_counter()
            with conn.cursor().copy(f"COPY {t} (id, embedding) FROM STDIN") as cp:
                for i in range(n):
                    v = rng.standard_normal(1024).astype(np.float32)
                    v /= np.linalg.norm(v)
                    cp.write_row((i, "[" + ",".join(f"{x:.5f}" for x in v) + "]"))
            load_s = time.perf_counter() - t0
            t0 = time.perf_counter()
            conn.execute(f"CREATE INDEX ON {t} USING hnsw (embedding vector_cosine_ops)")
            build_s = time.perf_counter() - t0
            lat = []
            for _ in range(200):
                q = rng.standard_normal(1024).astype(np.float32)
                q /= np.linalg.norm(q)
                t1 = time.perf_counter()
                conn.execute(
                    f"SELECT id FROM {t} ORDER BY embedding <=> %s LIMIT 5", (q,)
                ).fetchall()
                lat.append((time.perf_counter() - t1) * 1000)
            lat.sort()
            row = {
                "vectors": n,
                "load_s": round(load_s, 1),
                "hnsw_build_s": round(build_s, 1),
                "search_ms_p50": round(median(lat), 2),
                "search_ms_p95": round(lat[190], 2),
            }
            out["speed"].append(row)
            print(json.dumps(row), flush=True)
            conn.execute(f"DROP TABLE {t}")
    with open("/app/logs/scale_kb2.json", "w") as f:
        json.dump(out, f, indent=1)


if __name__ == "__main__":
    main()
