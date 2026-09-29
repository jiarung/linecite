"""Store schema generalization.

Contract under test:

1. deployment-specific ontology is NOT part of the public core API —
   neither as Chunk dataclass fields nor as always-allowed filter
   keys. Domain fields live in the *extension metadata* mechanism.
2. SourceType is user-defined (any string), not a Literal enum.
3. Extensions are registered per-store via `extension_columns`;
   registered keys round-trip through write/query and are filterable.
"""
from __future__ import annotations

import dataclasses
from datetime import datetime

import pytest

from linecite.store import Chunk, ChunkStore

from tests.conftest import det_vec


def _mk_chunk(text: str, *, source_type: str = "note", ext: dict | None = None,
              ordinal: int = 0, path: str = "docs/a.md", dim: int = 4) -> Chunk:
    from linecite.identity import chunk_id as _chunk_id, chunk_uid as _chunk_uid, content_sha as _content_sha
    csha = _content_sha(text)
    return Chunk(
        chunk_id=_chunk_id(path, text),
        source_path=path,
        source_type=source_type,
        text=text,
        start_line=1,
        end_line=2,
        embedded_at=datetime(2026, 9, 24),
        embedding=det_vec(text, dim),
        embedding_model="det-test",
        host="h1",
        origin_repo="r1",
        content_sha=csha,
        chunk_ordinal=ordinal,
        chunk_uid=_chunk_uid("h1", "r1", path, ordinal, csha),
        ext=ext,
    )


class TestGenericCore:
    def test_chunk_has_no_domain_fields(self):
        names = {f.name for f in dataclasses.fields(Chunk)}
        assert "project_id" not in names
        assert "review_stage" not in names
        assert "outcome" not in names
        assert "ext" in names

    def test_source_type_is_user_defined(self):
        s = ChunkStore(dim=4)
        c = _mk_chunk("hello world", source_type="recipe_doc")
        assert s.write_chunk(c) is True
        got, _ = s.query_topk(det_vec("hello world", 4), k=1, embedding_model="det-test")[0]
        assert got.source_type == "recipe_doc"

    def test_unregistered_ext_filter_rejected(self):
        s = ChunkStore(dim=4)
        s.write_chunk(_mk_chunk("x"))
        with pytest.raises(ValueError):
            s.query_topk(det_vec("x", 4), k=1, filters={"project_id": "X"}, embedding_model="det-test")

    def test_registered_extension_roundtrip_and_filter(self):
        s = ChunkStore(dim=4, extension_columns=[
            ("project_id", "TEXT"), ("outcome", "TEXT")])
        a = _mk_chunk("alpha text", ext={"project_id": "sA", "outcome": "fail"},
                      ordinal=0)
        b = _mk_chunk("beta text", ext={"project_id": "sB", "outcome": "open"},
                      ordinal=1, path="docs/b.md")
        assert s.write_chunk(a) and s.write_chunk(b)
        hits = s.query_topk(det_vec("alpha text", 4), k=10,
                            filters={"project_id": "sA"}, embedding_model="det-test")
        assert len(hits) == 1
        got = hits[0][0]
        assert got.ext == {"project_id": "sA", "outcome": "fail"}

    def test_extension_absent_reads_as_none_values(self):
        s = ChunkStore(dim=4, extension_columns=[("project_id", "TEXT")])
        s.write_chunk(_mk_chunk("no ext set"))
        got, _ = s.query_topk(det_vec("no ext set", 4), k=1, embedding_model="det-test")[0]
        assert got.ext in (None, {}, {"project_id": None})

    def test_ext_ignored_when_not_registered(self):
        # Core store without registration: ext payload is not persisted
        # as filterable state, and does not crash the insert.
        s = ChunkStore(dim=4)
        s.write_chunk(_mk_chunk("stray ext", ext={"project_id": "sX"}))
        got, _ = s.query_topk(det_vec("stray ext", 4), k=1, embedding_model="det-test")[0]
        assert not (got.ext or {}).get("project_id")


class TestExtensionValidation:
    def test_invalid_extension_name_rejected(self):
        with pytest.raises(ValueError):
            ChunkStore(dim=4, extension_columns=[("bad-name;drop", "TEXT")])

    def test_invalid_extension_type_rejected(self):
        with pytest.raises(ValueError):
            ChunkStore(dim=4, extension_columns=[("ok_name", "BLOB)")])

    def test_duplicate_extension_rejected_case_insensitive(self):
        with pytest.raises(ValueError):
            ChunkStore(dim=4, extension_columns=[
                ("dup_col", "TEXT"), ("DUP_col", "TEXT")])

    def test_core_shadow_rejected(self):
        with pytest.raises(ValueError):
            ChunkStore(dim=4, extension_columns=[("source_path", "TEXT")])

    def test_core_shadow_rejected_case_insensitive(self):
        for bad in ("Source_Path", "CHUNK_ID"):
            with pytest.raises(ValueError):
                ChunkStore(dim=4, extension_columns=[(bad, "TEXT")])


class TestUpsertExtension:
    def test_matched_upsert_updates_extension_values(self):
        # Regression: a matched chunk_uid must persist
        # new ext values, not keep stale/NULL ones.
        s = ChunkStore(dim=4, extension_columns=[("project_id", "TEXT")])
        a = _mk_chunk("stable text")            # no ext
        s.upsert_snapshot([a], host="h1", origin_repo="r1", sealed=False)
        a2 = _mk_chunk("stable text", ext={"project_id": "s1"})
        res = s.upsert_snapshot([a2], host="h1", origin_repo="r1",
                                sealed=False)
        assert res.updated == 1 and res.inserted == 0
        got, _ = s.query_topk(det_vec("stable text", 4), k=1, embedding_model="det-test")[0]
        assert got.ext == {"project_id": "s1"}

    def test_matched_upsert_same_ext_is_unchanged(self):
        s = ChunkStore(dim=4, extension_columns=[("project_id", "TEXT")])
        a = _mk_chunk("same ext", ext={"project_id": "s1"})
        s.upsert_snapshot([a], host="h1", origin_repo="r1", sealed=False)
        res = s.upsert_snapshot([a], host="h1", origin_repo="r1",
                                sealed=False)
        assert res.unchanged == 1 and res.updated == 0




class TestNoDomainDefaultsInCore:
    def test_backfill_requires_explicit_provenance(self):
        # Core must not silently default host/origin_repo to a domain value.
        s = ChunkStore(dim=4)
        with pytest.raises(TypeError):
            s._backfill_legacy_provenance()
