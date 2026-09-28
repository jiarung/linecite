"""Standalone scorer: pure-stdlib retrieval metrics over JSONL inputs.

Inputs are decoupled from any store or embedder:
- results JSONL:  {"query_id": ..., "retrieved": ["path-or-id", ...]}
- goldset JSONL:  {"query_id": ..., "expected": ["path-or-id", ...],
                   "query_class": "..."}
A retrieved item matches when it equals an expected item or starts with
it (prefix match — expected entries may be path prefixes).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from linecite.score import Metrics, score_run


def _write_jsonl(path: Path, rows: list[dict]) -> Path:
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n",
                    encoding="utf-8")
    return path


GOLD = [
    {"query_id": "q1", "expected": ["docs/a.md"], "query_class": "lookup"},
    {"query_id": "q2", "expected": ["docs/b.md"], "query_class": "lookup"},
    {"query_id": "q3", "expected": ["src/"], "query_class": "code"},
]
RES = [
    {"query_id": "q1", "retrieved": ["docs/a.md", "docs/x.md"]},   # hit@1
    {"query_id": "q2", "retrieved": ["docs/x.md", "docs/b.md"]},   # hit@2
    {"query_id": "q3", "retrieved": ["docs/x.md", "docs/y.md"]},   # miss
]


class TestMetrics:
    def test_basic_metrics(self, tmp_path):
        m = score_run(_write_jsonl(tmp_path / "r.jsonl", RES),
                      _write_jsonl(tmp_path / "g.jsonl", GOLD), k=5)
        assert isinstance(m, Metrics)
        assert m.n == 3
        assert m.hit_at_k == pytest.approx(2 / 3)
        assert m.precision_at_1 == pytest.approx(1 / 3)
        assert m.mrr == pytest.approx((1.0 + 0.5 + 0.0) / 3)
        assert 0 < m.ndcg < 1

    def test_prefix_match(self, tmp_path):
        res = [{"query_id": "q3", "retrieved": ["src/linecite/store.py"]}]
        gold = [GOLD[2]]
        m = score_run(_write_jsonl(tmp_path / "r.jsonl", res),
                      _write_jsonl(tmp_path / "g.jsonl", gold), k=5)
        assert m.hit_at_k == 1.0 and m.precision_at_1 == 1.0

    def test_class_slices(self, tmp_path):
        m = score_run(_write_jsonl(tmp_path / "r.jsonl", RES),
                      _write_jsonl(tmp_path / "g.jsonl", GOLD), k=5)
        assert set(m.by_class) == {"lookup", "code"}
        assert m.by_class["lookup"].n == 2
        assert m.by_class["lookup"].hit_at_k == pytest.approx(1.0)
        assert m.by_class["code"].hit_at_k == 0.0

    def test_bootstrap_ci_brackets_point_estimate(self, tmp_path):
        m = score_run(_write_jsonl(tmp_path / "r.jsonl", RES),
                      _write_jsonl(tmp_path / "g.jsonl", GOLD), k=5,
                      bootstrap=500, seed=7)
        lo, hi = m.hit_ci
        assert 0.0 <= lo <= m.hit_at_k <= hi <= 1.0

    def test_missing_result_row_counts_as_miss(self, tmp_path):
        m = score_run(_write_jsonl(tmp_path / "r.jsonl", RES[:2]),
                      _write_jsonl(tmp_path / "g.jsonl", GOLD), k=5)
        assert m.n == 3
        assert m.hit_at_k == pytest.approx(2 / 3)

    def test_k_truncates_retrieved(self, tmp_path):
        res = [{"query_id": "q1",
                "retrieved": ["docs/x.md", "docs/y.md", "docs/a.md"]}]
        m = score_run(_write_jsonl(tmp_path / "r.jsonl", res),
                      _write_jsonl(tmp_path / "g.jsonl", [GOLD[0]]), k=2)
        assert m.hit_at_k == 0.0


class TestCli:
    def test_cli_reports_metrics(self, tmp_path, capsys):
        from linecite.score import main
        r = _write_jsonl(tmp_path / "r.jsonl", RES)
        g = _write_jsonl(tmp_path / "g.jsonl", GOLD)
        rc = main([str(r), str(g), "--k", "5"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "hit@5" in out and "MRR" in out and "lookup" in out


class TestNdcgBounds:
    def test_duplicate_prefix_hits_do_not_inflate_ndcg(self, tmp_path):
        # Regression: 5 chunks from the same expected file must not push
        # nDCG above 1 (each expected entry credits once).
        res = [{"query_id": "q3",
                "retrieved": [f"src/f{i}.py" for i in range(5)]}]
        gold = [{"query_id": "q3", "expected": ["src/"],
                 "query_class": "code"}]
        m = score_run(_write_jsonl(tmp_path / "r.jsonl", res),
                      _write_jsonl(tmp_path / "g.jsonl", gold), k=5)
        assert m.ndcg <= 1.0
        assert m.ndcg == pytest.approx(1.0)  # first hit at rank 1, capacity 1

    def test_multi_expected_ndcg_uses_gold_capacity(self, tmp_path):
        res = [{"query_id": "q", "retrieved": ["a.md", "x.md", "b.md"]}]
        gold = [{"query_id": "q", "expected": ["a.md", "b.md"],
                 "query_class": "c"}]
        m = score_run(_write_jsonl(tmp_path / "r.jsonl", res),
                      _write_jsonl(tmp_path / "g.jsonl", gold), k=3)
        import math
        dcg = 1.0 + 1.0 / math.log2(4)
        idcg = 1.0 + 1.0 / math.log2(3)
        assert m.ndcg == pytest.approx(dcg / idcg)
        assert m.ndcg <= 1.0

    def test_small_bootstrap_counts_ok(self, tmp_path):
        for n in (1, 2, 3):
            m = score_run(_write_jsonl(tmp_path / "r.jsonl", RES),
                          _write_jsonl(tmp_path / "g.jsonl", GOLD), k=5,
                          bootstrap=n, seed=1)
            lo, hi = m.hit_ci
            assert 0.0 <= lo <= hi <= 1.0

    def test_invalid_k_and_bootstrap_rejected(self, tmp_path):
        r = _write_jsonl(tmp_path / "r.jsonl", RES)
        g = _write_jsonl(tmp_path / "g.jsonl", GOLD)
        with pytest.raises(ValueError):
            score_run(r, g, k=0)
        with pytest.raises(ValueError):
            score_run(r, g, k=5, bootstrap=-1)
