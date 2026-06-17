"""
Neon (serverless Postgres) connection helper.

Reuses the pg8000 connection across warm Lambda invocations. Reconnects
automatically if Neon auto-suspends the compute (idle timeout on free tier
is 5 minutes).
"""
import logging
import ssl
from urllib.parse import urlparse

import pg8000.native

logger = logging.getLogger(__name__)

_conn: pg8000.native.Connection | None = None


def _parse_dsn(dsn: str) -> dict:
    p = urlparse(dsn)
    return {
        "host": p.hostname,
        "port": p.port or 5432,
        "user": p.username,
        "password": p.password,
        "database": p.path.lstrip("/"),
    }


def get_connection(dsn: str) -> pg8000.native.Connection:
    """Return a live pg8000 connection, reconnecting if Neon suspended it."""
    global _conn
    if _conn is not None:
        try:
            _conn.run("SELECT 1")
            return _conn
        except Exception:
            logger.info("Neon connection dropped — reconnecting")
            _conn = None

    params = _parse_dsn(dsn)
    ssl_ctx = ssl.create_default_context()
    _conn = pg8000.native.Connection(
        host=params["host"],
        port=params["port"],
        user=params["user"],
        password=params["password"],
        database=params["database"],
        ssl_context=ssl_ctx,
    )
    logger.info("Connected to Neon: %s", params["host"])
    return _conn


def ensure_schema(conn: pg8000.native.Connection) -> None:
    """Create pgvector extension and document_chunks table if they don't exist."""
    conn.run("CREATE EXTENSION IF NOT EXISTS vector")
    conn.run(
        """
        CREATE TABLE IF NOT EXISTS document_chunks (
            id          SERIAL PRIMARY KEY,
            doc_name    TEXT NOT NULL,
            chunk_index INT  NOT NULL,
            content     TEXT NOT NULL,
            embedding   vector(1024),
            page_start  INT  NOT NULL DEFAULT 1,
            created_at  TIMESTAMPTZ DEFAULT NOW()
        )
        """
    )
    # Add page_start to tables created before this column existed
    conn.run(
        "ALTER TABLE document_chunks ADD COLUMN IF NOT EXISTS page_start INT NOT NULL DEFAULT 1"
    )
    conn.run(
        """
        CREATE INDEX IF NOT EXISTS document_chunks_embedding_idx
        ON document_chunks USING hnsw (embedding vector_cosine_ops)
        """
    )
    logger.info("Neon schema verified")


def format_vector(embedding: list[float]) -> str:
    """Format a Python float list as a pgvector literal. Safe to interpolate — floats only."""
    return "[" + ",".join(f"{x:.8f}" for x in embedding) + "]"
