"""One Postgres connection pool per process, shared by KBStore and PgVectorIndex.

Why a pool: the server answers requests on many threads at once. A single shared connection
makes them queue behind each other; opening a connection per request is slow (~10-50 ms).
A pool keeps a few connections open and lends one to each piece of work, then takes it back.
"""

import psycopg
from pgvector.psycopg import register_vector
from psycopg_pool import ConnectionPool

from copilot.config import Settings


def open_pool(st: Settings) -> ConnectionPool:
    """Open the pool (sizes from .env). Close it with `pool.close()` or a `with` block."""
    # The vector type must exist before connections can register it, so create it first.
    with psycopg.connect(st.database_url, autocommit=True) as conn:
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
    return ConnectionPool(
        st.database_url,
        min_size=1,
        max_size=st.db_pool_size,
        kwargs={"autocommit": True},  # each statement commits; multi-step writes use transaction()
        configure=register_vector,  # every new connection learns the pgvector type
        open=True,
    )
