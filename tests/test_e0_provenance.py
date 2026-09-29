"""Phase E0 acceptance (semantic-ladder plan): embedding provenance.

Pairing invariant at the write boundary, model-space isolation in
query_topk (store-level hard rule), model-aware ingest embed-skip,
persisted store dim, explicit reembed as the one vector-overwrite path
(incl. the contract §9 local-metadata exemption over remote rows).
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
from datetime import datetime

import duckdb
import pytest

from linecite.generic_ingester import ingest_paths
from linecite.identity import chunk_id as _cid, chunk_uid as _cuid, content_sha as _csha
from linecite.retrieve import Retriever
from linecite.store import Chunk, ChunkStore
from tests.conftest import det_vec


def _mk(text: str, *, host: str = "h1", path: str = "docs/a.md",
        ordinal: int = 0, embedding=None, model=None) -> Chunk:
    csha = _csha(text)
    return Chunk(
        chunk_id=_cid(path, text), source_path=path, source_type="note",
        text=text, start_line=1, end_line=1,
        embedded_at=datetime(2026, 9, 29), embedding=embedding,
        embedding_model=model, raw_text=text, host=host,
        origin_repo="r1", content_sha=csha, chunk_ordinal=ordinal,
        chunk_uid=_cuid(host, "r1", path, ordinal, csha),
    )


class FakeEmbedder:
    """Embedder-protocol test double (no network)."""

    def __init__(self, model="fake/test-a", dim=4):
        self.model, self.dim, self.calls = model, dim, 0

    def embed(self, texts):
        self.calls += 1
        return [det_vec(f"{self.model}:{t}", self.dim) for t in texts]


class TestPairingInvariant:
    def test_vector_without_model_rejected_all_paths(self):
        s = ChunkStore(dim=4)
        bad = _mk("x", embedding=det_vec("x", 4))     # model None
        with pytest.raises(ValueError, match="pairing"):
            s.write_chunk(bad)
        with pytest.raises(ValueError, match="pairing"):
            s.upsert_snapshot([bad], host="h1", origin_repo="r1",
                              sealed=False)

    def test_model_without_vector_rejected(self):
        s = ChunkStore(dim=4)
        bad = _mk("y", model="fake/test-a")           # vector None
        with pytest.raises(ValueError, match="pairing"):
            s.write_chunk(bad)

    def test_matched_backfill_without_model_rejected(self):
        # Review blocker: the matched-backfill path must hit the same
        # enforcement point — a vector with NULL model must not slip in
        # via upsert of an existing lexical row.
        s = ChunkStore(dim=4)
        lex = _mk("lexical first")
        s.upsert_snapshot([lex], host="h1", origin_repo="r1", sealed=False)
        bad = dataclasses.replace(lex, embedding=det_vec("v", 4))  # no model
        with pytest.raises(ValueError, match="pairing"):
            s.upsert_snapshot([bad], host="h1", origin_repo="r1",
                              sealed=False)
        assert s._con.execute(
            "SELECT embedding IS NULL FROM chunks").fetchone()[0]

    def test_backfill_path_carries_model(self):
        s = ChunkStore(dim=4)
        lex = _mk("stable text")
        s.upsert_snapshot([lex], host="h1", origin_repo="r1", sealed=False)
        emb = dataclasses.replace(lex, embedding=det_vec("stable text", 4),
                                  embedding_model="fake/test-a")
        res = s.upsert_snapshot([emb], host="h1", origin_repo="r1",
                                sealed=False)
        assert res.updated == 1
        got = s._con.execute(
            "SELECT embedding_model FROM chunks WHERE chunk_uid=?",
            [lex.chunk_uid]).fetchone()[0]
        assert got == "fake/test-a"


class TestModelIsolation:
    def _mixed_store(self):
        s = ChunkStore(dim=4)
        s.write_chunk(_mk("apple text", embedding=det_vec("a1", 4),
                          model="fake/test-a"))
        s.write_chunk(_mk("apple text two", path="docs/b.md",
                          embedding=det_vec("b1", 4), model="fake/test-b"))
        # legacy NULL-model vector: inject below the write boundary
        legacy = _mk("apple legacy", path="docs/c.md",
                     embedding=det_vec("c1", 4), model="fake/test-a")
        s.write_chunk(legacy)
        s._con.execute(
            "UPDATE chunks SET embedding_model=NULL WHERE chunk_uid=?",
            [legacy.chunk_uid])
        return s

    def test_store_level_missing_model_hard_rule(self):
        s = self._mixed_store()
        with pytest.raises(ValueError, match="embedding_model"):
            s.query_topk(det_vec("q", 4), k=5)

    def test_model_scoped_query_excludes_other_and_null(self):
        s = self._mixed_store()
        hits = s.query_topk(det_vec("q", 4), k=10,
                            embedding_model="fake/test-a")
        assert {c.embedding_model for c, _ in hits} == {"fake/test-a"}
        assert len(hits) == 1

    def test_allow_mixed_returns_all(self):
        s = self._mixed_store()
        hits = s.query_topk(det_vec("q", 4), k=10,
                            allow_mixed_embedding_models=True)
        assert len(hits) == 3

    def test_retriever_passes_embedder_model(self):
        # mode="vector" pins the pure-vector path (E2 made hybrid the
        # embedder default; the hybrid lexical leg is model-agnostic
        # by design, so this isolation property is vector-leg-scoped).
        s = self._mixed_store()
        emb = FakeEmbedder(model="fake/test-b")
        res = Retriever(s, embedder=emb).retrieve("apple", k=10,
                                                  mode="vector")
        assert {c.embedding_model for c in res.chunks} == {"fake/test-b"}


class TestIngestEmbedding:
    def test_embed_fn_requires_model(self, tmp_path):
        (tmp_path / "a.md").write_text("# A\nbody\n", encoding="utf-8")
        s = ChunkStore(dim=4)
        with pytest.raises(ValueError, match="embedding_model"):
            ingest_paths(s, tmp_path, host="h1", origin_repo="r1",
                         embed_fn=lambda ts: [det_vec(t, 4) for t in ts])

    def test_embedder_object_stamps_model(self, tmp_path):
        (tmp_path / "a.md").write_text("# A\nbody\n", encoding="utf-8")
        s = ChunkStore(dim=4)
        ingest_paths(s, tmp_path, host="h1", origin_repo="r1",
                     embedder=FakeEmbedder())
        models = s._con.execute(
            "SELECT DISTINCT embedding_model FROM chunks").fetchall()
        assert models == [("fake/test-a",)]

    def test_vectored_rows_skipped_on_reingest(self, tmp_path):
        (tmp_path / "a.md").write_text("# A\nbody\n", encoding="utf-8")
        s = ChunkStore(dim=4)
        emb = FakeEmbedder()
        ingest_paths(s, tmp_path, host="h1", origin_repo="r1", embedder=emb)
        calls_after_first = emb.calls
        ingest_paths(s, tmp_path, host="h1", origin_repo="r1", embedder=emb)
        assert emb.calls == calls_after_first   # unchanged rows: no embeds

    def test_mismatched_model_rows_untouched_on_reingest(self, tmp_path):
        # E0.4: rows embedded by model A are NOT re-embedded (and not
        # overwritten) when re-ingesting with model B — explicit reembed
        # is the only reclaim path.
        (tmp_path / "a.md").write_text("# A\nbody\n", encoding="utf-8")
        s = ChunkStore(dim=4)
        ingest_paths(s, tmp_path, host="h1", origin_repo="r1",
                     embedder=FakeEmbedder(model="fake/test-a"))
        emb_b = FakeEmbedder(model="fake/test-b")
        ingest_paths(s, tmp_path, host="h1", origin_repo="r1",
                     embedder=emb_b)
        assert emb_b.calls == 0                    # nothing embedded
        models = s._con.execute(
            "SELECT DISTINCT embedding_model FROM chunks "
            "WHERE embedding IS NOT NULL").fetchall()
        assert models == [("fake/test-a",)]        # untouched
        assert s.reembed(emb_b) > 0                # explicit reclaim
        models = s._con.execute(
            "SELECT DISTINCT embedding_model FROM chunks").fetchall()
        assert models == [("fake/test-b",)]

    def test_legacy_null_model_rows_untouched_on_reingest(self, tmp_path):
        (tmp_path / "a.md").write_text("# A\nbody\n", encoding="utf-8")
        s = ChunkStore(dim=4)
        ingest_paths(s, tmp_path, host="h1", origin_repo="r1",
                     embedder=FakeEmbedder(model="fake/test-a"))
        s._con.execute("UPDATE chunks SET embedding_model = NULL")
        emb = FakeEmbedder(model="fake/test-b")
        ingest_paths(s, tmp_path, host="h1", origin_repo="r1",
                     embedder=emb)
        assert emb.calls == 0                      # quarantined, skipped
        assert s._con.execute(
            "SELECT DISTINCT embedding_model FROM chunks "
            "WHERE embedding IS NOT NULL").fetchall() == [(None,)]

    def test_both_embedder_and_embed_fn_rejected(self, tmp_path):
        (tmp_path / "a.md").write_text("# A\nbody\n", encoding="utf-8")
        s = ChunkStore(dim=4)
        with pytest.raises(ValueError, match="not both"):
            ingest_paths(s, tmp_path, host="h1", origin_repo="r1",
                         embedder=FakeEmbedder(),
                         embed_fn=lambda ts: [])


class TestStoreDim:
    def test_fresh_store_persists_dim_and_mismatch_errors(self, tmp_path):
        db = tmp_path / "d.db"
        ChunkStore(db, dim=8).close()
        with pytest.raises(ValueError, match="dim mismatch"):
            ChunkStore(db, dim=4)
        s = ChunkStore(db, dim=8)     # correct dim reopens fine
        s.close()

    def test_pre_e0_db_infers_dim_from_column(self, tmp_path):
        db = tmp_path / "old.db"
        con = duckdb.connect(str(db))
        con.execute("""
            CREATE TABLE chunks (
                chunk_id TEXT PRIMARY KEY, source_path TEXT NOT NULL,
                source_type TEXT NOT NULL, text TEXT NOT NULL,
                start_line INTEGER NOT NULL, end_line INTEGER NOT NULL,
                embedded_at TIMESTAMP NOT NULL, embedding FLOAT[6])
        """)
        con.close()
        with pytest.raises(ValueError, match="dim mismatch"):
            ChunkStore(db, dim=4)      # inferred 6 != 4, clear error
        s = ChunkStore(db, dim=6)
        assert s._con.execute(
            "SELECT value FROM chunks_meta WHERE key='store_dim'"
        ).fetchone()[0] == 6
        s.close()


class TestReembed:
    def test_reembed_is_the_only_overwrite_path(self):
        s = ChunkStore(dim=4)
        c = _mk("text v", embedding=det_vec("old", 4), model="fake/old")
        s.write_chunk(c)
        # upsert cannot overwrite a stored vector (existing principle)
        again = dataclasses.replace(c, embedding=det_vec("new", 4),
                                    embedding_model="fake/new")
        s.upsert_snapshot([again], host="h1", origin_repo="r1",
                          sealed=False)
        kept = s._con.execute(
            "SELECT embedding_model FROM chunks").fetchone()[0]
        assert kept == "fake/old"
        # reembed overwrites vector + model + embedded_at
        n = s.reembed(FakeEmbedder(model="fake/new"))
        assert n == 1
        got = s._con.execute(
            "SELECT embedding_model FROM chunks").fetchone()[0]
        assert got == "fake/new"

    def test_reembed_dim_mismatch_rejected(self):
        s = ChunkStore(dim=4)
        with pytest.raises(ValueError, match="dim"):
            s.reembed(FakeEmbedder(dim=8))

    def test_reembed_rejects_model_less_embedder(self):
        s = ChunkStore(dim=4)
        s.write_chunk(_mk("row"))
        bad = FakeEmbedder()
        bad.model = None
        with pytest.raises(ValueError, match="non-empty string"):
            s.reembed(bad)

    def test_cli_dim_resolves_from_existing_store(self, tmp_path):
        from linecite.generic_ingester import _resolve_dim
        db = tmp_path / "d.db"
        ChunkStore(db, dim=8).close()
        assert _resolve_dim(str(db), None) == 8      # persisted wins
        assert _resolve_dim(str(db), 4) == 4         # explicit passes thru
        assert _resolve_dim(str(tmp_path / "new.db"), None) == 1536

    def test_reembed_remote_partition_leaves_contract_state_intact(
            self, tmp_path):
        # Contract §9 exemption: reembed may touch imported remote rows
        # but MUST NOT alter contract fields or manifests state.
        from linecite.snapshot import _ROW_FIELDS, export_snapshot, import_snapshot
        src_root = tmp_path / "repo"
        (src_root / "docs").mkdir(parents=True)
        (src_root / "docs/a.md").write_text("# A\nremote body\n",
                                            encoding="utf-8")
        src = ChunkStore(dim=4)
        src.init_local_identity("srchost")
        ingest_paths(src, src_root, host="srchost", origin_repo="r1")
        pkg = export_snapshot(src, "r1", tmp_path / "pkg")
        dst = ChunkStore(dim=4)
        dst.init_local_identity("recv")
        import_snapshot(dst, pkg)

        before_rows = dst._con.execute(
            f"SELECT {', '.join(_ROW_FIELDS)} FROM chunks "
            "ORDER BY chunk_uid").fetchall()
        before_manifests = dst._con.execute(
            "SELECT * FROM manifests ORDER BY host, origin_repo, "
            "direction").fetchall()
        n = dst.reembed(FakeEmbedder(),
                        filters={"host": "srchost"})
        assert n > 0
        after_rows = dst._con.execute(
            f"SELECT {', '.join(_ROW_FIELDS)} FROM chunks "
            "ORDER BY chunk_uid").fetchall()
        after_manifests = dst._con.execute(
            "SELECT * FROM manifests ORDER BY host, origin_repo, "
            "direction").fetchall()
        assert before_rows == after_rows          # contract fields
        assert before_manifests == after_manifests
        assert dst._con.execute(
            "SELECT COUNT(*) FROM chunks WHERE embedding IS NOT NULL "
            "AND embedding_model = 'fake/test-a'").fetchone()[0] == n


class TestLegacyColumnMigration:
    def test_pre_e0_v2_db_gains_column(self, tmp_path):
        # Simulate a schema-v2 DB written BEFORE E0 (no embedding_model
        # column) via raw DDL — DuckDB refuses DROP COLUMN on tables
        # with dependent indexes, so we build the old shape directly.
        db = tmp_path / "v2old.db"
        con = duckdb.connect(str(db))
        con.execute("""
            CREATE TABLE chunks (
                chunk_uid TEXT PRIMARY KEY, chunk_id TEXT NOT NULL,
                source_path TEXT NOT NULL, source_type TEXT NOT NULL,
                text TEXT NOT NULL, start_line INTEGER NOT NULL,
                end_line INTEGER NOT NULL, embedded_at TIMESTAMP NOT NULL,
                embedding FLOAT[4], artifact_type TEXT, heading_path TEXT,
                language TEXT, quality_tag TEXT, raw_text TEXT,
                host TEXT, origin_repo TEXT, content_sha TEXT,
                chunk_ordinal INTEGER)
        """)
        con.execute("CREATE TABLE chunks_meta "
                    "(key TEXT PRIMARY KEY, value BIGINT)")
        con.execute("INSERT INTO chunks_meta VALUES ('fts_gen', 0), "
                    "('schema_version', 2), ('store_dim', 4)")
        con.close()
        s2 = ChunkStore(db, dim=4)    # reopen: ALTER backfill adds col
        cols = {r[0] for r in s2._con.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='chunks'").fetchall()}
        assert "embedding_model" in cols
        s2.close()
