# Changelog

All notable changes to linecite. Pre-1.0: breaking changes may land in
any release; this file is the migration record.

## Unreleased

### Changed (behavior)
- **Hybrid retrieval is now the default when an embedder is present.**
  `Retriever.retrieve` gains `mode="auto"|"lexical"|"vector"|"hybrid"`;
  `auto` resolves to `hybrid` (reciprocal rank fusion of the BM25 and
  vector legs) instead of the previous pure-vector behavior. Migration:
  pass `mode="vector"` to keep the old semantics. `RetrievalResult`
  gains `retrieval_mode`; score semantics differ per mode and are not
  comparable across modes.

### Added
- `chunks.embedding_model` column: vector provenance (which model
  produced the stored embedding). New writes enforce the pairing
  invariant `embedding IS NOT NULL <=> embedding_model IS NOT NULL`;
  pre-existing vectors with NULL model are quarantined legacy —
  untouched, excluded from model-scoped vector queries, reclaimable
  via the new explicit `ChunkStore.reembed(embedder, ...)` API (the
  one sanctioned vector-overwrite path).
- `query_topk` gains `embedding_model=` (required) and
  `allow_mixed_embedding_models=` (explicit opt-in): cosine across
  different embedding models is meaningless, so model-space isolation
  is now a hard rule. `Retriever` passes its embedder's model
  automatically.
- `ingest_paths(embedder=...)`: preferred embedding entry point (an
  object with `.embed/.model/.dim`). Bare `embed_fn=` now requires
  `embedding_model=`.
- Store dimension is persisted in `chunks_meta` and checked at open:
  wrong constructor `dim` fails immediately with a clear message.
  Pre-existing DBs infer the dim from the physical embedding column.

### Migration notes
- No `SCHEMA_VERSION` bump: `embedding_model` arrives via the existing
  idempotent ALTER/backfill path (no key or index changes).
- Direct `query_topk` callers must now pass `embedding_model=` or
  `allow_mixed_embedding_models=True`.
- Snapshot contract: `embedding_model` joins `embedding`/`embedded_at`
  as local storage metadata — never exported, NULL on import; explicit
  local-metadata updates (reembed) are exempt from the non-local-write
  prohibition (contract §9 / clause 14).
