"""Phase C2 acceptance: simulated multi-host goldset (plan C2.4/C2.5).

Three host keys in ONE store (no satellite needed), near-duplicate
cross-compile docs as distractors, goldset rows carrying `scope`
consumed by the retrieval runner (host filter). Asserts ≥15 queries run
and a measurable scoped-vs-unscoped gap on the distractor class.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from linecite.generic_ingester import ingest_paths
from linecite.retrieve import Retriever
from linecite.score import score_run
from linecite.store import ChunkStore

REPO = Path(__file__).resolve().parent.parent
GOLDSET = REPO / "evals/multihost-goldset.jsonl"

# Deterministic corpus. wn64 docs double the trap phrases so BM25
# prefers the WRONG host when the query is unscoped (cross-compile
# scenario: near-identical toolchain docs on two build hosts).
CORPUS = {
    "lx64": {  # origin_repo: toolchain
        "README.md": "# Toolchain\ncross compile setup guide\n"
                     "target triple selection for the linux runner\n",
        "build.md": "# Build\nstatic linking flags for the release build\n"
                    "use gcc with glibc\n",
        "deploy.md": "# Deploy\nartifact upload to the linux fleet\n",
    },
    "wn64": {  # origin_repo: toolchain
        "README.md": "# Toolchain\ncross compile setup guide\n"
                     "cross compile notes for the windows runner notes\n",
        "build.md": "# Build\nstatic linking and static linking caveats\n"
                    "use mingw for dll builds\n",
        "deploy.md": "# Deploy\nartifact upload and artifact upload "
                     "retries on windows\n",
        # Five short high-density docs per trap phrase: unscoped BM25
        # top-5 fills with wn64 for these queries, so the lx64 target
        # is pushed OUT of hit@5 — the C2.5 gap is measured on the
        # plan's stated metric, not a rank-order artifact.
        **{f"notes/cc{i}.md": "cross compile note\ncross compile tip\n"
           for i in range(5)},
        **{f"notes/sl{i}.md": "static linking note\nstatic linking tip\n"
           for i in range(5)},
        **{f"notes/au{i}.md": "artifact upload note\nartifact upload tip\n"
           for i in range(5)},
    },
    "dochub": {  # origin_repo: handbook
        "release.md": "# Release\nversion tagging policy and changelog "
                      "rules for the release train\n",
        "oncall.md": "# Oncall\npager rotation schedule for the platform "
                     "team\n",
        "style.md": "# Style\nnaming conventions for python modules and "
                    "module naming style rules\n",
    },
}
REPOS = {"lx64": "toolchain", "wn64": "toolchain", "dochub": "handbook"}


def _fp(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()[:32]


def _build_store(tmp_path) -> ChunkStore:
    s = ChunkStore(dim=4)
    hosts = list(CORPUS)
    s.init_local_identity(hosts[0])
    for h in hosts[1:]:
        # Plan C2.4 前置: registry invariant would otherwise reject
        # seeding foreign hosts (C0.5 escape hatch).
        s.register_host(h, _fp(h), allow_direct_writes=True)
    for h, files in CORPUS.items():
        root = tmp_path / h
        for rel, text in files.items():
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")
        ingest_paths(s, root, host=h, origin_repo=REPOS[h])
    return s


def _run(retriever, gold, *, scoped: bool, tmp_path, name: str) -> Path:
    out = tmp_path / f"results-{name}.jsonl"
    with out.open("w") as f:
        for g in gold:
            kwargs = {"scope": g["scope"]} if scoped else {}
            res = retriever.retrieve(g["query"], k=5, **kwargs)
            f.write(json.dumps({
                "query_id": g["query_id"],
                "retrieved": [f"{c.host}:{c.source_path}"
                              for c in res.chunks]}) + "\n")
    return out


def test_multihost_goldset_scoped_vs_unscoped(tmp_path):
    store = _build_store(tmp_path)
    retriever = Retriever(store)
    gold = [json.loads(l) for l in
            GOLDSET.read_text(encoding="utf-8").splitlines() if l]
    assert len(gold) >= 15, "plan C2.5: goldset must be ≥ 15 queries"

    scoped_m = score_run(
        _run(retriever, gold, scoped=True, tmp_path=tmp_path,
             name="scoped"), GOLDSET, k=5)
    unscoped_m = score_run(
        _run(retriever, gold, scoped=False, tmp_path=tmp_path,
             name="unscoped"), GOLDSET, k=5)
    assert scoped_m.n == len(gold) == unscoped_m.n

    # Scoped retrieval must dominate overall …
    assert scoped_m.hit_at_k >= unscoped_m.hit_at_k
    # … and show a MEASURABLE hit@5 gap on the distractor class
    # (plan C2.5): the wrong host's five high-density near-duplicates
    # fill the unscoped top-5, pushing the true target out entirely.
    sd_scoped = scoped_m.by_class["scoped_distractor"]
    sd_unscoped = unscoped_m.by_class["scoped_distractor"]
    assert sd_scoped.hit_at_k == 1.0, \
        f"scoped hit@5 should be perfect, got {sd_scoped.hit_at_k}"
    assert sd_scoped.hit_at_k - sd_unscoped.hit_at_k >= 0.5, (
        f"distractor hit@5 gap not measurable: scoped="
        f"{sd_scoped.hit_at_k} unscoped={sd_unscoped.hit_at_k}")


def test_scope_is_consumed_by_runner_not_scorer(tmp_path):
    # The scorer ignores extra goldset fields; scope changes results only
    # through the retrieval runner's host filter.
    store = _build_store(tmp_path)
    retriever = Retriever(store)
    gold = [json.loads(l) for l in
            GOLDSET.read_text(encoding="utf-8").splitlines() if l]
    g = next(x for x in gold if x["query_class"] == "scoped_distractor")
    scoped = retriever.retrieve(g["query"], k=5, scope=g["scope"])
    assert {c.host for c in scoped.chunks} <= set(g["scope"])
    unscoped = retriever.retrieve(g["query"], k=5)
    # Without scope, out-of-scope hosts leak in (here: the trap host
    # dominates the top-5 outright).
    assert {c.host for c in unscoped.chunks} - set(g["scope"])
