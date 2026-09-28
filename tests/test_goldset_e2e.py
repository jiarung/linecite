"""End-to-end smoke test: ingest this repo's docs -> retrieve -> score.

A regression smoke test of the full no-key loop, NOT evidence of
retrieval quality — the goldset queries deliberately mirror this repo's
own docs so the loop should score high; a drop below the threshold
signals a wiring regression.
"""
from __future__ import annotations

import json
from pathlib import Path

from linecite import ChunkStore, Retriever, ingest_paths
from linecite.score import score_run

REPO = Path(__file__).resolve().parent.parent


def test_own_docs_goldset_smoke(tmp_path):
    store = ChunkStore(dim=4)
    ingest_paths(store, REPO, host="ci", origin_repo="linecite",
                 globs=("README.md", "docs/*.md"))
    retriever = Retriever(store)          # lexical, no key
    gold = [json.loads(l) for l in
            (REPO / "evals/goldset.jsonl").read_text().splitlines() if l]
    results = []
    for g in gold:
        res = retriever.retrieve(g["query"], k=5)
        results.append({"query_id": g["query_id"],
                        "retrieved": [c.source_path for c in res.chunks]})
    rpath = tmp_path / "results.jsonl"
    rpath.write_text("\n".join(json.dumps(r) for r in results) + "\n")
    m = score_run(rpath, REPO / "evals/goldset.jsonl", k=5,
                  bootstrap=200, seed=1)
    assert m.n == len(gold)
    assert m.hit_at_k >= 0.75, f"goldset hit@5 too low: {m.hit_at_k}"
    assert set(m.by_class) == {"doc_lookup", "behavior"}
