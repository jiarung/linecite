"""Phase C2 acceptance: remote freshness + scoped retrieval API shape.

Plan C2.5: local_host omitted → all prior behavior unchanged (the rest
of the suite is that regression); remote chunk → remote_unverified with
local chunks unaffected; scope filter correctness; citation_checks
per-result records; duplicate marking without silent dedup.
"""
from __future__ import annotations

import dataclasses
from datetime import datetime
from pathlib import Path

import pytest

from linecite.identity import chunk_id as _cid, chunk_uid as _cuid, content_sha as _csha
from linecite.retrieve import Retriever
from linecite.store import Chunk, ChunkStore
from linecite.validate_citation import validate_verbatim


def _mk(text: str, *, host: str, path: str = "docs/a.md",
        ordinal: int = 0, raw_text=None) -> Chunk:
    csha = _csha(text)
    return Chunk(
        chunk_id=_cid(path, text), source_path=path, source_type="note",
        text=text, start_line=1, end_line=1,
        embedded_at=datetime(2026, 9, 28), embedding=None,
        raw_text=text if raw_text is None else raw_text,
        host=host, origin_repo="r1", content_sha=csha,
        chunk_ordinal=ordinal,
        chunk_uid=_cuid(host, "r1", path, ordinal, csha),
    )


def _fp(seed: str) -> str:
    import hashlib
    return hashlib.sha256(seed.encode()).hexdigest()[:32]


class TestValidateRemote:
    def test_remote_chunk_marks_remote_unverified_without_fs(self, tmp_path):
        c = _mk("remote content", host="other")
        # repo_root does not even exist: an attempted read would yield
        # source_missing — remote_unverified proves the early return.
        r = validate_verbatim(c, tmp_path / "nonexistent",
                              local_host="me")
        assert not r.valid and r.reason == "remote_unverified"

    def test_local_chunk_unaffected_by_local_host(self, tmp_path):
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs/a.md").write_text("local content\n",
                                            encoding="utf-8")
        c = _mk("local content", host="me")
        r = validate_verbatim(c, tmp_path, local_host="me")
        assert r.valid and r.reason is None

    def test_legacy_host_none_keeps_local_semantics(self, tmp_path):
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs/a.md").write_text("legacy content\n",
                                            encoding="utf-8")
        c = dataclasses.replace(_mk("legacy content", host="x"), host=None)
        r = validate_verbatim(c, tmp_path, local_host="me")
        assert r.valid    # NOT remote_unverified (contract §9)

    def test_no_local_host_is_prior_behavior(self, tmp_path):
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs/a.md").write_text("shared content\n",
                                            encoding="utf-8")
        c = _mk("shared content", host="other")
        assert validate_verbatim(c, tmp_path).valid   # old semantics


def _dual_host_store():
    s = ChunkStore(dim=4)
    s.init_local_identity("me")
    s.register_host("other", _fp("other"), allow_direct_writes=True)
    return s


class TestRetrieverC2:
    def test_citation_checks_records_and_stale_compat(self, tmp_path):
        s = _dual_host_store()
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs/a.md").write_text("alpha beta gamma\n",
                                            encoding="utf-8")
        local = _mk("alpha beta gamma", host="me")
        remote = _mk("alpha beta delta", host="other", path="docs/b.md")
        s.write_chunk(local)
        s.write_chunk(remote)
        res = Retriever(s).retrieve("alpha beta", k=5,
                                    verify_root=tmp_path, local_host="me")
        # A LIST of per-result records, index-aligned with chunks —
        # one record per result, order preserved (the design forbids a
        # uid-keyed dict because legacy rows may carry chunk_uid None;
        # schema v2 stores cannot hold such rows, so the shape contract
        # is what we can assert here).
        assert isinstance(res.citation_checks, list)
        assert len(res.citation_checks) == len(res.chunks)
        for i, (uid, cid, _reason) in enumerate(res.citation_checks):
            assert uid == res.chunks[i].chunk_uid
            assert cid == res.chunks[i].chunk_id
        by_uid = {u: reason for u, _, reason in res.citation_checks}
        assert by_uid[local.chunk_uid] is None
        assert by_uid[remote.chunk_uid] == "remote_unverified"
        assert remote.chunk_id in res.stale_citations    # deprecated field
        assert local.chunk_id not in res.stale_citations

    def test_scope_filters_hosts(self):
        s = _dual_host_store()
        s.write_chunk(_mk("solar panel text", host="me"))
        s.write_chunk(_mk("solar panel text", host="other",
                          path="docs/b.md"))
        res = Retriever(s).retrieve("solar panel", k=5, scope=["other"])
        assert {c.host for c in res.chunks} == {"other"}

    def test_scope_conflicts_and_empty_rejected(self):
        s = _dual_host_store()
        s.write_chunk(_mk("text body", host="me"))
        r = Retriever(s)
        with pytest.raises(ValueError, match="not both"):
            r.retrieve("text", scope=["me"], filters={"host": "other"})
        with pytest.raises(ValueError, match="non-empty"):
            r.retrieve("text", scope=[])

    def test_duplicate_groups_marked_not_deduped(self):
        s = _dual_host_store()
        text = "identical body on two hosts"
        s.write_chunk(_mk(text, host="me"))
        s.write_chunk(_mk(text, host="other"))
        res = Retriever(s).retrieve("identical body", k=5)
        assert len(res.chunks) == 2            # nothing dropped
        assert len(res.duplicate_groups) == 1
        idxs = res.duplicate_groups[0]
        assert {res.chunks[i].content_sha for i in idxs} == \
            {_csha(text)}
