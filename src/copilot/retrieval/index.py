"""Vector index (D7): one interface, two implementations.

- InMemoryIndex: numpy, for unit tests and the no-Docker quick start.
- PgVectorIndex: Postgres + pgvector, for the app, eval runs and Phase 2.
The rest of the system only knows `VectorIndex`.

Incremental (D8): each row keeps the chunk's content_hash, so ingestion embeds only new or
changed chunks and deletes removed ones. The server only calls `hashes()` and `search()`.
"""

import re
from typing import Protocol

import numpy as np
from psycopg_pool import ConnectionPool


class VectorIndex(Protocol):
    def hashes(self) -> dict[str, str]:
        """chunk_id → content_hash of every stored vector."""
        ...

    def upsert(
        self, ids: list[str], vectors: np.ndarray, flags: list[list[str]], hashes: list[str]
    ) -> None:
        """Insert new chunks, replace changed ones. Other rows are left alone."""
        ...

    def delete(self, ids: list[str]) -> None: ...

    def search(
        self, query: np.ndarray, k: int, exclude_flags: tuple[str, ...] = ()
    ) -> list[tuple[str, float]]:
        """Return up to k (chunk_id, cosine similarity) pairs, best first."""
        ...


class InMemoryIndex:
    def __init__(self) -> None:
        self._rows: dict[str, tuple[np.ndarray, list[str], str]] = {}  # id → (vector, flags, hash)

    def hashes(self) -> dict[str, str]:
        return {cid: h for cid, (_, _, h) in self._rows.items()}

    def upsert(self, ids, vectors, flags, hashes) -> None:
        for cid, v, f, h in zip(ids, vectors, flags, hashes, strict=True):
            self._rows[cid] = (np.asarray(v, dtype=np.float32), list(f), h)

    def delete(self, ids) -> None:
        for cid in ids:
            self._rows.pop(cid, None)

    def search(self, query, k, exclude_flags=()):
        rows = [
            (cid, v) for cid, (v, f, _) in self._rows.items() if not set(f) & set(exclude_flags)
        ]
        if not rows:
            return []
        scores = np.stack([v for _, v in rows]) @ query  # normalised: dot product = cosine
        order = np.argsort(-scores)[:k]
        return [(rows[i][0], float(scores[i])) for i in order]


class PgVectorIndex:
    """One table per embedding model, because each model has its own vector length.

    Takes the shared pool (copilot/db.py): each call borrows a connection and gives it back.
    """

    def __init__(self, pool: ConnectionPool, model_name: str, dim: int):
        self.pool = pool
        self.table = "chunk_vectors_" + re.sub(r"[^a-z0-9]", "_", model_name.lower())
        self.dim = dim
        with pool.connection() as conn:
            conn.execute(
                f"""CREATE TABLE IF NOT EXISTS {self.table} (
                        chunk_id     text PRIMARY KEY,
                        content_hash text NOT NULL,
                        flags        text[] NOT NULL DEFAULT '{{}}',
                        embedding    vector({dim}) NOT NULL
                    )"""
            )
            # HNSW index: fast approximate search with cosine distance (useful as the KB grows).
            conn.execute(
                f"CREATE INDEX IF NOT EXISTS {self.table}_hnsw ON {self.table} "
                "USING hnsw (embedding vector_cosine_ops)"
            )

    def hashes(self) -> dict[str, str]:
        with self.pool.connection() as conn:
            return dict(conn.execute(f"SELECT chunk_id, content_hash FROM {self.table}"))

    def upsert(self, ids, vectors, flags, hashes) -> None:
        with self.pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            cur.executemany(
                f"""INSERT INTO {self.table} (chunk_id, content_hash, flags, embedding)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (chunk_id) DO UPDATE SET
                        content_hash = EXCLUDED.content_hash,
                        flags = EXCLUDED.flags,
                        embedding = EXCLUDED.embedding""",
                [
                    (i, h, f, np.asarray(v, dtype=np.float32))
                    for i, v, f, h in zip(ids, vectors, flags, hashes, strict=True)
                ],
            )

    def delete(self, ids) -> None:
        with self.pool.connection() as conn:
            conn.execute(f"DELETE FROM {self.table} WHERE chunk_id = ANY(%s)", (list(ids),))

    def search(self, query, k, exclude_flags=()):
        # <=> is pgvector's cosine DISTANCE (0 = identical); similarity = 1 - distance.
        q = np.asarray(query, dtype=np.float32)
        with self.pool.connection() as conn:
            rows = conn.execute(
                f"""SELECT chunk_id, 1 - (embedding <=> %s) AS score
                    FROM {self.table}
                    WHERE NOT (flags && %s::text[])
                    ORDER BY embedding <=> %s
                    LIMIT %s""",
                (q, list(exclude_flags), q, k),
            ).fetchall()
        return [(cid, float(score)) for cid, score in rows]
