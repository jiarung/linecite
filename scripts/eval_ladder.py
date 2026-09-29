#!/usr/bin/env python3
"""Three-line retrieval ladder eval: bm25 / local-embed / hybrid.

Runs the SAME corpus + goldset through the three retrieval modes and
scores each with linecite-score metrics. The bundled goldset
(evals/paraphrase-goldset.jsonl over evals/ladder-corpus/) is a
SYNTHETIC, narrow regression fixture (plan E3): the `paraphrase` class
is constructed with zero content-word overlap against its target — it
demonstrates that the semantic leg carries signal when lexical overlap
is absent, NOT general retrieval quality. `exact_term` guards the
other direction (hybrid must not regress BM25's strengths).

    .venv/bin/python scripts/eval_ladder.py [--k 5] [--history PATH]
    .venv/bin/python scripts/eval_ladder.py --verify-construction

--verify-construction is the freeze gate for goldset edits, with TWO
checks per paraphrase item: (1) structural — zero content-word overlap
between query and target under the documented policy (lowercased
alnum tokens, small stopword list, naive s/es/ed/ing suffix strip as a
stemming-collision heuristic); (2) empirical, authoritative — BM25
must actually miss the target in the top-k. Requires the
local-embeddings extra with the model cached.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from linecite.generic_ingester import ingest_paths            # noqa: E402
from linecite.local_embedder import LocalEmbedder             # noqa: E402
from linecite.retrieve import Retriever                       # noqa: E402
from linecite.score import score_run                          # noqa: E402
from linecite.store import ChunkStore                         # noqa: E402

CORPUS = REPO / "evals" / "ladder-corpus"
GOLDSET = REPO / "evals" / "paraphrase-goldset.jsonl"
LINES = ("bm25", "local-embed", "hybrid")
_MODE = {"bm25": "lexical", "local-embed": "vector", "hybrid": "hybrid"}

_STOP = frozenset("a an the to of for with on in and or is are how".split())


def _norm(tok: str) -> str:
    for suf in ("ing", "es", "ed", "s"):
        if tok.endswith(suf) and len(tok) - len(suf) >= 3:
            return tok[:-len(suf)]
    return tok


def _content_overlap(query: str, target_text: str) -> set:
    q = {_norm(w) for w in re.findall(r"[a-z0-9]+", query.lower())} - \
        {_norm(w) for w in _STOP}
    d = {_norm(w) for w in re.findall(r"[a-z0-9]+", target_text.lower())}
    return q & d


def build_store() -> tuple[ChunkStore, LocalEmbedder]:
    emb = LocalEmbedder()
    store = ChunkStore(dim=emb.dim)
    store.init_local_identity("ladder")
    ingest_paths(store, CORPUS, host="ladder", origin_repo="ladder",
                 embedder=emb)
    return store, emb


def history_row(ts: str, parent: str, line: str, k: int, m) -> dict:
    """Pure serializer for one history.jsonl row (hook schema + line);
    unit-tested without the model."""
    return {
        "ts": ts, "parent": parent,
        "goldset": "evals/paraphrase-goldset.jsonl",
        "line": line, "k": k, "n": m.n,
        f"hit@{k}": round(m.hit_at_k, 4),
        "p@1": round(m.precision_at_1, 4),
        "mrr": round(m.mrr, 4), "ndcg": round(m.ndcg, 4),
        "by_class": {c: round(cm.hit_at_k, 4)
                     for c, cm in m.by_class.items()},
    }


def run_line(retriever: Retriever, gold: list[dict], line: str,
             k: int, out_dir: Path) -> Path:
    out = out_dir / f"results-{line}.jsonl"
    with out.open("w") as f:
        for g in gold:
            res = retriever.retrieve(g["query"], k=k, mode=_MODE[line])
            f.write(json.dumps({
                "query_id": g["query_id"],
                "retrieved": [c.source_path for c in res.chunks]}) + "\n")
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--history", default=str(REPO / "local/history.jsonl"),
                    help="developer-local trend log (jsonl; NOT tracked)")
    ap.add_argument("--out-dir", default=None)
    ap.add_argument("--verify-construction", action="store_true")
    args = ap.parse_args(argv)

    gold = [json.loads(l) for l in
            GOLDSET.read_text("utf-8").splitlines() if l]
    store, emb = build_store()
    retriever = Retriever(store, embedder=emb)

    if args.verify_construction:
        bad = 0
        for g in gold:
            if g["query_class"] != "paraphrase":
                continue
            target_text = " ".join(
                (CORPUS / doc).read_text("utf-8")
                for doc in g["expected"])
            overlap = _content_overlap(g["query"], target_text)
            res = retriever.retrieve(g["query"], k=args.k, mode="lexical")
            hit = any(c.source_path in g["expected"] for c in res.chunks)
            fail = bool(overlap) or hit
            print(f"{g['query_id']}  overlap={sorted(overlap) or '-'}  "
                  f"bm25_hits_target={hit}  "
                  f"{'** REWORD **' if fail else 'ok'}  {g['query']!r}")
            bad += fail
        print(f"\n{bad} paraphrase item(s) need rewording"
              if bad else "\nconstruction OK: zero content-word overlap "
                          "AND bm25 misses every paraphrase target")
        return 1 if bad else 0

    import tempfile
    out_dir = Path(args.out_dir) if args.out_dir else \
        Path(tempfile.mkdtemp(prefix="ladder-"))
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        parent = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=REPO,
            capture_output=True, text=True).stdout.strip() or "root"
    except OSError:
        parent = "root"
    rows = []
    hist = Path(args.history)
    # local/ is gitignored: a fresh checkout has no such directory.
    hist.parent.mkdir(parents=True, exist_ok=True)
    for line in LINES:
        results = run_line(retriever, gold, line, args.k, out_dir)
        m = score_run(results, GOLDSET, k=args.k)
        rows.append((line, m))
        with hist.open("a") as f:
            f.write(json.dumps(
                history_row(ts, parent, line, args.k, m)) + "\n")

    print(f"{'line':<12} {'hit@' + str(args.k):>6} {'p@1':>6} "
          f"{'MRR':>6} {'para h@' + str(args.k):>9} "
          f"{'exact h@' + str(args.k):>10}")
    for line, m in rows:
        para = m.by_class.get("paraphrase")
        exact = m.by_class.get("exact_term")
        print(f"{line:<12} {m.hit_at_k:>6.3f} {m.precision_at_1:>6.3f} "
              f"{m.mrr:>6.3f} {para.hit_at_k:>9.3f} "
              f"{exact.hit_at_k:>10.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
