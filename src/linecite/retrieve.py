"""Query → (embed → vector search | FTS) → RetrievalResult.

Citation fields (start_line/end_line) enforced here, not at /ask layer.

Two modes, one contract:
- vector (embedder given): query → embed → store.query_topk
- lexical (embedder=None): query → store.query_fts (BM25, no API key)
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

from linecite.embedder import Embedder
from linecite.store import Chunk, ChunkStore


@dataclass(frozen=True)
class RetrievalResult:
    query: str
    chunks: list[Chunk]
    scores: list[float]
    retrieval_ms: float
    top_k: int
    reranker_used: bool = False
    # DEPRECATED (kept for compat): chunk_ids whose verbatim citation
    # failed validation. chunk_id is host-unqualified, so it is ambiguous
    # across hosts — use citation_checks instead.
    stale_citations: tuple = ()
    # Per-result citation validation records, same order as `chunks`
    # (only populated when verify_root is given): each entry is
    # (chunk_uid | None, chunk_id, reason | None) — reason None means
    # the citation verified fresh. A LIST of per-result records, not a
    # dict, because legacy rows may carry chunk_uid None (contract §9)
    # and a uid-keyed map would collapse them. Stale citations are
    # MARKED, never dropped — the caller decides.
    citation_checks: list = field(default_factory=list)
    # Cross-host duplicate marking (plan C2.3): groups of result indices
    # sharing the same content_sha (the natural dedup group id). Only
    # groups with 2+ members are listed; nothing is silently deduped —
    # dropping duplicates is the caller's decision.
    duplicate_groups: tuple = ()


class Retriever:
    def __init__(self, store: ChunkStore,
                 embedder: Optional[Embedder] = None):
        self.store = store
        self.embedder = embedder

    def retrieve(self, query: str, k: int = 5,
                 filters: dict | None = None,
                 verify_root=None, *,
                 local_host: Optional[str] = None,
                 scope: Optional[list] = None) -> RetrievalResult:
        """Embed query and retrieve top-k with optional metadata filters.

        filters passed through to store.query_topk()
        which applies SQL WHERE before scoring. Citation enforcement remains
        enforced on the filtered result set so shadow_runner can share this
        single path.

        scope: host allowlist (Phase C2) — the ergonomic wrapper over the
        store's existing host filter. Mutually exclusive with a 'host'
        key in filters.
        local_host: this store's host id (from store.local_identity());
        forwarded to validate_verbatim so remote chunks verify as
        remote_unverified instead of reading files that live elsewhere.
        """
        if not query or not query.strip():
            raise ValueError("query must be non-empty")
        if k <= 0:
            raise ValueError("k must be > 0")
        if scope is not None:
            if filters and "host" in filters:
                raise ValueError(
                    "pass either scope= or filters={'host': ...}, not both")
            if isinstance(scope, (str, bytes)):
                raise ValueError(
                    "scope must be a list of host ids, not a single "
                    "string (a str would be split into characters)")
            scope = list(scope)
            if not scope or not all(
                    isinstance(h, str) and h for h in scope):
                raise ValueError(
                    "scope must be a non-empty list of non-empty "
                    "host strings")
            filters = {**(filters or {}), "host": scope}

        t0 = time.monotonic()
        if self.embedder is None:
            hits = self.store.query_fts(query, k=k, filters=filters)
        else:
            [qvec] = self.embedder.embed([query])
            # Model-space isolation: only compare against vectors the
            # same model produced (store-level hard rule).
            hits = self.store.query_topk(
                qvec, k=k, filters=filters,
                embedding_model=self.embedder.model)
        elapsed_ms = (time.monotonic() - t0) * 1000

        chunks = [c for c, _ in hits]
        scores = [s for _, s in hits]

        # Citation enforcement 
        for c in chunks:
            if c.start_line < 1 or c.end_line < c.start_line:
                raise RuntimeError(
                    f"chunk {c.chunk_id} violates citation contract: "
                    f"start_line={c.start_line}, end_line={c.end_line}"
                )

        # Optional verbatim verification : mark, never drop.
        stale: tuple = ()
        checks: list = []
        if verify_root is not None:
            from linecite.validate_citation import validate_verbatim
            stale_ids = []
            for c in chunks:
                res_c = validate_verbatim(c, verify_root,
                                          local_host=local_host)
                checks.append((c.chunk_uid, c.chunk_id, res_c.reason))
                if not res_c.valid:
                    stale_ids.append(c.chunk_id)
            stale = tuple(stale_ids)

        # Cross-host duplicate marking: group result indices by
        # content_sha; groups of 2+ are surfaced, nothing is dropped.
        by_sha: dict = {}
        for i, c in enumerate(chunks):
            if c.content_sha is not None:
                by_sha.setdefault(c.content_sha, []).append(i)
        dup_groups = tuple(tuple(v) for v in by_sha.values() if len(v) > 1)

        return RetrievalResult(
            query=query,
            chunks=chunks,
            scores=scores,
            retrieval_ms=round(elapsed_ms, 2),
            top_k=k,
            reranker_used=False,
            stale_citations=stale,
            citation_checks=checks,
            duplicate_groups=dup_groups,
        )
