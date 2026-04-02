"""VectorStore — abstract interface + JSONVectorStore implementation.

  VectorStore      — ABC: upsert / search / delete / ids
  JSONVectorStore  — stores vectors in a JSON file; uses numpy for cosine similarity

The abstract base makes the storage backend swappable. To use ChromaDB or Qdrant
in the future: implement VectorStore, pass the new class to MemoryStore — nothing
else changes.

JSONVectorStore file format:
  {"vectors": {"<record_id>": [float, ...], ...}}

The file is loaded fully into memory at construction and written on every mutation.
This is appropriate for the scale of a personal coding assistant (<10k memories).
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from pathlib import Path

import numpy as np


class VectorStore(ABC):
    """Abstract interface for vector storage and similarity search."""

    @abstractmethod
    def upsert(self, record_id: str, embedding: list[float]) -> None:
        """Add or replace the embedding for record_id."""
        ...

    @abstractmethod
    def search(
        self,
        query_embedding: list[float],
        top_k: int,
        min_score: float = 0.0,
    ) -> list[tuple[str, float]]:
        """Return up to top_k results as [(record_id, cosine_similarity)].

        Results are sorted by similarity descending. Only results with
        similarity >= min_score are included.
        """
        ...

    @abstractmethod
    def delete(self, record_id: str) -> None:
        """Remove the embedding for record_id. No-ops if not present."""
        ...

    @abstractmethod
    def ids(self) -> list[str]:
        """Return all record IDs currently in the store."""
        ...


class JSONVectorStore(VectorStore):
    """VectorStore backed by a single JSON file, cosine similarity via numpy.

    Thread-safety: not thread-safe. Minion's REPL loop is single-threaded,
    so this is fine for Phase 6.
    """

    def __init__(self, index_path: Path) -> None:
        self._path = index_path
        self._vectors: dict[str, list[float]] = {}
        self._load()

    # ─── Persistence ──────────────────────────────────────────────────────────

    def _load(self) -> None:
        if self._path.exists():
            data = json.loads(self._path.read_text(encoding="utf-8"))
            self._vectors = data.get("vectors", {})

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps({"vectors": self._vectors}, indent=2),
            encoding="utf-8",
        )

    # ─── VectorStore interface ─────────────────────────────────────────────────

    def upsert(self, record_id: str, embedding: list[float]) -> None:
        self._vectors[record_id] = embedding
        self._save()

    def search(
        self,
        query_embedding: list[float],
        top_k: int,
        min_score: float = 0.0,
    ) -> list[tuple[str, float]]:
        if not self._vectors:
            return []

        q = np.array(query_embedding, dtype=np.float32)
        q_norm = float(np.linalg.norm(q))
        if q_norm == 0.0:
            return []

        results: list[tuple[str, float]] = []
        for record_id, vec in self._vectors.items():
            v = np.array(vec, dtype=np.float32)
            v_norm = float(np.linalg.norm(v))
            if v_norm == 0.0:
                continue
            score = float(np.dot(q, v) / (q_norm * v_norm))
            if score >= min_score:
                results.append((record_id, score))

        results.sort(key=lambda x: x[1], reverse=True)
        return results[:top_k]

    def delete(self, record_id: str) -> None:
        self._vectors.pop(record_id, None)
        self._save()

    def ids(self) -> list[str]:
        return list(self._vectors.keys())
