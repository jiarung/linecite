"""Phase E3 acceptance: the three-line ladder proof (fixture-bound).

Integration-only (local_embeddings marker + cache-only skip): the
numbers come from the real local model over the frozen synthetic
corpus. All assertions are properties of THIS fixture's construction
(plan E3.5), not general retrieval-quality claims.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.test_e1_local_embedder import needs_local, skip_unless_installed

REPO = Path(__file__).resolve().parent.parent


class TestHistoryRowSchema:
    """Always-run (no model): the row serializer carries the hook
    schema fields plus `line`."""

    def test_history_row_fields(self):
        import sys
        sys.path.insert(0, str(REPO / "scripts"))
        import eval_ladder
        from linecite.score import Metrics
        m = Metrics(n=20, hit_at_k=0.4, precision_at_1=0.4, mrr=0.4,
                    ndcg=0.4, k=5)
        m.by_class = {"paraphrase": Metrics(n=12, hit_at_k=0.0, k=5),
                      "exact_term": Metrics(n=8, hit_at_k=1.0, k=5)}
        row = eval_ladder.history_row("2026-09-29T00:00:00Z", "abc123",
                                      "bm25", 5, m)
        for field in ("ts", "parent", "goldset", "line", "k", "n",
                      "hit@5", "p@1", "mrr", "ndcg", "by_class"):
            assert field in row, f"missing {field}"
        assert row["line"] == "bm25" and row["parent"] == "abc123"
        assert row["by_class"] == {"paraphrase": 0.0, "exact_term": 1.0}


@needs_local
@skip_unless_installed
class TestHistoryPathCreation:
    def test_history_parent_dir_created(self, tmp_path):
        # Fresh checkouts have no local/ (gitignored): the default
        # history path's parent must be created, not assumed.
        import sys
        sys.path.insert(0, str(REPO / "scripts"))
        import eval_ladder
        hist = tmp_path / "missing" / "sub" / "history.jsonl"
        assert eval_ladder.main(["--history", str(hist),
                                 "--out-dir", str(tmp_path)]) == 0
        assert hist.exists() and len(hist.read_text().splitlines()) == 3


@needs_local
@skip_unless_installed
class TestLadderProof:
    @pytest.fixture(scope="class")
    @staticmethod
    def metrics(tmp_path_factory):
        import sys
        sys.path.insert(0, str(REPO / "scripts"))
        import eval_ladder
        from linecite.retrieve import Retriever
        from linecite.score import score_run
        out = tmp_path_factory.mktemp("ladder")
        gold = [json.loads(l) for l in
                eval_ladder.GOLDSET.read_text("utf-8").splitlines() if l]
        store, emb = eval_ladder.build_store()
        r = Retriever(store, embedder=emb)
        m = {}
        for line in eval_ladder.LINES:
            res = eval_ladder.run_line(r, gold, line, 5, out)
            m[line] = score_run(res, eval_ladder.GOLDSET, k=5)
        return m

    def test_goldset_composition_frozen(self, metrics):
        m = metrics["bm25"]
        assert m.n == 20
        assert m.by_class["paraphrase"].n == 12
        assert m.by_class["exact_term"].n == 8

    def test_history_rows_carry_hook_schema_plus_line(self, tmp_path):
        import sys
        sys.path.insert(0, str(REPO / "scripts"))
        import eval_ladder
        hist = tmp_path / "history.jsonl"
        assert eval_ladder.main(["--history", str(hist),
                                 "--out-dir", str(tmp_path)]) == 0
        rows = [json.loads(l) for l in
                hist.read_text().splitlines() if l]
        assert len(rows) == 3
        for r in rows:
            for field in ("ts", "parent", "goldset", "line", "k", "n",
                          "hit@5", "p@1", "mrr", "ndcg", "by_class"):
                assert field in r, f"missing {field}"
        assert {r["line"] for r in rows} == \
            {"bm25", "local-embed", "hybrid"}

    def test_paraphrase_gap_is_by_construction(self, metrics):
        # bm25 was per-item verified to miss every paraphrase target
        # before freezing (scripts/eval_ladder.py --verify-construction)
        bm25 = metrics["bm25"].by_class["paraphrase"].hit_at_k
        embed = metrics["local-embed"].by_class["paraphrase"].hit_at_k
        hybrid = metrics["hybrid"].by_class["paraphrase"].hit_at_k
        assert embed - bm25 >= 0.5
        assert hybrid - bm25 >= 0.5

    def test_exact_term_no_regression(self, metrics):
        bm25 = metrics["bm25"].by_class["exact_term"].hit_at_k
        hybrid = metrics["hybrid"].by_class["exact_term"].hit_at_k
        assert hybrid >= bm25 - 0.1     # bounded regression, no theorem

    def test_readme_table_matches_measured_numbers(self, metrics):
        readme = (REPO / "README.md").read_text("utf-8")
        for line in ("bm25", "local-embed", "hybrid"):
            m = metrics[line]
            row = (f"| {line} | {m.hit_at_k:.3f} | "
                   f"{m.by_class['paraphrase'].hit_at_k:.3f} | "
                   f"{m.by_class['exact_term'].hit_at_k:.3f} |")
            assert row in readme, f"README ladder row stale: {row}"
