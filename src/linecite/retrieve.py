"""Query → (lexical | vector | hybrid) → RetrievalResult.

Citation fields (start_line/end_line) enforced here, not at /ask layer.

Modes, one contract (mode="auto" resolves by embedder presence):
- lexical: store.query_fts (BM25, no API key) — the no-embedder default
- vector: query → embed → store.query_topk (model-space isolated)
- hybrid: both legs fused with reciprocal rank fusion — the default
  whenever an embedder is attached
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

from linecite.embedder import Embedder
from linecite.store import Chunk, ChunkStore


RRF_K = 60          # rank-fusion constant (standard default)


@dataclass(frozen=True)
class RetrievalResult:
    """One retrieval outcome. `retrieval_mode` names the resolved path
    ("lexical" | "vector" | "hybrid" — never "auto") and governs the
    score semantics: BM25 for lexical, cosine similarity for vector,
    RRF mass (sum of 1/(RRF_K + rank) across legs) for hybrid. Scores
    are NOT comparable across modes; serialize retrieval_mode next to
    them."""

    query: str
    chunks: list[Chunk]
    # Score semantics depend on retrieval_mode and are NOT comparable
    # across modes: BM25 for "lexical", cosine similarity for
    # "vector", RRF mass (sum of 1/(RRF_K+rank) over legs) for
    # "hybrid". Downstream serializers should record retrieval_mode
    # alongside scores.
    scores: list[float]
    retrieval_ms: float
    top_k: int
    reranker_used: bool = False
    # Which path produced this result: "lexical" | "vector" | "hybrid"
    # (the resolved mode, never "auto").
    retrieval_mode: str = "lexical"
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
                 scope: Optional[list] = None,
                 mode: str = "auto") -> RetrievalResult:
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
        mode: "auto" (default) resolves to "hybrid" when an embedder is
        present, else "lexical". BEHAVIOR CHANGE (pre-release, see
        CHANGELOG): an embedder-equipped Retriever previously ran pure
        vector search — pass mode="vector" for the old behavior.
        "hybrid" fuses the lexical and vector legs with reciprocal
        rank fusion; filters/scope/model isolation apply INSIDE each
        leg before its depth cut, so scoped results can never be
        starved by out-of-scope fused heads. Note: the hybrid lexical
        leg is embedding-model-agnostic by design (BM25 relevance does
        not live in a vector space).
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

        if mode not in ("auto", "lexical", "vector", "hybrid"):
            raise ValueError(
                f"mode must be auto|lexical|vector|hybrid, got {mode!r}")
        if mode == "auto":
            mode = "hybrid" if self.embedder is not None else "lexical"
        if mode in ("vector", "hybrid") and self.embedder is None:
            raise ValueError(f"mode={mode!r} requires an embedder")

        t0 = time.monotonic()
        if mode == "lexical":
            hits = self.store.query_fts(query, k=k, filters=filters)
        elif mode == "vector":
            [qvec] = self.embedder.embed([query])
            # Model-space isolation: only compare against vectors the
            # same model produced (store-level hard rule).
            hits = self.store.query_topk(
                qvec, k=k, filters=filters,
                embedding_model=self.embedder.model)
        else:
            hits = self._hybrid(query, k, filters)
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
            retrieval_mode=mode,
            stale_citations=stale,
            citation_checks=checks,
            duplicate_groups=dup_groups,
        )

    def _hybrid(self, query: str, k: int,
                filters: dict | None) -> list[tuple[Chunk, float]]:
        """Reciprocal rank fusion of the lexical and vector legs.

        Each leg fetches max(4k, 20) candidates WITH the full
        filters/scope (and, for the vector leg, model isolation)
        applied inside the leg — filtering after fusion could starve
        scoped results entirely. Fused score = sum over legs of
        1/(RRF_K + rank); ties break on chunk_uid for determinism.
        Fixture-bound property, not a theorem: RRF can demote a
        single-leg winner when the other leg ranks distractors.
        """
        depth = max(4 * k, 20)
        lex = self.store.query_fts(query, k=depth, filters=filters)
        [qvec] = self.embedder.embed([query])
        vec = self.store.query_topk(
            qvec, k=depth, filters=filters,
            embedding_model=self.embedder.model)
        fused: dict = {}      # chunk_uid -> [chunk, rrf_score]
        for leg in (lex, vec):
            for rank, (chunk, _score) in enumerate(leg, start=1):
                # Legacy rows (uid None) must not fuse across hosts on
                # the host-unqualified chunk_id — key on the full
                # provenance tuple instead.
                key = chunk.chunk_uid or "legacy\0" + "\0".join(
                    str(x) for x in (
                        chunk.host, chunk.origin_repo,
                        chunk.source_path, chunk.chunk_ordinal,
                        chunk.content_sha, chunk.chunk_id))
                entry = fused.setdefault(key, [chunk, 0.0])
                entry[1] += 1.0 / (RRF_K + rank)
        ordered = sorted(
            fused.items(), key=lambda kv: (-kv[1][1], kv[0]))
        return [(c, s) for _, (c, s) in ordered[:k]]
