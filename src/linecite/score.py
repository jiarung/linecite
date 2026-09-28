"""Standalone retrieval scorer — pure stdlib, no store/embedder deps.

Scores a retrieval run against a goldset, both as JSONL files:

    results JSONL:  {"query_id": "...", "retrieved": ["item", ...]}
    goldset JSONL:  {"query_id": "...", "expected": ["item-or-prefix", ...],
                     "query_class": "..."}

A retrieved item matches when it equals an expected entry or starts
with one (expected entries may be path prefixes). Metrics: hit@k,
precision@1, MRR@k, nDCG@k, optional bootstrap 95% CI on hit@k, all
sliced by query_class. Queries missing from the results file count as
misses — a scorer that silently drops queries flatters the run.

CLI:
    linecite-score results.jsonl goldset.jsonl [--k 5] [--bootstrap 1000]
"""
from __future__ import annotations

import argparse
import json
import math
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class Metrics:
    n: int = 0
    hit_at_k: float = 0.0
    precision_at_1: float = 0.0
    mrr: float = 0.0
    ndcg: float = 0.0
    k: int = 5
    hit_ci: Optional[tuple] = None          # (lo, hi) when bootstrapped
    by_class: dict = field(default_factory=dict)   # class -> Metrics


def _load_jsonl(path: Path | str) -> list[dict]:
    rows = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def _matches(item: str, expected: list[str]) -> bool:
    return any(item == e or item.startswith(e) for e in expected)


def _per_query(retrieved: list[str], expected: list[str], k: int
               ) -> tuple[float, float, float, float]:
    """(hit, p@1, rr, ndcg) for one query.

    Binary relevance with each expected entry creditable ONCE: repeated
    retrieved items matching the same expected prefix earn no extra gain,
    and IDCG is the gold capacity min(len(expected), k) — so nDCG is
    always in [0, 1] regardless of duplicates."""
    top = retrieved[:k]
    remaining = list(expected)
    hits: list[float] = []
    for r in top:
        matched = next((e for e in remaining
                        if r == e or r.startswith(e)), None)
        if matched is not None:
            remaining.remove(matched)
            hits.append(1.0)
        else:
            hits.append(0.0)
    hit = 1.0 if any(hits) else 0.0
    p1 = hits[0] if hits else 0.0
    rr = 0.0
    for i, h in enumerate(hits):
        if h:
            rr = 1.0 / (i + 1)
            break
    dcg = sum(h / math.log2(i + 2) for i, h in enumerate(hits))
    ideal_hits = min(len(expected), k)
    idcg = sum(1.0 / math.log2(i + 2) for i in range(ideal_hits))
    ndcg = dcg / idcg if idcg else 0.0
    return hit, p1, rr, ndcg


def _aggregate(rows: list[tuple], k: int) -> Metrics:
    n = len(rows)
    if n == 0:
        return Metrics(k=k)
    return Metrics(
        n=n,
        hit_at_k=sum(r[0] for r in rows) / n,
        precision_at_1=sum(r[1] for r in rows) / n,
        mrr=sum(r[2] for r in rows) / n,
        ndcg=sum(r[3] for r in rows) / n,
        k=k,
    )


def score_run(results_path: Path | str, goldset_path: Path | str, *,
              k: int = 5, bootstrap: int = 0,
              seed: Optional[int] = None) -> Metrics:
    """Score one retrieval run against a goldset."""
    if k <= 0:
        raise ValueError("k must be > 0")
    if bootstrap < 0:
        raise ValueError("bootstrap must be >= 0")
    results = {r["query_id"]: r.get("retrieved", [])
               for r in _load_jsonl(results_path)}
    gold = _load_jsonl(goldset_path)
    per_q: list[tuple] = []
    per_class: dict[str, list[tuple]] = {}
    for g in gold:
        retrieved = results.get(g["query_id"], [])
        row = _per_query(retrieved, g["expected"], k)
        per_q.append(row)
        per_class.setdefault(g.get("query_class", "unclassified"),
                             []).append(row)
    m = _aggregate(per_q, k)
    m.by_class = {c: _aggregate(rows, k) for c, rows in per_class.items()}
    if bootstrap and per_q:
        rng = random.Random(seed)
        samples = []
        for _ in range(bootstrap):
            pick = [per_q[rng.randrange(len(per_q))] for _ in per_q]
            samples.append(sum(r[0] for r in pick) / len(pick))
        samples.sort()
        n_s = len(samples)
        lo = samples[math.floor(0.025 * (n_s - 1))]
        hi = samples[math.ceil(0.975 * (n_s - 1))]
        m.hit_ci = (lo, hi)
    return m


def _fmt(m: Metrics, k: int) -> str:
    ci = (f"  [95% CI {m.hit_ci[0]:.3f}-{m.hit_ci[1]:.3f}]"
          if m.hit_ci else "")
    return (f"n={m.n}  hit@{k}={m.hit_at_k:.3f}{ci}  "
            f"p@1={m.precision_at_1:.3f}  MRR@{k}={m.mrr:.3f}  "
            f"nDCG@{k}={m.ndcg:.3f}")


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="linecite-score",
        description="Score a retrieval-results JSONL against a goldset.")
    ap.add_argument("results", help="results JSONL (query_id, retrieved)")
    ap.add_argument("goldset", help="goldset JSONL (query_id, expected, query_class)")
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--bootstrap", type=int, default=1000)
    ap.add_argument("--seed", type=int, default=None)
    args = ap.parse_args(argv)
    m = score_run(args.results, args.goldset, k=args.k,
                  bootstrap=args.bootstrap, seed=args.seed)
    print("overall  " + _fmt(m, args.k))
    for cls in sorted(m.by_class):
        print(f"{cls:<8} " + _fmt(m.by_class[cls], args.k))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
