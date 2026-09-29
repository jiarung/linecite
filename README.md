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

Three pillars, all local-first:

1. **A citation contract you can check** — every chunk is a verbatim
   line-range slice, and every citation is re-verified against the
   live file at retrieval time.
2. **A retrieval-quality ladder you opt into** — BM25 by default (zero
   deps), local embeddings (still no key), API embeddings, hybrid
   fusion. The citation contract is identical on every rung.
3. **Team memory without a server** — a host's memory exports as a
   digest-verified snapshot directory that any teammate imports
   atomically; the exchange is a frozen, conformance-tested contract,
   not a service.

In detail:

- **Verbatim chunks** - every chunk stores `raw_text`, the exact
  `\n`-joined slice of `source_path:Lstart-Lend`. No LLM rewriting.
- **Verifiable citations** - `validate_verbatim(chunk, repo_root)`
  re-reads the live file and answers with a closed status set:
  `content_drift` / `source_missing` / `no_raw_text` /
  `remote_unverified` — instead of letting an agent cite stale lines.
- **No API key, no services** - the default retrieval path is DuckDB
  full-text search (BM25) over a single local database file. Stored
  vectors are never silently re-embedded, and every vector records
  which model produced it (`embedding_model`) so model spaces never
  mix.
- **Provenance-keyed upserts** - chunks carry host/repo/ordinal/sha
  identity computed from content (never trusted as declared);
  re-ingest reconciles (insert/update/delete) instead of duplicating,
  and a failed scan can never mass-delete.

## Quickstart (no key needed)

```bash
pip install linecite
linecite-ingest ./my-repo --db kb.duckdb
```

```python
from linecite import ChunkStore, Retriever

store = ChunkStore("kb.duckdb")
res = Retriever(store).retrieve("how do we handle retries", k=5,
                                verify_root="./my-repo")
for chunk, score, (_uid, _cid, reason) in zip(
        res.chunks, res.scores, res.citation_checks):
    print(f"{chunk.source_path}:L{chunk.start_line}-{chunk.end_line}"
          f"  [{reason or 'fresh'}]  {score:.3f}")
```

A drifted citation is *marked, never dropped* — the caller decides
what stale means (re-ingest, warn, or filter).

## The retrieval ladder

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
(`linecite[embeddings]`) remain available as the API rung.

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

## Team memory without a server

A store's local partition exports as a plain directory — canonical
JSON, digest-verified, replay-protected — that travels over anything
(git, rsync, object storage, a USB stick) and imports atomically:

```python
from linecite import export_snapshot, import_snapshot

pkg = export_snapshot(my_store, "my-repo", "snapshots/laptop-a/")
# ...move the directory however you like...
import_snapshot(team_store, "snapshots/laptop-a/")   # all-or-nothing
```

What the exchange guarantees:

- **Identity is computed, not declared** — importers recompute every
  chunk's content hash and identity keys and reject the whole package
  on any mismatch; a tampered row cannot slip in.
- **Replay protection** — snapshots carry a per-stream monotonic
  sequence; an old snapshot cannot resurrect deleted chunks, and an
  empty snapshot must say so explicitly (no accidental mass-delete).
- **Host identity** — every machine carries a stable fingerprint;
  same-name collisions are rejected, and one host can never write
  another host's partition outside a validated import.
- **Secrets stay home** — the export hook drops `.env`/key-file
  chunks by default and records the exclusion count in the manifest.
- **Honest freshness** — imported chunks retrieve normally (scope
  queries to hosts with `retrieve(..., scope=["laptop-a"])`), but
  their citations verify as `remote_unverified`: the source files
  live on another machine, and linecite won't pretend otherwise. The
  verbatim text itself is still exact as-of-export.
- Embeddings never travel — each machine builds its own semantic
  index (`store.reembed(embedder)`) over imported chunks.

The wire format is a frozen, versioned contract: JSON Schemas, MUST
clauses, golden test vectors, and an adjudication log live in
[`contract/`](contract/), with the prose specification in
[`docs/design/snapshot-contract.md`](docs/design/snapshot-contract.md).
Any independent importer can check itself with
`linecite-conformance contract/vectors`.

## Why line-anchored citations

An agent that answers from memory should be auditable in one glance:
click the citation, see the exact lines. That requires the memory layer
to (1) keep the verbatim text, (2) anchor it to line ranges, and
(3) check the anchor against the live file at retrieval time. linecite
enforces all three at the storage/retrieval contract level - a chunk
with an invalid line range is rejected at write, and a drifted citation
is marked at read.

## Status

Early release; the public API may still move (see CHANGELOG.md for
behavior changes). Shipped: the verify layer, provenance-keyed
reconciliation, the full retrieval ladder with model-provenance
isolation, host-scoped retrieval, the snapshot exchange contract v0
(unstable) with conformance vectors, and the standalone scorer
(`linecite-score`). Roadmap: temporal supersession
(valid_at / invalid_at / superseded_by), a real external-corpus
goldset, an MCP server, and a transport companion
(`linecite-sync`) built on the snapshot contract.

Notes: DuckDB loads its `fts` (and optional `vss`) extension on first
use — on most installs they ship with DuckDB or download once; for
fully offline machines pre-install them. Legacy rows ingested before
verbatim slices were recorded validate as `no_raw_text` (unverified)
rather than fresh.

## License

Apache-2.0
