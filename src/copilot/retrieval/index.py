"""Vector index (D7): one interface, two implementations.

- InMemoryIndex: numpy, for unit tests and the no-Docker quick start.
- PgVectorIndex: Postgres + pgvector, for the app, eval runs and Phase 2.
The rest of the system only knows `VectorIndex`.
"""

import re
from typing import Protocol

import numpy as np


class VectorIndex(Protocol):
    def upsert(self, ids: list[str], vectors: np.ndarray, flags: list[list[str]], kb_hash: str) -> None: ...

    def search(self, query: np.ndarray, k: int, exclude_flags: tuple[str, ...] = ()) -> list[tuple[str, float]]:
        """Return up to k (chunk_id, cosine similarity) pairs, best first."""
        ...


class InMemoryIndex:
    def __init__(self) -> None:
        self._ids: list[str] = []
        self._flags: list[list[str]] = []
        self._vecs = np.zeros((0, 0), dtype=np.float32)

    def upsert(self, ids, vectors, flags, kb_hash) -> None:
        self._ids, self._flags, self._vecs = list(ids), list(flags), np.asarray(vectors, dtype=np.float32)

    def search(self, query, k, exclude_flags=()):
        scores = self._vecs @ query  # vectors are normalised, so dot product = cosine similarity
        order = np.argsort(-scores)
        out = []
        for i in order:
            if exclude_flags and set(self._flags[i]) & set(exclude_flags):
                continue
            out.append((self._ids[i], float(scores[i])))
            if len(out) == k:
                break
        return out


class PgVectorIndex:
    """One table per embedding model, because each model has its own vector length."""

    def __init__(self, database_url: str, model_name: str, dim: int):
        import psycopg
        from pgvector.psycopg import register_vector

        self.table = "chunk_vectors_" + re.sub(r"[^a-z0-9]", "_", model_name.lower())
        self.dim = dim
        self.conn = psycopg.connect(database_url, autocommit=True)
        self.conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        register_vector(self.conn)
        self.conn.execute(
            f"""CREATE TABLE IF NOT EXISTS {self.table} (
                    chunk_id  text PRIMARY KEY,
                    kb_hash   text NOT NULL,
                    flags     text[] NOT NULL DEFAULT '{{}}',
                    embedding vector({dim}) NOT NULL
                )"""
        )
        # HNSW index for fast approximate search with cosine distance (useful as the KB grows).
        self.conn.execute(
            f"CREATE INDEX IF NOT EXISTS {self.table}_hnsw ON {self.table} "
            "USING hnsw (embedding vector_cosine_ops)"
        )

    def upsert(self, ids, vectors, flags, kb_hash) -> None:
        with self.conn.transaction():  # all-or-nothing: the table always holds one consistent KB version
            self.conn.execute(f"DELETE FROM {self.table}")
            with self.conn.cursor() as cur:
                cur.executemany(
                    f"INSERT INTO {self.table} (chunk_id, kb_hash, flags, embedding) VALUES (%s, %s, %s, %s)",
                    [(i, kb_hash, f, np.asarray(v, dtype=np.float32)) for i, f, v in zip(ids, flags, vectors)],
                )

    def kb_hash(self) -> str | None:
        row = self.conn.execute(f"SELECT kb_hash FROM {self.table} LIMIT 1").fetchone()
        return row[0] if row else None

    def search(self, query, k, exclude_flags=()):
        # <=> is pgvector's cosine DISTANCE (0 = identical); similarity = 1 - distance.
        rows = self.conn.execute(
            f"""SELECT chunk_id, 1 - (embedding <=> %s) AS score
                FROM {self.table}
                WHERE NOT (flags && %s::text[])
                ORDER BY embedding <=> %s
                LIMIT %s""",
            (np.asarray(query, dtype=np.float32), list(exclude_flags), np.asarray(query, dtype=np.float32), k),
        ).fetchall()
        return [(cid, float(score)) for cid, score in rows]
