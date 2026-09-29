# linecite

**Verbatim, line-cited memory for AI agents.** Zero services, no API key
required.

Most agent-memory systems extract "facts" from conversations with an LLM
- lossy, unverifiable, and hard to audit. Projects like
[MemPalace](https://github.com/MemPalace/mempalace) have shown the
demand for verbatim, extraction-free memory on the conversation side.
**linecite is the artifact-side counterpart**: memory whose unit is an
exact line range of a real file, with a citation contract that is
*enforced and mechanically verifiable*.

- **Verbatim chunks** - every chunk stores `raw_text`, the exact
  `\n`-joined slice of `source_path:Lstart-Lend`. No LLM rewriting.
- **Verifiable citations** - `validate_verbatim(chunk, repo_root)`
  re-reads the live file and tells you `content_drift` /
  `source_missing` instead of letting an agent cite stale lines.
- **No API key, no services** - the default retrieval path is DuckDB
  full-text search (BM25) over a single local database file. Retrieval
  quality is a ladder you opt into: BM25 (zero deps) → local embeddings
  (still no key) → API embeddings → hybrid. Stored vectors are never
  silently re-embedded, and every vector records which model produced
  it (`embedding_model`) so model spaces never mix.
- **Provenance-keyed upserts** - chunks carry host/repo/ordinal/sha
  identity; re-ingest reconciles (insert/update/delete) instead of
  duplicating, and a failed scan can never mass-delete.

## Quickstart (no key needed)

```bash
pip install linecite
linecite-ingest ./my-repo --db kb.duckdb
```

```python
from linecite import ChunkStore, Retriever, validate_verbatim

store = ChunkStore("kb.duckdb")
res = Retriever(store).retrieve("how do we handle retries", k=5,
                                verify_root="./my-repo")
for chunk, score in zip(res.chunks, res.scores):
    fresh = chunk.chunk_id not in res.stale_citations
    print(f"{chunk.source_path}:L{chunk.start_line}-{chunk.end_line}"
          f"  [{'fresh' if fresh else 'STALE'}]  {score:.3f}")
```

## Semantic retrieval without an API key (optional)

```bash
pip install 'linecite[local-embeddings]'
linecite-ingest ./my-repo --db kb.duckdb --embed local
```

`--embed local` uses a small static embedding model
([model2vec](https://github.com/MinishLab/model2vec) potion-base-8M:
no torch, ~30MB, millisecond CPU inference), so paraphrased queries
match even with zero word overlap — still no API key, no runtime
service. Honest note: the model itself is downloaded from Hugging
Face **once** on first use and cached (same spirit as DuckDB's
one-time extension install); fully offline machines should
pre-download it and pass `--model-path`. OpenAI embeddings
(`linecite[embeddings]`) remain available as the API rung of the
ladder.

With an embedder attached, `Retriever.retrieve` defaults to **hybrid**
(BM25 + vector, reciprocal rank fusion); pass `mode="vector"` or
`mode="lexical"` to run a single leg:

```python
r = Retriever(store, embedder=emb)
res = r.retrieve("how do we retry", k=5)
res.retrieval_mode   # "hybrid"

# migration: pre-hybrid releases ran pure vector search when an
# embedder was attached — keep that behavior explicitly:
old_style = r.retrieve("how do we retry", k=5, mode="vector")
```

Measured on the bundled **synthetic paraphrase regression fixture**
(`evals/paraphrase-goldset.jsonl`, 20 queries; the `paraphrase` class
is constructed with zero content-word overlap against its target and
per-item verified, so it demonstrates the semantic leg's signal — not
general retrieval quality; `exact_term` guards against hybrid
regressing BM25's strengths):

| line | hit@5 | paraphrase h@5 | exact h@5 |
|---|---|---|---|
| bm25 | 0.400 | 0.000 | 1.000 |
| local-embed | 1.000 | 1.000 | 1.000 |
| hybrid | 1.000 | 1.000 | 1.000 |

Reproduce: `python scripts/eval_ladder.py` (needs the
local-embeddings extra).

## Why line-anchored citations

An agent that answers from memory should be auditable in one glance:
click the citation, see the exact lines. That requires the memory layer
to (1) keep the verbatim text, (2) anchor it to line ranges, and
(3) check the anchor against the live file at retrieval time. linecite
enforces all three at the storage/retrieval contract level - a chunk
with an invalid line range is rejected at write, and a drifted citation
is marked at read (`content_drift` / `source_missing` / `no_raw_text`).

## Status

Early release; the public API may still move. Roadmap: temporal supersession (valid_at / invalid_at /
superseded_by), scoped cross-host retrieval, standalone eval scorer +
public goldset format.

Notes: DuckDB loads its `fts` (and optional `vss`) extension on first
use — on most installs they ship with DuckDB or download once; for
fully offline machines pre-install them. Legacy rows ingested before
verbatim slices were recorded validate as `no_raw_text` (unverified)
rather than fresh.

## License

Apache-2.0
