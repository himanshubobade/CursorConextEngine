"""Embedding generation and the vector index over the document table.

Vectors are L2-normalized on the way in, so cosine similarity is a plain dot
product. The index prefers sqlite-vec when the extension loads and falls
back to a cached numpy matrix otherwise - at metastore scale (tens of
thousands of docs at 768 dimensions) the brute-force scan is a few
milliseconds, so the fallback is a real option rather than a degraded mode.
"""
from __future__ import annotations

import logging
import threading
from typing import Callable, Sequence

import numpy as np
import requests

from app.config import ModelSpec, get_settings
from app.context.documents import build_documents
from app.context.store import Store, get_store

log = logging.getLogger(__name__)

OPENAI_EMBEDDINGS_URL = "https://api.openai.com/v1/embeddings"


class EmbeddingError(RuntimeError):
    pass


class EmbeddingClient:
    def __init__(self, spec: ModelSpec, api_key: str | None, dimensions: int) -> None:
        self.spec = spec
        self.api_key = api_key
        self.dimensions = dimensions
        self.tokens_used = 0

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        """Returns an (n, dim) float32 array of unit vectors."""
        if not texts:
            return np.zeros((0, self.dimensions), dtype=np.float32)
        if not self.api_key:
            raise EmbeddingError(
                "OPENAI_API_KEY is not configured; it is required for embeddings "
                "(see secrets.example.yaml)."
            )
        payload = {
            "model": self.spec.api_model,
            "input": list(texts),
            "dimensions": self.dimensions,
        }
        resp = requests.post(
            OPENAI_EMBEDDINGS_URL,
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            json=payload,
            timeout=120,
        )
        if not resp.ok:
            try:
                detail = resp.json().get("error", {}).get("message", resp.text)
            except ValueError:
                detail = resp.text
            raise EmbeddingError(f"Embedding request failed ({resp.status_code}): {detail}")
        body = resp.json()
        self.tokens_used += body.get("usage", {}).get("total_tokens", 0)
        ordered = sorted(body["data"], key=lambda d: d["index"])
        matrix = np.array([d["embedding"] for d in ordered], dtype=np.float32)
        return normalize(matrix)

    def embed_one(self, text: str) -> np.ndarray:
        return self.embed([text])[0]

    @property
    def cost_usd(self) -> float:
        return self.tokens_used * self.spec.price_input / 1_000_000


def normalize(matrix: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(matrix, axis=-1, keepdims=True)
    norms[norms == 0] = 1.0
    return (matrix / norms).astype(np.float32)


def get_embedding_client() -> EmbeddingClient:
    settings = get_settings()
    spec = settings.embedding_model
    dim = int(settings.get("retrieval", "embedding_dimensions", default=spec.dimensions or 768))
    return EmbeddingClient(spec, settings.api_key("openai"), dim)


# ----------------------------------------------------------------------
# index maintenance
# ----------------------------------------------------------------------


def refresh_index(
    store: Store | None = None,
    catalog: str | None = None,
    progress: Callable[[str, dict], None] | None = None,
) -> dict:
    """Rebuild documents from the snapshot and embed only what changed.

    Doc bodies are content-hashed, so a re-sync that touched nothing costs
    nothing: `stale_docs` returns an empty list and no API call is made.
    """
    store = store or get_store()
    client = get_embedding_client()
    emit = progress or (lambda *_: None)

    docs = build_documents(store, catalog)
    store.upsert_docs(docs)
    emit("documents", {"built": len(docs)})

    stale = store.stale_docs(client.spec.id, client.dimensions)
    emit("embedding", {"stale": len(stale), "done": 0})
    if not stale:
        return {"documents": len(docs), "embedded": 0, "cost_usd": 0.0}

    batch_size = int(get_settings().get("retrieval", "embedding_batch_size", default=128))
    embedded = 0
    for start in range(0, len(stale), batch_size):
        batch = stale[start : start + batch_size]
        vectors = client.embed([d["body"] for d in batch])
        store.upsert_embeddings(
            [
                (d["doc_id"], client.spec.id, client.dimensions, d["body_hash"], vec.tobytes())
                for d, vec in zip(batch, vectors)
            ]
        )
        embedded += len(batch)
        emit("embedding", {"stale": len(stale), "done": embedded})

    VectorIndex.invalidate()
    return {
        "documents": len(docs),
        "embedded": embedded,
        "tokens": client.tokens_used,
        "cost_usd": round(client.cost_usd, 6),
    }


# ----------------------------------------------------------------------
# search
# ----------------------------------------------------------------------


class VectorIndex:
    """Cached similarity search over the embeddings table."""

    _instance: "VectorIndex | None" = None
    _lock = threading.Lock()

    def __init__(self, store: Store, model: str, dim: int) -> None:
        self.store = store
        self.model = model
        self.dim = dim
        self.doc_ids: list[str] = []
        self.matrix: np.ndarray = np.zeros((0, dim), dtype=np.float32)
        self.version: str | None = None
        self._vec_backend = _SqliteVecBackend(store, dim)

    @classmethod
    def get(cls) -> "VectorIndex":
        settings = get_settings()
        spec = settings.embedding_model
        dim = int(settings.get("retrieval", "embedding_dimensions", default=spec.dimensions or 768))
        with cls._lock:
            if cls._instance is None or cls._instance.model != spec.id or cls._instance.dim != dim:
                cls._instance = VectorIndex(get_store(), spec.id, dim)
            return cls._instance

    @classmethod
    def invalidate(cls) -> None:
        with cls._lock:
            if cls._instance is not None:
                cls._instance.version = None

    def _load(self) -> None:
        current = self.store.index_version()
        if self.version == current:
            return
        with self._lock:
            doc_ids, blobs = self.store.all_embeddings(self.model, self.dim)
            if doc_ids:
                self.matrix = np.frombuffer(b"".join(blobs), dtype=np.float32).reshape(len(doc_ids), self.dim)
            else:
                self.matrix = np.zeros((0, self.dim), dtype=np.float32)
            self.doc_ids = doc_ids
            self.version = current
            self._vec_backend.rebuild(doc_ids, self.matrix)

    def search(self, query_vector: np.ndarray, top_k: int, doc_type: str | None = None) -> list[tuple[str, float]]:
        self._load()
        if not self.doc_ids:
            return []

        prefix = f"{doc_type}::" if doc_type else None
        backend_hits = self._vec_backend.search(query_vector, top_k, prefix)
        if backend_hits is not None:
            return backend_hits

        scores = self.matrix @ query_vector.astype(np.float32)
        if prefix:
            mask = np.array([d.startswith(prefix) for d in self.doc_ids])
            scores = np.where(mask, scores, -np.inf)
        k = min(top_k, len(scores))
        top = np.argpartition(-scores, k - 1)[:k] if k < len(scores) else np.arange(len(scores))
        top = top[np.argsort(-scores[top])]
        return [(self.doc_ids[i], float(scores[i])) for i in top if np.isfinite(scores[i])]

    @property
    def size(self) -> int:
        self._load()
        return len(self.doc_ids)


class _SqliteVecBackend:
    """Optional sqlite-vec acceleration.

    Kept entirely behind `search() -> None` meaning "not available": every
    failure mode (package missing, extension loading disabled in this Python
    build, older vec0 syntax) collapses to the numpy path above.
    """

    def __init__(self, store: Store, dim: int) -> None:
        self.store = store
        self.dim = dim
        # Store loads the extension per connection; if that failed there is
        # nothing to accelerate with.
        store.conn  # noqa: B018 - force a connection so vec_enabled is set
        self.available = store.vec_enabled
        self.ready = False
        self._doc_ids: list[str] = []
        if not self.available:
            log.info("sqlite-vec unavailable; using numpy vector scan")

    def rebuild(self, doc_ids: list[str], matrix: np.ndarray) -> None:
        if not self.available:
            return
        try:
            conn = self.store.conn
            conn.execute("DROP TABLE IF EXISTS vec_docs")
            conn.execute(f"CREATE VIRTUAL TABLE vec_docs USING vec0(embedding float[{self.dim}])")
            conn.executemany(
                "INSERT INTO vec_docs (rowid, embedding) VALUES (?, ?)",
                [(i, matrix[i].tobytes()) for i in range(len(doc_ids))],
            )
            conn.commit()
            self._doc_ids = doc_ids
            self.ready = True
        except Exception as exc:  # noqa: BLE001
            log.info("sqlite-vec index build failed (%s); using numpy vector scan", exc)
            self.available = False
            self.ready = False

    def search(self, query_vector: np.ndarray, top_k: int, prefix: str | None) -> list[tuple[str, float]] | None:
        if not (self.available and self.ready):
            return None
        try:
            # Over-fetch when filtering by doc type, since the filter is
            # applied after the KNN rather than inside it.
            k = top_k * 6 if prefix else top_k
            rows = self.store.conn.execute(
                "SELECT rowid, distance FROM vec_docs WHERE embedding MATCH ? AND k = ? ORDER BY distance",
                (query_vector.astype(np.float32).tobytes(), k),
            ).fetchall()
            hits: list[tuple[str, float]] = []
            for row in rows:
                doc_id = self._doc_ids[row["rowid"]]
                if prefix and not doc_id.startswith(prefix):
                    continue
                # vec0 returns L2 distance over unit vectors: d^2 = 2 - 2cos
                hits.append((doc_id, 1.0 - (float(row["distance"]) ** 2) / 2.0))
                if len(hits) >= top_k:
                    break
            return hits
        except Exception as exc:  # noqa: BLE001
            log.info("sqlite-vec query failed (%s); using numpy vector scan", exc)
            self.available = False
            return None
