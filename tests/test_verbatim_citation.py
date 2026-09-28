"""Verbatim citation contract.

Every chunk stores raw_text — the exact "\\n".join of source lines
start_line..end_line — alongside the normalized `text` used for
search. `validate_citation.validate_verbatim` compares raw_text
against the live file and MARKS failures (stale / missing / legacy)
instead of crashing.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from linecite.chunker import chunk_text
from linecite.store import Chunk, ChunkStore
from tests.conftest import det_vec

MD = """# Title

## Section One
alpha line one
alpha line two

## Section Two
beta line one
beta line two
"""


def _slice(text: str, start: int, end: int) -> str:
    return "\n".join(text.splitlines()[start - 1:end])


class TestChunkerRawText:
    def test_heading_chunk_raw_text_is_exact_slice(self):
        for tc in chunk_text(MD):
            assert tc.raw_text == _slice(MD, tc.start_line, tc.end_line), (
                f"raw_text must be the verbatim slice for "
                f"L{tc.start_line}-{tc.end_line}")

    def test_window_chunk_raw_text_is_exact_slice(self):
        plain = "\n".join(f"word{i} tok{i} extra{i}" for i in range(40))
        for tc in chunk_text(plain, tokens=30, overlap=5):
            assert tc.raw_text == _slice(plain, tc.start_line, tc.end_line)

    def test_search_text_still_normalized_for_window(self):
        plain = "\n".join(f"word{i}" for i in range(40))
        tcs = chunk_text(plain, tokens=30, overlap=5)
        assert all("\n" not in tc.text for tc in tcs)  # window text joined


def _mk_chunk_from(text: str, path: str, repo: Path, dim: int = 4,
                   pick: str | None = None) -> Chunk:
    from linecite.identity import chunk_id as _chunk_id, chunk_uid as _chunk_uid, content_sha as _content_sha
    (repo / path).parent.mkdir(parents=True, exist_ok=True)
    (repo / path).write_text(text, encoding="utf-8")
    tcs = chunk_text(text)
    tc = (next(x for x in tcs if pick in x.text) if pick else tcs[0])
    csha = _content_sha(tc.text)
    return Chunk(
        chunk_id=_chunk_id(path, tc.text), source_path=path,
        source_type="note", text=tc.text,
        start_line=tc.start_line, end_line=tc.end_line,
        embedded_at=datetime(2026, 9, 24), embedding=None,
        raw_text=tc.raw_text,
        host="h1", origin_repo="r1", content_sha=csha,
        chunk_ordinal=0,
        chunk_uid=_chunk_uid("h1", "r1", path, 0, csha),
    )


class TestStoreRawText:
    def test_raw_text_roundtrips(self, tmp_path):
        s = ChunkStore(dim=4)
        c = _mk_chunk_from(MD, "docs/spec.md", tmp_path, pick="alpha")
        s.write_chunk(c)
        s.write_chunk(_mk_chunk_from(
            "## Other\ngamma delta epsilon\n", "docs/other.md", tmp_path))
        got_pairs = s.query_fts("alpha", k=5)
        got = next(c2 for c2, _ in got_pairs if "alpha" in c2.text)
        assert got.raw_text == c.raw_text


class TestValidator:
    def test_valid_when_file_unchanged(self, tmp_path):
        from linecite.validate_citation import validate_verbatim
        c = _mk_chunk_from(MD, "docs/spec.md", tmp_path)
        r = validate_verbatim(c, tmp_path)
        assert r.valid and r.reason is None

    def test_marks_content_drift_not_crash(self, tmp_path):
        from linecite.validate_citation import validate_verbatim
        c = _mk_chunk_from(MD, "docs/spec.md", tmp_path)
        (tmp_path / "docs/spec.md").write_text(
            "totally rewritten\n" * 20, encoding="utf-8")
        r = validate_verbatim(c, tmp_path)
        assert not r.valid and r.reason == "content_drift"

    def test_marks_missing_file(self, tmp_path):
        from linecite.validate_citation import validate_verbatim
        c = _mk_chunk_from(MD, "docs/spec.md", tmp_path)
        (tmp_path / "docs/spec.md").unlink()
        r = validate_verbatim(c, tmp_path)
        assert not r.valid and r.reason == "source_missing"

    def test_out_of_file_range_is_drift_not_fresh(self, tmp_path):
        # Regression: slice clamping must not let a
        # beyond-EOF range with raw_text == "" validate as clean.
        from linecite.validate_citation import validate_verbatim
        import dataclasses
        c = _mk_chunk_from(MD, "docs/spec.md", tmp_path)
        beyond = dataclasses.replace(c, start_line=999, end_line=1000,
                                     raw_text="")
        r = validate_verbatim(beyond, tmp_path)
        assert not r.valid and r.reason == "content_drift"

    def test_raw_text_reserved_against_extensions(self):
        # Regression: content columns are core.
        with pytest.raises(ValueError):
            ChunkStore(dim=4, extension_columns=[("raw_text", "TEXT")])

    def test_marks_legacy_chunk_without_raw_text(self, tmp_path):
        from linecite.validate_citation import validate_verbatim
        import dataclasses
        c = dataclasses.replace(
            _mk_chunk_from(MD, "docs/spec.md", tmp_path), raw_text=None)
        r = validate_verbatim(c, tmp_path)
        assert not r.valid and r.reason == "no_raw_text"


class TestRetrieverMarksStale:
    def test_verify_root_marks_stale_citations(self, tmp_path):
        from linecite.retrieve import Retriever
        s = ChunkStore(dim=4)
        c = _mk_chunk_from(MD, "docs/spec.md", tmp_path, pick="alpha")
        s.write_chunk(c)
        s.write_chunk(_mk_chunk_from(
            "## Other\ngamma delta epsilon\n", "docs/other.md", tmp_path))
        (tmp_path / "docs/spec.md").write_text("drifted\n", encoding="utf-8")
        res = Retriever(s, embedder=None).retrieve(
            "alpha", k=5, verify_root=tmp_path)
        assert c.chunk_id in res.stale_citations
        # and retrieval itself did not crash / still returned chunks
        assert res.chunks

    def test_no_verify_root_means_no_marking(self, tmp_path):
        from linecite.retrieve import Retriever
        s = ChunkStore(dim=4)
        s.write_chunk(_mk_chunk_from(MD, "docs/spec.md", tmp_path))
        res = Retriever(s, embedder=None).retrieve("alpha", k=5)
        assert res.stale_citations == ()


class TestUpsertRawText:
    def test_matched_upsert_updates_raw_text(self, tmp_path):
        import dataclasses
        s = ChunkStore(dim=4)
        c = _mk_chunk_from(MD, "docs/spec.md", tmp_path, pick="alpha")
        s.write_chunk(_mk_chunk_from(
            "## Other\ngamma delta epsilon\n", "docs/other.md", tmp_path))
        s.upsert_snapshot([c], host="h1", origin_repo="r1", sealed=False)
        shifted = dataclasses.replace(
            c, start_line=c.start_line + 2, end_line=c.end_line + 2,
            raw_text="shifted raw slice")
        res = s.upsert_snapshot([shifted], host="h1", origin_repo="r1",
                                sealed=False)
        assert res.updated == 1
        got, _ = s.query_fts("alpha", k=1)[0]
        assert got.raw_text == "shifted raw slice"


class TestWriteTimeLineContract:
    def test_write_chunk_rejects_invalid_line_range(self, tmp_path):
        import dataclasses
        s = ChunkStore(dim=4)
        c = _mk_chunk_from(MD, "docs/spec.md", tmp_path)
        for bad in [dict(start_line=0), dict(end_line=0),
                    dict(start_line=5, end_line=3)]:
            with pytest.raises(ValueError):
                s.write_chunk(dataclasses.replace(c, **bad))

    def test_upsert_rejects_invalid_line_range_on_insert(self, tmp_path):
        import dataclasses
        s = ChunkStore(dim=4)
        c = dataclasses.replace(
            _mk_chunk_from(MD, "docs/spec.md", tmp_path), start_line=0)
        with pytest.raises(ValueError):
            s.upsert_snapshot([c], host="h1", origin_repo="r1", sealed=False)
