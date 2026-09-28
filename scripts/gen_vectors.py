#!/usr/bin/env python3
"""Regenerate the golden conformance vectors (contract/vectors/).

Phase K freeze procedure (plan K.2/K.2.5): vectors are frozen from REAL
export_snapshot output — deterministic via a frozen clock and a pinned
producer fingerprint — then each is adjudicated line-by-line against
docs/design/snapshot-contract.md before being committed
(contract/vectors/ADJUDICATION.md records the verdicts). Code is NOT
the default authority: a divergence found during adjudication must be
fixed on whichever side is wrong before the vector lands.

Usage: .venv/bin/python scripts/gen_vectors.py
"""
from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from linecite.generic_ingester import ingest_paths            # noqa: E402
from linecite.snapshot import (_canonical_json, _sha256,      # noqa: E402
                               export_snapshot)
from linecite.store import ChunkStore                         # noqa: E402

VEC = REPO / "contract" / "vectors"
FROZEN = datetime(2026, 9, 28, 0, 0, 0, tzinfo=timezone.utc)
PRODUCER_FP = "ab" * 16          # pinned for reproducibility


def _producer(tmp: Path, files: dict) -> ChunkStore:
    root = tmp / "repo"
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    s = ChunkStore(dim=4)
    s.init_local_identity("vechost")
    # Pin the generated fingerprint so regeneration is byte-stable.
    s._con.execute("UPDATE hosts SET fingerprint = ? WHERE origin='local'",
                   [PRODUCER_FP])
    ingest_paths(s, root, host="vechost", origin_repo="vecrepo")
    return s


def _redigest(pkg: Path) -> None:
    cb = (pkg / "chunks.jsonl").read_bytes()
    m = json.loads((pkg / "manifest.json").read_bytes())
    m["chunk_count"] = len([l for l in cb.split(b"\n") if l])
    m["content_digest"] = _sha256(cb)
    probe = {k: v for k, v in m.items() if k != "manifest_digest"}
    m["manifest_digest"] = _sha256(cb + _canonical_json(probe))
    (pkg / "manifest.json").write_bytes(_canonical_json(m))


def _expected(pkg: Path, obj: dict) -> None:
    (pkg / "expected.json").write_bytes(
        json.dumps(obj, indent=1, sort_keys=True).encode() + b"\n")


def main() -> int:
    import tempfile
    if VEC.exists():
        shutil.rmtree(VEC)
    VEC.mkdir(parents=True)

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        # v1 — basic accept: two markdown files, heading metadata intact.
        src = _producer(tmp, {
            "docs/alpha.md": "# Alpha\nalpha body line one\n",
            "docs/beta.md": "# Beta\nbeta body line one\n"})
        p1 = export_snapshot(src, "vecrepo", VEC / "v1-basic",
                             meta={"repo_url": "https://example.test/vec",
                                   "repo_sha": "0" * 40,
                                   "ingest_globs": ["**/*.md"],
                                   "ingested_at": "2026-09-28T00:00:00Z"},
                             now_fn=lambda: FROZEN)
        m1 = json.loads((p1 / "manifest.json").read_bytes())
        _expected(p1, {"outcome": "accepted",
                       "snapshot_seq": m1["snapshot_seq"],
                       "content_digest": m1["content_digest"],
                       "inserted": m1["chunk_count"]})

        # v2 — explicit empty snapshot (mass-delete guard exercised on
        # the accept side: empty:true, zero rows).
        src2 = _producer(tmp / "e", {})
        p2 = export_snapshot(src2, "vecrepo", VEC / "v2-empty",
                             now_fn=lambda: FROZEN)
        m2 = json.loads((p2 / "manifest.json").read_bytes())
        assert m2["empty"] is True and m2["chunk_count"] == 0
        _expected(p2, {"outcome": "accepted",
                       "snapshot_seq": m2["snapshot_seq"],
                       "content_digest": m2["content_digest"],
                       "inserted": 0})

        # v3 — error vector: manifest field tampered AFTER digest
        # (transport integrity must reject).
        p3 = VEC / "v3-tampered-manifest"
        shutil.copytree(p1, p3)
        (p3 / "expected.json").unlink()
        m3 = json.loads((p3 / "manifest.json").read_bytes())
        m3["origin_repo"] = "evilrepo"       # no redigest on purpose
        (p3 / "manifest.json").write_bytes(_canonical_json(m3))
        _expected(p3, {"outcome": "rejected", "code": "package_integrity"})

        # v4 — row identity tamper: text edited, digests recomputed so
        # the package is transport-valid but §4 recompute must reject.
        p4 = VEC / "v4-row-identity-tamper"
        shutil.copytree(p1, p4)
        (p4 / "expected.json").unlink()
        lines = [json.loads(l) for l in
                 (p4 / "chunks.jsonl").read_bytes().splitlines() if l]
        lines[0]["text"] = lines[0]["text"] + " tampered"
        (p4 / "chunks.jsonl").write_bytes(
            b"".join(_canonical_json(r) + b"\n" for r in lines))
        _redigest(p4)
        _expected(p4, {"outcome": "rejected", "code": "row_identity"})

    print(f"wrote 4 vectors under {VEC}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
