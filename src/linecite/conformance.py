"""Conformance runner over the frozen golden vectors (Phase K).

Lives in core (the contract directory itself holds NO code — only
schemas, clauses, and vectors). Any importer implementation can be
checked by passing its import callable; core's own CI runs this via
tests/test_k_conformance.py.

    linecite-conformance [contract/vectors]
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from linecite.snapshot import SnapshotError, import_snapshot
from linecite.store import ChunkStore

RUNNER_HOST = "conformance-runner"


@dataclass(frozen=True)
class VectorResult:
    name: str
    ok: bool
    detail: str


def _default_import(pkg: Path):
    """Fresh core store per vector — each vector is self-contained."""
    store = ChunkStore(dim=4)
    store.init_local_identity(RUNNER_HOST)
    try:
        return import_snapshot(store, pkg)
    finally:
        store.close()


def run_vectors(vectors_root: Path | str,
                import_fn: Callable[[Path], object] = _default_import
                ) -> list[VectorResult]:
    """Run every vector directory (has expected.json) through import_fn
    and compare outcomes. Fail-closed: a vector that cannot be read or
    produces an unexpected error shape counts as a failure."""
    root = Path(vectors_root)
    results: list[VectorResult] = []
    dirs = sorted(d for d in root.iterdir()
                  if d.is_dir() and (d / "expected.json").exists())
    if not dirs:
        return [VectorResult("(none)", False,
                             f"no vectors found under {root}")]
    for d in dirs:
        expected = json.loads((d / "expected.json").read_text("utf-8"))
        try:
            res = import_fn(d)
        except SnapshotError as e:
            if expected["outcome"] != "rejected":
                results.append(VectorResult(
                    d.name, False, f"unexpected rejection: {e}"))
            elif e.code != expected["code"]:
                results.append(VectorResult(
                    d.name, False,
                    f"rejected with {e.code!r}, expected "
                    f"{expected['code']!r}"))
            else:
                results.append(VectorResult(d.name, True, e.code))
            continue
        except Exception as e:            # noqa: BLE001 — fail closed
            results.append(VectorResult(
                d.name, False, f"non-contract error {type(e).__name__}: {e}"))
            continue
        if expected["outcome"] != "accepted":
            results.append(VectorResult(
                d.name, False, "accepted but expected rejection "
                f"{expected.get('code')!r}"))
            continue
        problems = []
        if res.snapshot_seq != expected["snapshot_seq"]:
            problems.append(f"seq {res.snapshot_seq}")
        if res.content_digest != expected["content_digest"]:
            problems.append("content_digest mismatch")
        inserted = res.reconcile.inserted if res.reconcile else 0
        if inserted != expected["inserted"]:
            problems.append(f"inserted {inserted}")
        results.append(VectorResult(
            d.name, not problems,
            "; ".join(problems) or "accepted"))
    return results


def main(argv: Optional[list] = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(
        prog="linecite-conformance",
        description="Run the frozen contract vectors against an importer.")
    ap.add_argument("vectors", nargs="?", default="contract/vectors")
    args = ap.parse_args(argv)
    results = run_vectors(args.vectors)
    ok = True
    for r in results:
        print(f"{'PASS' if r.ok else 'FAIL'}  {r.name}: {r.detail}")
        ok = ok and r.ok
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
