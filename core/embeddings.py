"""Semantic embedding layer for the learning structure.

One model: `sentence-transformers/all-MiniLM-L6-v2`. 80 MB. Runs on CPU
fast enough for both ingestion (one-time per fact) and query (one-time
per recall call). 384-dim float32 vectors.

Storage convention: vectors are packed as little-endian float32 bytes
(4 bytes × 384 = 1536 bytes per fact) into the `long_term_facts.embedding`
BLOB column. Conversion helpers `pack/unpack` round-trip cleanly.

Why MiniLM and not something bigger:
  - 80 MB vs 420 MB+ for mpnet — fits in memory budget alongside MLX
  - sub-50ms encode on M1 Ultra (we have one user, not a workload)
  - quality is "good enough" for recall — when the structure gets denser
    we can swap in a larger model + re-embed in one migration pass
  - this is the foundation layer; quality tradeoffs live above it

The model is lazy-loaded on first call. Subsequent calls reuse the
in-memory instance.
"""
from __future__ import annotations

import logging
import struct
from typing import Iterable

import numpy as np

log = logging.getLogger("charles.embeddings")

_MODEL_NAME = "all-MiniLM-L6-v2"
_DIM = 384

_model = None  # lazy singleton


def _get_model():
    """Lazy-load the embedding model on first use. Subsequent calls are O(1)."""
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer  # heavy import
        log.info("loading embedding model: %s", _MODEL_NAME)
        _model = SentenceTransformer(_MODEL_NAME)
        log.info("embedding model loaded")
    return _model


def encode(text: str) -> bytes:
    """Encode a single string into packed float32 bytes (1536 bytes / 384 dim)."""
    if not text or not text.strip():
        # Empty / whitespace-only — return a zero vector so the row is still
        # storable, but cosine similarity will rank it neutral.
        return struct.pack(f"<{_DIM}f", *([0.0] * _DIM))
    vec = _get_model().encode(text, convert_to_numpy=True, normalize_embeddings=True)
    return pack(vec)


def encode_batch(texts: list[str]) -> list[bytes]:
    """Batch-encode N strings. Much faster than N x encode() for migration runs."""
    if not texts:
        return []
    vecs = _get_model().encode(texts, convert_to_numpy=True, normalize_embeddings=True, batch_size=32)
    return [pack(v) for v in vecs]


def pack(vec: np.ndarray) -> bytes:
    """Pack a float32 ndarray of shape (DIM,) into 1536 little-endian bytes."""
    v = np.asarray(vec, dtype=np.float32).reshape(-1)
    if v.shape[0] != _DIM:
        raise ValueError(f"expected {_DIM}-dim vector, got {v.shape[0]}")
    return v.tobytes()


def unpack(blob: bytes) -> np.ndarray:
    """Inverse of pack(). Returns a float32 ndarray of shape (DIM,)."""
    if not blob or len(blob) != _DIM * 4:
        return np.zeros(_DIM, dtype=np.float32)
    return np.frombuffer(blob, dtype=np.float32)


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine similarity between two unit-normalized vectors.
    Since encode() normalizes, this reduces to a dot product."""
    return float(np.dot(a, b))


def topk_by_cosine(
    query_vec: np.ndarray,
    candidates: list[tuple[int, bytes]],
    k: int = 5,
) -> list[tuple[int, float]]:
    """Return top-k (candidate_id, similarity) tuples, highest similarity first.

    candidates: list of (id, embedding_blob) — usually rows from long_term_facts.
    """
    if not candidates:
        return []
    # Stack all candidate vectors into one matrix for vectorized dot product
    ids = [cid for cid, _ in candidates]
    matrix = np.vstack([unpack(blob) for _, blob in candidates])
    # query is shape (DIM,), matrix is (N, DIM); result is (N,)
    sims = matrix @ query_vec
    # argsort descending, take top-k
    top_idx = np.argsort(-sims)[:k]
    return [(ids[i], float(sims[i])) for i in top_idx]


def dim() -> int:
    """Expose embedding dimension so callers can sanity-check schema."""
    return _DIM
