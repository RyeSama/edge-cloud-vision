"""Vector index abstraction and two implementations.

The retriever never imports ChromaDB directly. If the optional backend is
missing or fails, the caller degrades to SQLite text search and records a
warning; the primary event path keeps running.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Protocol

import numpy as np

from ecvision.memory.embedder import Embedder
from ecvision.memory.models import VectorRecord

log = logging.getLogger(__name__)


class VectorBackendUnavailable(RuntimeError):
    """The configured vector backend cannot be opened."""


class VectorQueryError(RuntimeError):
    """A vector query failed after the backend was opened."""


class VectorIndex(Protocol):
    backend: str

    def upsert(self, records: list[VectorRecord]) -> int: ...

    def query(
        self,
        vector: list[float],
        top_k: int = 10,
        allowed_ids: set[str] | None = None,
    ) -> list[tuple[str, float]]: ...

    def delete(self, ids: list[str]) -> int: ...

    def count(self) -> int: ...

    def healthy(self) -> bool: ...


class InMemoryVectorIndex:
    """Deterministic cosine index used by default and by the test suite."""

    backend = "memory"

    def __init__(self, dimension: int = 256) -> None:
        if dimension < 16:
            raise ValueError("dimension must be >= 16")
        self.dimension = dimension
        self._vectors: dict[str, np.ndarray] = {}

    def upsert(self, records: list[VectorRecord]) -> int:
        for record in records:
            if len(record.vector) != self.dimension:
                raise ValueError(
                    f"vector dimension mismatch: expected {self.dimension}, got {len(record.vector)}"
                )
            vector = np.asarray(record.vector, dtype=np.float32)
            norm = float(np.linalg.norm(vector))
            if norm == 0.0:
                self._vectors.pop(record.id, None)
                continue
            self._vectors[record.id] = vector / norm
        return len(records)

    def query(
        self,
        vector: list[float],
        top_k: int = 10,
        allowed_ids: set[str] | None = None,
    ) -> list[tuple[str, float]]:
        if len(vector) != self.dimension:
            raise VectorQueryError(
                f"query dimension mismatch: expected {self.dimension}, got {len(vector)}"
            )
        query = np.asarray(vector, dtype=np.float32)
        norm = float(np.linalg.norm(query))
        if norm == 0.0:
            return []
        query /= norm
        candidates = self._vectors.items()
        if allowed_ids is not None:
            candidates = ((key, value) for key, value in candidates if key in allowed_ids)
        scored = [(key, float(np.dot(query, value))) for key, value in candidates]
        scored.sort(key=lambda pair: pair[1], reverse=True)
        return scored[: max(0, top_k)]

    def delete(self, ids: list[str]) -> int:
        removed = 0
        for key in ids:
            if self._vectors.pop(key, None) is not None:
                removed += 1
        return removed

    def count(self) -> int:
        return len(self._vectors)

    def healthy(self) -> bool:
        return True


class ChromaVectorIndex:
    """Optional persistent backend.

    Install with ``pip install -e ".[memory]"``. Chroma is imported lazily so
    the base install stays lightweight and Windows-friendly.
    """

    backend = "chroma"

    def __init__(
        self,
        persist_dir: str | Path,
        collection_name: str = "ecv_memory",
        dimension: int = 256,
    ) -> None:
        try:
            import chromadb  # type: ignore[import-not-found]
        except Exception as exc:  # pragma: no cover - optional dependency
            raise VectorBackendUnavailable(f"chromadb is not installed: {exc}") from exc
        self.dimension = dimension
        path = Path(persist_dir)
        path.mkdir(parents=True, exist_ok=True)
        self._client = chromadb.PersistentClient(path=str(path))
        self._collection = self._client.get_or_create_collection(
            name=collection_name,
            metadata={"hnsw:space": "cosine"},
        )
        self._last_error: str | None = None

    def upsert(self, records: list[VectorRecord]) -> int:
        if not records:
            return 0
        try:
            self._collection.upsert(
                ids=[record.id for record in records],
                embeddings=[record.vector for record in records],
                documents=[record.text for record in records],
                metadatas=[record.metadata for record in records],
            )
        except Exception as exc:  # pragma: no cover - backend-specific
            raise VectorQueryError(f"chroma upsert failed: {exc}") from exc
        return len(records)

    def query(
        self,
        vector: list[float],
        top_k: int = 10,
        allowed_ids: set[str] | None = None,
    ) -> list[tuple[str, float]]:
        try:
            result = self._collection.query(
                query_embeddings=[vector],
                n_results=max(top_k * 4, top_k),
                include=["distances"],
            )
        except Exception as exc:  # pragma: no cover - backend-specific
            raise VectorQueryError(f"chroma query failed: {exc}") from exc
        ids = (result.get("ids") or [[]])[0]
        distances = (result.get("distances") or [[]])[0]
        pairs: list[tuple[str, float]] = []
        for event_id, distance in zip(ids, distances, strict=False):
            if allowed_ids is not None and event_id not in allowed_ids:
                continue
            # Cosine distance in Chroma: score = 1 - distance.
            pairs.append((str(event_id), 1.0 - float(distance)))
        pairs.sort(key=lambda pair: pair[1], reverse=True)
        return pairs[: max(0, top_k)]

    def delete(self, ids: list[str]) -> int:
        if not ids:
            return 0
        try:
            self._collection.delete(ids=ids)
        except Exception as exc:  # pragma: no cover - backend-specific
            raise VectorQueryError(f"chroma delete failed: {exc}") from exc
        return len(ids)

    def count(self) -> int:
        try:
            return int(self._collection.count())
        except Exception as exc:  # pragma: no cover - backend-specific
            raise VectorQueryError(f"chroma count failed: {exc}") from exc

    def healthy(self) -> bool:
        try:
            self.count()
            return True
        except Exception:
            return False


def build_vector_index(
    backend: str,
    *,
    dimension: int,
    persist_dir: str | Path,
    collection_name: str = "ecv_memory",
    embedder: Embedder | None = None,
) -> tuple[VectorIndex, str | None]:
    """Return ``(index, warning)``; never let an optional backend break startup."""
    normalized = backend.strip().lower()
    if normalized in {"memory", "inmemory", "in-memory", "auto", ""}:
        return InMemoryVectorIndex(dimension), None
    if normalized == "chroma":
        try:
            return ChromaVectorIndex(persist_dir, collection_name, dimension), None
        except Exception as exc:
            warning = (
                f"chroma unavailable, fell back to in-memory index: {type(exc).__name__}: {exc}"
            )
            log.warning(warning)
            return InMemoryVectorIndex(dimension), warning
    warning = f"unknown vector backend {backend!r}, used in-memory index"
    log.warning(warning)
    return InMemoryVectorIndex(dimension), warning
