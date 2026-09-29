"""Generic line-window ingester + CLI.

Package-neutral ingest path for the public core: imports core modules only
(store / chunker / retrieve / identity), user-defined source types, no
OpenAI dependency. Acceptance: in a no-key environment (OPENAI_API_KEY
unset + openai unimportable) THIS ingester drives ingest → lexical
retrieve → citation contract end to end.
"""
from __future__ import annotations

import builtins
import sys
from pathlib import Path

import pytest

from linecite.store import ChunkStore


def _toy_repo(root: Path) -> None:
    (root / "docs").mkdir(parents=True, exist_ok=True)
    (root / "docs/alpha.md").write_text(
        "# Alpha\n\n## Usage\nalpha usage line one\nalpha usage line two\n",
        encoding="utf-8")
    (root / "docs/beta.md").write_text(
        "# Beta\n\n## Notes\nbeta notes line one\nbeta notes line two\n",
        encoding="utf-8")


class TestIdentityCore:
    def test_identity_helpers_live_in_core(self):
        from linecite.identity import chunk_id, chunk_uid, content_sha
        csha = content_sha("hello")
        assert len(csha) == 64
        uid = chunk_uid("h", "r", "p.md", 0, csha)
        assert len(uid) == 32
        assert chunk_id("p.md", "hello")



    def test_generic_ingester_imports_core_only(self):
        import linecite.generic_ingester as gi
        src = Path(gi.__file__).read_text(encoding="utf-8")
        pkg = gi.__name__.rsplit(".", 1)[0]
        for line in src.splitlines():
            if line.startswith(("from ", "import ")) and pkg.split(".")[0] in line:
                mod = line.split()[1]
                leaf = mod.rsplit(".", 1)[-1]
                assert leaf in {"chunker", "identity", "store"}, (
                    f"unexpected non-core import: {line}")


class TestGenericIngest:
    def test_ingest_populates_store_with_provenance_and_raw_text(self, tmp_path):
        from linecite.generic_ingester import ingest_paths
        _toy_repo(tmp_path)
        s = ChunkStore(dim=4)
        stats = ingest_paths(s, tmp_path, host="h1", origin_repo="toy")
        assert stats.inserted > 0
        hits = s.query_fts("alpha usage", k=5)
        assert hits
        c = hits[0][0]
        assert c.chunk_uid and c.content_sha and c.raw_text
        assert c.host == "h1" and c.origin_repo == "toy"

    def test_reingest_unchanged_is_noop(self, tmp_path):
        from linecite.generic_ingester import ingest_paths
        _toy_repo(tmp_path)
        s = ChunkStore(dim=4)
        ingest_paths(s, tmp_path, host="h1", origin_repo="toy")
        stats2 = ingest_paths(s, tmp_path, host="h1", origin_repo="toy")
        assert stats2.inserted == 0 and stats2.deleted == 0

    def test_changed_file_reconciles(self, tmp_path):
        from linecite.generic_ingester import ingest_paths
        _toy_repo(tmp_path)
        s = ChunkStore(dim=4)
        ingest_paths(s, tmp_path, host="h1", origin_repo="toy")
        (tmp_path / "docs/alpha.md").write_text(
            "# Alpha\n\n## Usage\ncompletely new alpha content\n",
            encoding="utf-8")
        stats = ingest_paths(s, tmp_path, host="h1", origin_repo="toy")
        assert stats.inserted >= 1 and stats.deleted >= 1
        hits = s.query_fts("completely new alpha", k=5)
        assert hits

    def test_custom_source_type(self, tmp_path):
        from linecite.generic_ingester import ingest_paths
        _toy_repo(tmp_path)
        s = ChunkStore(dim=4)
        ingest_paths(s, tmp_path, host="h1", origin_repo="toy",
                     source_type="recipe")
        c = s.query_fts("beta notes", k=1)[0][0]
        assert c.source_type == "recipe"


class TestSafetyAndScope:
    def test_unreadable_file_downgrades_to_unsealed_no_delete(self, tmp_path):
        # Regression: a read failure must never let
        # the sealed reconciliation mass-delete previously good chunks.
        import os
        from linecite.generic_ingester import ingest_paths
        _toy_repo(tmp_path)
        s = ChunkStore(dim=4)
        ingest_paths(s, tmp_path, host="h1", origin_repo="toy")
        before = s.count()
        bad = tmp_path / "docs/alpha.md"
        os.chmod(bad, 0o000)
        if os.access(bad, os.R_OK):
            pytest.skip("cannot make file unreadable (privileged run)")
        try:
            stats = ingest_paths(s, tmp_path, host="h1", origin_repo="toy")
        finally:
            os.chmod(bad, 0o644)
        assert stats.deleted == 0
        assert s.count() == before

    def test_removed_file_reclaimed_in_full_scope(self, tmp_path):
        # Regression: full-scope scan reclaims chunks
        # of files that no longer exist.
        from linecite.generic_ingester import ingest_paths
        _toy_repo(tmp_path)
        s = ChunkStore(dim=4)
        ingest_paths(s, tmp_path, host="h1", origin_repo="toy")
        (tmp_path / "docs/beta.md").unlink()
        stats = ingest_paths(s, tmp_path, host="h1", origin_repo="toy")
        assert stats.deleted >= 1
        assert not s.query_fts("beta notes", k=5)

    def test_files_scope_preserves_removed_files(self, tmp_path):
        from linecite.generic_ingester import ingest_paths
        _toy_repo(tmp_path)
        s = ChunkStore(dim=4)
        ingest_paths(s, tmp_path, host="h1", origin_repo="toy")
        (tmp_path / "docs/beta.md").unlink()
        stats = ingest_paths(s, tmp_path, host="h1", origin_repo="toy",
                             scope="files")
        assert stats.deleted == 0

    def test_escaping_glob_rejected(self, tmp_path):
        from linecite.generic_ingester import ingest_paths
        _toy_repo(tmp_path)
        s = ChunkStore(dim=4)
        for bad in ("../**/*.md", "/etc/*.conf", "docs/../../*.md"):
            with pytest.raises(ValueError):
                ingest_paths(s, tmp_path, host="h1", origin_repo="toy",
                             globs=[bad])

    def test_embed_fn_only_embeds_new_chunks(self, tmp_path):
        # Regression: matched uids must not re-embed.
        from linecite.generic_ingester import ingest_paths
        calls = []

        def fake_embed(texts):
            calls.append(len(texts))
            return [[0.1, 0.2, 0.3, 0.4] for _ in texts]
        _toy_repo(tmp_path)
        s = ChunkStore(dim=4)
        ingest_paths(s, tmp_path, host="h1", origin_repo="toy",
                     embed_fn=fake_embed, embedding_model="fake/t")
        first_calls = sum(calls)
        assert first_calls > 0
        ingest_paths(s, tmp_path, host="h1", origin_repo="toy",
                     embed_fn=fake_embed, embedding_model="fake/t")
        assert sum(calls) == first_calls  # unchanged corpus → zero embeds

    def test_lexical_rows_backfilled_when_embed_fn_added(self, tmp_path):
        # Regression: a lexical-first corpus
        # later ingested WITH an embedder gets vectors backfilled; stored
        # vectors are never re-embedded.
        from linecite.generic_ingester import ingest_paths
        calls = []

        def fake_embed(texts):
            calls.append(len(texts))
            return [[0.1, 0.2, 0.3, 0.4] for _ in texts]
        _toy_repo(tmp_path)
        s = ChunkStore(dim=4)
        ingest_paths(s, tmp_path, host="h1", origin_repo="toy")  # lexical
        n = s.count()
        stats = ingest_paths(s, tmp_path, host="h1", origin_repo="toy",
                             embed_fn=fake_embed, embedding_model="fake/t")
        assert sum(calls) == n            # every lexical row embedded once
        assert stats.updated == n         # backfill counted as update
        emb = s._con.execute(
            "SELECT COUNT(*) FROM chunks WHERE embedding IS NOT NULL"
        ).fetchone()[0]
        assert emb == n
        # third pass: nothing left to embed
        ingest_paths(s, tmp_path, host="h1", origin_repo="toy",
                     embed_fn=fake_embed, embedding_model="fake/t")
        assert sum(calls) == n

    def test_cli_custom_glob_defaults_to_files_scope(self, tmp_path):
        # Regression: a subset --glob run must
        # not partition-delete files outside its globs.
        from linecite.generic_ingester import main
        _toy_repo(tmp_path)
        db = tmp_path / "kb.duckdb"
        assert main([str(tmp_path), "--db", str(db), "--dim", "4"]) == 0
        # subset scan over a glob matching only alpha.md
        assert main([str(tmp_path), "--db", str(db), "--dim", "4",
                     "--glob", "docs/alpha.md"]) == 0
        s = ChunkStore(db, dim=4)
        assert s.query_fts("beta notes", k=5), "beta chunks must survive"
        s.close()


class TestNoKeyFullChain:
    def test_generic_ingest_retrieve_contract_no_key(self, tmp_path, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        for mod in [m for m in sys.modules if m == "openai"
                    or m.startswith("openai.")]:
            monkeypatch.delitem(sys.modules, mod, raising=False)
        real_import = builtins.__import__

        def deny_openai(name, *a, **kw):
            if name == "openai" or name.startswith("openai."):
                raise ImportError("openai denied")
            return real_import(name, *a, **kw)
        monkeypatch.setattr(builtins, "__import__", deny_openai)

        from linecite.generic_ingester import ingest_paths
        from linecite.retrieve import Retriever
        from linecite.validate_citation import validate_verbatim
        _toy_repo(tmp_path)
        s = ChunkStore(dim=4)
        ingest_paths(s, tmp_path, host="h1", origin_repo="toy")
        res = Retriever(s, embedder=None).retrieve(
            "alpha usage", k=3, verify_root=tmp_path)
        assert res.chunks
        assert res.stale_citations == ()          # nothing drifted
        for c in res.chunks:
            assert validate_verbatim(c, tmp_path).valid


class TestCli:
    def test_cli_ingests_and_reports(self, tmp_path, capsys):
        from linecite.generic_ingester import main
        _toy_repo(tmp_path)
        db = tmp_path / "kb.duckdb"
        rc = main([str(tmp_path), "--db", str(db), "--host", "h1",
                   "--origin-repo", "toy", "--dim", "4"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "inserted" in out
        s = ChunkStore(db, dim=4)
        assert s.count() > 0
        s.close()
