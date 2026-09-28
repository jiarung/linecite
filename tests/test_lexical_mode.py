"""Lexical no-key mode.

No-key acceptance: with the `openai` package unimportable and zero
embedding calls, the core supports ingest (write) → retrieve (FTS) →
citation contract end to end.

Also covers: embedding column nullable (incl. migration of a legacy
NOT NULL table), lazy VSS (vector machinery only touched on vector
paths), and the strict require_embedding mode.
"""
from __future__ import annotations

import builtins
import sys
from datetime import datetime

import duckdb
import pytest

from linecite.store import Chunk, ChunkStore
from tests.conftest import det_vec


def _mk(text: str, *, path: str = "docs/a.md", ordinal: int = 0,
        embedding=None, start: int = 1, end: int = 2) -> Chunk:
    from linecite.identity import chunk_id as _chunk_id, chunk_uid as _chunk_uid, content_sha as _content_sha
    csha = _content_sha(text)
    return Chunk(
        chunk_id=_chunk_id(path, text),
        source_path=path,
        source_type="note",
        text=text,
        start_line=start,
        end_line=end,
        embedded_at=datetime(2026, 9, 24),
        embedding=embedding,
        host="h1",
        origin_repo="r1",
        content_sha=csha,
        chunk_ordinal=ordinal,
        chunk_uid=_chunk_uid("h1", "r1", path, ordinal, csha),
    )


class TestNullableEmbedding:
    def test_write_chunk_without_embedding(self):
        s = ChunkStore(dim=4)
        assert s.write_chunk(_mk("no vector here")) is True
        assert s.count() == 1

    def test_wrong_length_embedding_still_rejected(self):
        s = ChunkStore(dim=4)
        with pytest.raises(ValueError):
            s.write_chunk(_mk("bad vec", embedding=[0.1] * 3))

    def test_upsert_insert_without_embedding(self):
        s = ChunkStore(dim=4)
        res = s.upsert_snapshot([_mk("lexical only")], host="h1",
                                origin_repo="r1", sealed=False)
        assert res.inserted == 1

    def test_legacy_not_null_db_migrates(self, tmp_path):
        # Simulate an old DB whose embedding column is NOT NULL.
        db = tmp_path / "legacy.db"
        con = duckdb.connect(str(db))
        con.execute("""
            CREATE TABLE chunks (
                chunk_id TEXT PRIMARY KEY, source_path TEXT NOT NULL,
                source_type TEXT NOT NULL, text TEXT NOT NULL,
                start_line INTEGER NOT NULL, end_line INTEGER NOT NULL,
                embedded_at TIMESTAMP NOT NULL, embedding FLOAT[4] NOT NULL)
        """)
        con.close()
        s = ChunkStore(db, dim=4)
        assert s.write_chunk(_mk("post-migration lexical")) is True


class TestFtsQuery:
    def test_query_fts_matches_and_scores(self):
        s = ChunkStore(dim=4)
        s.write_chunk(_mk("solar panel install steps", path="d/m.md"))
        s.write_chunk(_mk("warranty return policy text", path="d/r.md",
                          ordinal=1))
        hits = s.query_fts("solar install", k=5)
        assert hits, "FTS should match the solar chunk"
        top, score = hits[0]
        assert top.source_path == "d/m.md"
        assert score > 0

    def test_query_fts_respects_filters(self):
        s = ChunkStore(dim=4, extension_columns=[("team", "TEXT")])
        s.write_chunk(_mk("shared vocabulary alpha", path="d/1.md"))
        import dataclasses
        b = dataclasses.replace(
            _mk("shared vocabulary beta", path="d/2.md", ordinal=1),
            ext={"team": "core"})
        s.write_chunk(b)
        hits = s.query_fts("shared vocabulary", k=5,
                           filters={"team": "core"})
        assert [c.source_path for c, _ in hits] == ["d/2.md"]

    def test_query_fts_sees_new_writes(self):
        # (Non-degenerate corpus: BM25 gives a term present in every doc
        # zero information, so seed two docs before asserting matches.)
        s = ChunkStore(dim=4)
        s.write_chunk(_mk("alpha document text", path="d/1.md"))
        s.write_chunk(_mk("bravo document text", path="d/2.md", ordinal=1))
        assert any(c.source_path == "d/1.md"
                   for c, _ in s.query_fts("alpha", k=5))
        # Index must refresh after a later write.
        s.write_chunk(_mk("charlie document text", path="d/3.md", ordinal=2))
        hits = s.query_fts("charlie", k=5)
        assert any(c.source_path == "d/3.md" for c, _ in hits)


class TestMixedStore:
    def test_query_topk_skips_lexical_only_rows(self):
        # Regression: NULL-embedding rows must not
        # reach the vector scorer even when k exceeds embedded rows.
        s = ChunkStore(dim=4)
        s.write_chunk(_mk("embedded row", embedding=det_vec("embedded", 4)))
        s.write_chunk(_mk("lexical only row", path="d/lex.md", ordinal=1))
        hits = s.query_topk(det_vec("embedded", 4), k=5)
        assert len(hits) == 1
        assert hits[0][0].embedding is not None

    def test_fts_fresh_across_instances(self, tmp_path):
        # Regression: FTS staleness is DB-persisted,
        # so a reopened store rebuilds against the latest writes.
        db = tmp_path / "x.db"
        s1 = ChunkStore(db, dim=4)
        s1.write_chunk(_mk("alpha doc", path="d/1.md"))
        s1.write_chunk(_mk("bravo doc", path="d/2.md", ordinal=1))
        assert s1.query_fts("alpha", k=5)
        s1.close()
        s2 = ChunkStore(db, dim=4)
        s2.write_chunk(_mk("charlie doc", path="d/3.md", ordinal=2))
        hits = s2.query_fts("charlie", k=5)
        assert any(c.source_path == "d/3.md" for c, _ in hits)
        s2.close()


class TestNoKeyEndToEnd:
    def test_ingest_retrieve_contract_without_openai(self, monkeypatch):
        # Gate (a): OPENAI_API_KEY unset AND `openai` unimportable AND zero
        # embedding calls → write → lexical retrieve → contract all green.
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        for mod in [m for m in sys.modules if m == "openai"
                    or m.startswith("openai.")]:
            monkeypatch.delitem(sys.modules, mod, raising=False)
        real_import = builtins.__import__

        def deny_openai(name, *a, **kw):
            if name == "openai" or name.startswith("openai."):
                raise ImportError("openai denied by no-key acceptance test")
            return real_import(name, *a, **kw)
        monkeypatch.setattr(builtins, "__import__", deny_openai)

        from linecite.retrieve import Retriever
        s = ChunkStore(dim=4)
        s.write_chunk(_mk("verbatim line anchored citation contract",
                          path="docs/spec.md", start=3, end=9))
        r = Retriever(s, embedder=None)     # lexical mode
        res = r.retrieve("citation contract", k=3)
        assert res.chunks, "lexical retrieve must return the chunk"
        assert res.chunks[0].source_path == "docs/spec.md"
        assert res.chunks[0].start_line == 3
        assert res.reranker_used is False

    def test_retriever_without_embedder_rejects_nothing_extra(self):
        from linecite.retrieve import Retriever
        s = ChunkStore(dim=4)
        s.write_chunk(_mk("alpha beta gamma"))
        res = Retriever(s, embedder=None).retrieve("alpha", k=2)
        assert len(res.chunks) >= 1


class TestRequireEmbedding:
    def test_strict_store_rejects_missing_embedding_on_insert(self):
        s = ChunkStore(dim=4, require_embedding=True)
        with pytest.raises(ValueError):
            s.write_chunk(_mk("must embed"))

    def test_strict_store_accepts_embedded_chunk(self):
        s = ChunkStore(dim=4, require_embedding=True)
        assert s.write_chunk(_mk("ok", embedding=det_vec("ok", 4))) is True


