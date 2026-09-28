"""Phase K acceptance: core passes its own conformance run (plan K.5),
and the frozen vectors validate against the machine-readable schemas
(the freeze guard's mechanical half — schemas were written from the
prose spec, so schema/vector agreement cross-checks both against code).
"""
from __future__ import annotations

import json
from pathlib import Path

from linecite.conformance import run_vectors

REPO = Path(__file__).resolve().parent.parent
VEC = REPO / "contract" / "vectors"


def test_core_passes_own_conformance_run():
    results = run_vectors(VEC)
    assert len(results) >= 4
    failures = [r for r in results if not r.ok]
    assert not failures, failures


def test_vectors_validate_against_frozen_schemas():
    """Real JSON Schema validation over EVERY vector's package surface.

    v3/v4 are semantically rejected (digest / row identity) but their
    JSON SHAPE is still part of the frozen surface: v4 in particular
    must be schema-valid while failing row_identity — proving schema
    validity alone is not conformance."""
    import jsonschema
    manifest_schema = json.loads(
        (REPO / "contract/schema/manifest.v0.schema.json").read_text())
    row_schema = json.loads(
        (REPO / "contract/schema/chunk-row.v0.schema.json").read_text())
    checked = 0
    for d in sorted(VEC.iterdir()):
        if not d.is_dir():
            continue
        m = json.loads((d / "manifest.json").read_text())
        jsonschema.validate(m, manifest_schema)
        for line in (d / "chunks.jsonl").read_text().splitlines():
            if line:
                jsonschema.validate(json.loads(line), row_schema)
        checked += 1
    assert checked == 4


def test_vector_regeneration_is_deterministic(tmp_path):
    # The freeze is reproducible: regenerating into a scratch dir
    # yields byte-identical packages (pinned fingerprint + frozen
    # clock). Guards against silent drift in scripts/gen_vectors.py.
    import shutil
    import subprocess
    import sys
    scratch = tmp_path / "repo"
    scratch.mkdir()
    for item in ("src", "scripts", "docs"):
        shutil.copytree(REPO / item, scratch / item)
    subprocess.run(
        [sys.executable, "scripts/gen_vectors.py"], cwd=scratch,
        check=True, capture_output=True)
    for d in sorted(VEC.iterdir()):
        if not d.is_dir():
            continue
        for f in ("manifest.json", "chunks.jsonl", "expected.json"):
            assert (scratch / "contract/vectors" / d.name / f).read_bytes() \
                == (d / f).read_bytes(), f"{d.name}/{f} drifted"
