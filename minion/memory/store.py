"""MemoryStore — orchestrator for all memory operations.

Single responsibility: coordinate retrieval, storage, extraction, and deletion
across two memory scopes (global and project). No LLM prompts live here;
those are in extractor.py.

Retrieval algorithm:
  1. Embed the query (or fall back to keyword search)
  2. Search both global and project vector indexes
  3. Load MemoryRecord for each candidate
  4. Score = 0.7×similarity + 0.2×recency + 0.1×type_weight
  5. Sort, take top_k
  6. Pairwise consolidation check: if any pair > consolidation_threshold,
     call extractor.consolidate() and apply the result

Storage:
  - Records route by scope: global → global_dir, project → project_dir
  - Each record written as <id>.md in the records/ subdirectory
  - Vector index updated when embedder is available

Keyword fallback (no embedder):
  - grep file content for query terms
  - sort by recency only; no scoring
"""

from __future__ import annotations

import math
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ..llm.base import LLMClient
from ..tracing import get_tracer
from .config import MemoryConfig
from .embedder import Embedder
from .extractor import ConsolidationResult, MemoryExtractor
from .record import MemoryRecord
from .vector_store import JSONVectorStore


# Categories always injected regardless of query — the agent's "core memory"
ALWAYS_INJECT_CATEGORIES = frozenset({"identity", "preference", "project"})


class MemoryStore:
    """Orchestrates all memory operations across global and project scopes."""

    def __init__(
        self,
        config: MemoryConfig,
        project_cwd: Path,
        client: LLMClient,
        embedder: Optional[Embedder] = None,
    ) -> None:
        self._config = config
        self._client = client
        self._embedder = embedder
        self._extractor = MemoryExtractor(config)

        self._global_dir = config.global_memory_dir
        self._project_dir = project_cwd / ".minion" / "memory"

        self._global_index = JSONVectorStore(self._global_dir / "index.json")
        self._project_index = JSONVectorStore(self._project_dir / "index.json")

    # ─── Public API ───────────────────────────────────────────────────────────

    def retrieve(self, query: str) -> list[MemoryRecord]:
        """Return memories to inject before a turn.

        Two-pass retrieval:
        1. Core memories — all non-superseded records whose category is in
           ALWAYS_INJECT_CATEGORIES (identity, preference, project). These are
           always returned regardless of the query.
        2. Query-relevant memories — vector + keyword search over event-category
           records, merged and deduplicated against core results.

        Runs consolidation on any pair that exceeds the consolidation threshold.
        """
        all_active = [r for r in self._all_records() if r.superseded_by is None]

        # Pass 1: core memories — always inject
        core = [r for r in all_active if r.category in ALWAYS_INJECT_CATEGORIES]
        core.sort(key=lambda r: r.created_at, reverse=True)
        core_ids = {r.id for r in core}

        # Pass 2: query-relevant event memories
        if self._embedder:
            query_hits = self._vector_retrieve(query)
            seen_ids = core_ids | {r.id for r in query_hits}
            for record in self._keyword_retrieve(query):
                if record.id not in seen_ids:
                    query_hits.append(record)
        else:
            query_hits = self._keyword_retrieve(query)

        query_hits = [r for r in query_hits if r.superseded_by is None and r.id not in core_ids]

        results = core + query_hits[: self._config.top_k]
        get_tracer().emit(
            "memory_retrieve",
            query=query,
            num_retrieved=len(results),
            memories=[r.content for r in results],
        )
        return results

    def store(self, record: MemoryRecord) -> None:
        """Write a memory record to disk and update the vector index."""
        get_tracer().emit(
            "memory_store",
            content=record.content,
            type=record.type,
            category=getattr(record, "category", ""),
            scope=record.scope,
        )
        records_dir = self._records_dir_for(record)
        records_dir.mkdir(parents=True, exist_ok=True)
        file_path = records_dir / f"{record.id}.md"
        file_path.write_text(record.to_file_content(), encoding="utf-8")

        if self._embedder:
            embedding = self._embedder.embed(record.content)
            index = self._index_for(record)
            index.upsert(record.id, embedding)

    def delete(self, record_id_or_query: str) -> int:
        """Delete memories by exact ID or fuzzy text match.

        Returns the count of records deleted.
        """
        deleted = 0
        for record in self._all_records():
            if record.id == record_id_or_query or record_id_or_query.lower() in record.content.lower():
                self._delete_record(record)
                deleted += 1
        return deleted

    def maybe_extract(self, prompt: str, response: str) -> list[MemoryRecord]:
        """Run extraction if the trigger fires.

        For each extracted record, checks for a similar existing record before
        storing. If similarity exceeds consolidation_threshold, calls the LLM
        to consolidate — preventing duplicates from accumulating over time.
        """
        if not self._config.trigger.should_extract(prompt, response):
            get_tracer().emit("memory_skip", reason="trigger_not_met")
            return []

        project_path = str(self._project_dir.parent.parent)  # <cwd>
        existing = [r for r in self._all_records() if r.superseded_by is None]
        extracted = self._extractor.extract(prompt, response, self._client, project_path, existing)

        stored: list[MemoryRecord] = []
        for record in extracted:
            existing = self._find_similar_existing(record)
            if existing is None:
                self.store(record)
                stored.append(record)
                continue

            result = self._extractor.consolidate(existing, record, self._client)
            if result.action == "supersede_a":
                # existing is outdated — replace with new record
                self._mark_superseded(existing, record.id)
                self.store(record)
                stored.append(record)
            elif result.action == "supersede_b":
                # new record is redundant — discard silently
                pass
            elif result.action == "merge" and result.merged_content:
                merged = self._create_merged(existing, record, result.merged_content)
                self.store(merged)
                self._mark_superseded(existing, merged.id)
                stored.append(merged)
            else:
                # keep_both
                self.store(record)
                stored.append(record)

        return stored

    def _find_similar_existing(self, record: MemoryRecord) -> Optional[MemoryRecord]:
        """Find an existing non-superseded record similar to the given one.

        Uses vector search when embedder is available, keyword overlap as fallback.
        Returns the most similar existing record above consolidation_threshold,
        or None if no close match is found.
        """
        if self._embedder:
            embedding = self._embedder.embed(record.content)
            index = self._index_for(record)
            hits = index.search(
                embedding, top_k=1,
                min_score=self._config.consolidation_threshold,
            )
            for record_id, _ in hits:
                existing = self._load_record_by_id(record_id)
                if existing and existing.superseded_by is None:
                    return existing
        else:
            # Keyword fallback — match if majority of significant words overlap
            words = [w.strip("\"'?!.,;:") for w in record.content.lower().split()]
            keywords = [w for w in words if len(w) >= 3]
            if not keywords:
                return None
            threshold = max(1, len(keywords) // 2)
            for existing in self._all_records():
                if existing.superseded_by is not None:
                    continue
                content_lower = existing.content.lower()
                overlap = sum(1 for kw in keywords if kw in content_lower)
                if overlap >= threshold:
                    return existing
        return None

    def list_all(self, query: Optional[str] = None) -> list[MemoryRecord]:
        """Return all non-superseded memories, optionally searched by query.

        Without query: returns all memories sorted by recency.
        With query: merges semantic search (when embedder available) and keyword
        search results, deduplicated by ID, sorted by recency. No consolidation
        side effects, no top_k cap, no similarity threshold — this is a browse
        operation for the user, not a retrieval for the LLM.
        """
        if not query:
            records = [r for r in self._all_records() if r.superseded_by is None]
            records.sort(key=lambda r: r.created_at, reverse=True)
            return records
        return self._search(query)

    def _search(self, query: str) -> list[MemoryRecord]:
        """Merge semantic + keyword results for a user-facing /recall query.

        Semantic search: embed query, find all records above zero similarity
        (no threshold, no top_k). Only runs when embedder is available.
        Keyword search: substring match on content and tags.
        Results are deduplicated by ID and sorted by recency.
        """
        seen: dict[str, MemoryRecord] = {}

        # Semantic search — no threshold, no top_k, no consolidation
        if self._embedder:
            query_vec = self._embedder.embed(query)
            for index, base_dir in (
                (self._global_index, self._global_dir),
                (self._project_index, self._project_dir),
            ):
                all_ids = index.ids()
                if all_ids:
                    hits = index.search(query_vec, top_k=len(all_ids), min_score=0.0)
                    for record_id, score in hits:
                        if score > 0.0 and record_id not in seen:
                            record = self._load_record_by_id(record_id)
                            if record and record.superseded_by is None:
                                seen[record_id] = record

        # Keyword search — always runs, catches what semantic may miss
        words = [w.strip("\"'?!.,;:") for w in query.lower().split()]
        keywords = [w for w in words if len(w) >= 3]
        for record in self._all_records():
            if record.superseded_by is not None or record.id in seen:
                continue
            content_lower = record.content.lower()
            tags_lower = [t.lower() for t in record.tags]
            if keywords and any(kw in content_lower or any(kw in t for t in tags_lower) for kw in keywords):
                seen[record.id] = record

        results = list(seen.values())
        results.sort(key=lambda r: r.created_at, reverse=True)
        return results

    def stats(self) -> dict:
        """Return counts and configuration info for the /memory slash command."""
        all_records = list(self._all_records())
        active = [r for r in all_records if r.superseded_by is None]
        global_count = sum(1 for r in active if r.scope == "global")
        project_count = sum(1 for r in active if r.scope == "project")
        return {
            "global_count": global_count,
            "project_count": project_count,
            "total_count": global_count + project_count,
            "has_embeddings": self._embedder is not None,
        }

    # ─── Retrieval internals ──────────────────────────────────────────────────

    def _vector_retrieve(self, query: str) -> list[MemoryRecord]:
        """Embed query, search both indexes, score, and return sorted candidates."""
        assert self._embedder is not None
        query_vec = self._embedder.embed(query)

        global_hits = self._global_index.search(
            query_vec, top_k=self._config.top_k * 2,
            min_score=self._config.similarity_threshold,
        )
        project_hits = self._project_index.search(
            query_vec, top_k=self._config.top_k * 2,
            min_score=self._config.similarity_threshold,
        )

        scored: list[tuple[MemoryRecord, float]] = []
        for record_id, sim in global_hits + project_hits:
            record = self._load_record_by_id(record_id)
            if record is None:
                continue
            score = self._combined_score(sim, record)
            scored.append((record, score))

        scored.sort(key=lambda x: x[1], reverse=True)
        return [r for r, _ in scored]

    def _keyword_retrieve(self, query: str) -> list[MemoryRecord]:
        """Keyword grep fallback when no embedder is available.

        Matches on any meaningful word (3+ chars) from the query against
        record content and tags, so natural language queries like
        "what's my name?" match records containing "name".
        """
        words = [w.strip("\"'?!.,;:") for w in query.lower().split()]
        keywords = [w for w in words if len(w) >= 3]
        if not keywords:
            return []
        matches: list[MemoryRecord] = []
        for record in self._all_records():
            content_lower = record.content.lower()
            tags_lower = [t.lower() for t in record.tags]
            if any(kw in content_lower or any(kw in t for t in tags_lower) for kw in keywords):
                matches.append(record)
        matches.sort(key=lambda r: r.created_at, reverse=True)
        return matches[: self._config.top_k * 2]

    def _combined_score(self, similarity: float, record: MemoryRecord) -> float:
        """Compute combined retrieval score from similarity, recency, and type."""
        recency = self._recency_score(record.created_at)
        type_weight = 1.0 if record.type == "semantic" else 0.8
        return 0.7 * similarity + 0.2 * recency + 0.1 * type_weight

    @staticmethod
    def _recency_score(iso_timestamp: str) -> float:
        """Return a 0–1 score that decays with age. Score = 1 / (1 + days_ago)."""
        if not iso_timestamp:
            return 0.0
        try:
            created = datetime.fromisoformat(iso_timestamp)
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            days_ago = max(0.0, (now - created).total_seconds() / 86400)
            return 1.0 / (1.0 + days_ago)
        except (ValueError, OverflowError):
            return 0.0

    # ─── Consolidation ────────────────────────────────────────────────────────

    def _maybe_consolidate(self, records: list[MemoryRecord]) -> list[MemoryRecord]:
        """Check all pairs; consolidate any pair above the threshold.

        Returns the updated list with superseded records removed.
        Only processes pairs once (upper triangle of the similarity matrix).
        Uses the vector index for pairwise similarity; skips when no embedder.
        """
        if not self._embedder or len(records) < 2:
            return records

        superseded_ids: set[str] = set()
        merged_additions: list[MemoryRecord] = []

        for i in range(len(records)):
            for j in range(i + 1, len(records)):
                a, b = records[i], records[j]
                if a.id in superseded_ids or b.id in superseded_ids:
                    continue
                sim = self._pair_similarity(a, b)
                if sim < self._config.consolidation_threshold:
                    continue

                result = self._extractor.consolidate(a, b, self._client)
                if result.action == "supersede_a":
                    self._mark_superseded(a, b.id)
                    superseded_ids.add(a.id)
                elif result.action == "supersede_b":
                    self._mark_superseded(b, a.id)
                    superseded_ids.add(b.id)
                elif result.action == "merge" and result.merged_content:
                    merged = self._create_merged(a, b, result.merged_content)
                    self.store(merged)
                    merged_additions.append(merged)
                    self._mark_superseded(a, merged.id)
                    self._mark_superseded(b, merged.id)
                    superseded_ids.update({a.id, b.id})
                # keep_both: do nothing

        surviving = [r for r in records if r.id not in superseded_ids]
        return surviving + merged_additions

    def _pair_similarity(self, a: MemoryRecord, b: MemoryRecord) -> float:
        """Compute cosine similarity between two records via their index entries."""
        assert self._embedder is not None
        a_hits = self._index_for(a).search(
            self._embedder.embed(a.content), top_k=1
        )
        b_hits = self._index_for(b).search(
            self._embedder.embed(b.content), top_k=1
        )
        if not a_hits or not b_hits:
            return 0.0
        # Cross-similarity: embed a, look up b's score for a's vector
        results = self._index_for(b).search(
            self._embedder.embed(a.content), top_k=len(self._index_for(b).ids()),
        )
        for rid, score in results:
            if rid == b.id:
                return score
        return 0.0

    def _mark_superseded(self, record: MemoryRecord, new_id: str) -> None:
        """Update record's superseded_by field on disk."""
        record.superseded_by = new_id
        file_path = self._records_dir_for(record) / f"{record.id}.md"
        if file_path.exists():
            file_path.write_text(record.to_file_content(), encoding="utf-8")

    def _create_merged(self, a: MemoryRecord, b: MemoryRecord, content: str) -> MemoryRecord:
        """Create a new merged record from two existing records."""
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return MemoryRecord(
            id=str(uuid.uuid4()),
            content=content,
            type=a.type,  # inherit from the first record
            scope=a.scope,
            project_path=a.project_path,
            tags=list(dict.fromkeys(a.tags + b.tags)),  # deduplicated, ordered
            created_at=now,
            superseded_by=None,
        )

    # ─── File I/O helpers ─────────────────────────────────────────────────────

    def _records_dir_for(self, record: MemoryRecord) -> Path:
        base = self._global_dir if record.scope == "global" else self._project_dir
        return base / "records"

    def _index_for(self, record: MemoryRecord) -> JSONVectorStore:
        return self._global_index if record.scope == "global" else self._project_index

    def _all_records(self) -> list[MemoryRecord]:
        """Load all MemoryRecord files from both global and project directories."""
        records: list[MemoryRecord] = []
        for base_dir in (self._global_dir, self._project_dir):
            records_dir = base_dir / "records"
            if not records_dir.exists():
                continue
            for path in records_dir.glob("*.md"):
                try:
                    records.append(MemoryRecord.from_file(path))
                except (ValueError, KeyError):
                    pass  # skip malformed files silently
        return records

    def _load_record_by_id(self, record_id: str) -> Optional[MemoryRecord]:
        """Try to load a record by ID from either scope directory."""
        for base_dir in (self._global_dir, self._project_dir):
            path = base_dir / "records" / f"{record_id}.md"
            if path.exists():
                try:
                    return MemoryRecord.from_file(path)
                except (ValueError, KeyError):
                    return None
        return None

    def _delete_record(self, record: MemoryRecord) -> None:
        """Remove the record file and its vector index entry."""
        file_path = self._records_dir_for(record) / f"{record.id}.md"
        if file_path.exists():
            file_path.unlink()
        self._index_for(record).delete(record.id)
