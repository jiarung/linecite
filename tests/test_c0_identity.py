"""Phase C0 (cross-host split plan): chunk_uid primary key + host registry.

Acceptance (plan C0.6):
- two host keys with identical repo content coexist (no key collision)
- FTS returns the correct chunk after migration (incl. two-host clones)
- second snapshot from the same host updates; same-name different machine
  is rejected
- unregistered-host writes reject on all three public write paths
- full-suite regression is the rest of the test suite
"""
from __future__ import annotations

import dataclasses
from datetime import datetime

import duckdb
import pytest

from linecite.identity import chunk_id as _cid, chunk_uid as _cuid, content_sha as _csha
from linecite.store import Chunk, ChunkStore


def _mk(text: str, *, host: str, repo: str = "r1", path: str = "docs/a.md",
        ordinal: int = 0) -> Chunk:
    csha = _csha(text)
    return Chunk(
        chunk_id=_cid(path, text), source_path=path, source_type="note",
        text=text, start_line=1, end_line=2,
        embedded_at=datetime(2026, 9, 28), embedding=None,
        raw_text=text, host=host, origin_repo=repo, content_sha=csha,
        chunk_ordinal=ordinal,
        chunk_uid=_cuid(host, repo, path, ordinal, csha),
    )


def _fp(seed: str) -> str:
    import hashlib
    return hashlib.sha256(seed.encode()).hexdigest()[:32]


def _store_with_hosts(*hosts: str) -> ChunkStore:
    """Fresh store whose local identity is hosts[0]; the rest registered
    (distinct fingerprints) with the direct-write escape hatch (C0.5)."""
    s = ChunkStore(dim=4)
    s.init_local_identity(hosts[0])
    for h in hosts[1:]:
        s.register_host(h, _fp(h), allow_direct_writes=True)
    return s


class TestUidPrimaryKey:
    def test_two_hosts_same_content_coexist(self):
        s = _store_with_hosts("h1", "h2")
        text = "identical content on both hosts"
        assert s.upsert_snapshot([_mk(text, host="h1")], host="h1",
                                 origin_repo="r1", sealed=False).inserted == 1
        assert s.upsert_snapshot([_mk(text, host="h2")], host="h2",
                                 origin_repo="r1", sealed=False).inserted == 1
        assert s.count() == 2

    def test_duplicate_uid_insert_rejected_by_pk(self):
        s = _store_with_hosts("h1")
        c = _mk("pk enforced", host="h1")
        s._insert_chunk_row(c)
        with pytest.raises(duckdb.Error):
            s._insert_chunk_row(c)

    def test_fts_returns_both_host_clones(self):
        s = _store_with_hosts("h1", "h2")
        text = "solar panel install guide"
        s.upsert_snapshot([_mk(text, host="h1")], host="h1",
                          origin_repo="r1", sealed=False)
        s.upsert_snapshot([_mk(text, host="h2")], host="h2",
                          origin_repo="r1", sealed=False)
        hits = s.query_fts("solar install", k=5)
        assert {c.host for c, _ in hits} == {"h1", "h2"}

    def test_partition_mismatch_rejected(self):
        s = _store_with_hosts("h1", "h2")
        with pytest.raises(ValueError, match="partition"):
            s.upsert_snapshot([_mk("x", host="h2")], host="h1",
                              origin_repo="r1", sealed=False)


class TestHostRegistry:
    def test_first_write_auto_inits_local_identity(self):
        s = ChunkStore(dim=4)
        assert s.local_identity() is None
        s.write_chunk(_mk("first write", host="h1"))
        ident = s.local_identity()
        assert ident is not None and ident[0] == "h1"
        assert len(ident[1]) == 32 and all(
            ch in "0123456789abcdef" for ch in ident[1])

    def test_local_identity_immutable(self):
        s = _store_with_hosts("h1")
        with pytest.raises(ValueError, match="immutable"):
            s.init_local_identity("other")

    def test_unregistered_host_rejected_on_all_write_paths(self, tmp_path):
        from linecite.generic_ingester import ingest_paths
        s = _store_with_hosts("h1")
        with pytest.raises(ValueError, match="unregistered"):
            s.write_chunk(_mk("via write_chunk", host="ghost"))
        with pytest.raises(ValueError, match="unregistered"):
            s.upsert_snapshot([_mk("via upsert", host="ghost")],
                              host="ghost", origin_repo="r1", sealed=False)
        (tmp_path / "a.md").write_text("# T\nbody\n", encoding="utf-8")
        with pytest.raises(ValueError, match="unregistered"):
            ingest_paths(s, tmp_path, host="ghost", origin_repo="r1")

    def test_registered_without_direct_writes_rejected(self):
        s = _store_with_hosts("h1")
        s.register_host("remote1", "a" * 32)          # import-only
        with pytest.raises(ValueError, match="direct write"):
            s.write_chunk(_mk("nope", host="remote1"))

    def test_escape_hatch_allows_direct_write(self):
        s = _store_with_hosts("h1", "sim1")
        assert s.write_chunk(_mk("simulated", host="sim1")) is True

    def test_same_host_id_different_machine_rejected(self):
        # Acceptance (C0.6): 撞名不同機器報錯 — same host id arriving
        # with a different identity fingerprint.
        s = _store_with_hosts("h1")
        s.register_host("r2", "a" * 32)
        with pytest.raises(ValueError, match="collision"):
            s.register_host("r2", "b" * 32)

    def test_same_fingerprint_second_host_rejected(self):
        # Contract §6 invariant is 1:1 in BOTH directions.
        s = _store_with_hosts("h1")
        s.register_host("r2", "a" * 32)
        with pytest.raises(ValueError, match="fingerprint collision"):
            s.register_host("r3", "a" * 32)

    def test_remote_cannot_claim_local_id(self):
        s = _store_with_hosts("h1")
        with pytest.raises(ValueError, match="local identity"):
            s.register_host("h1", "c" * 32)

    def test_same_host_same_fingerprint_reregister_ok(self):
        s = _store_with_hosts("h1")
        s.register_host("r2", "a" * 32)
        s.register_host("r2", "a" * 32, allow_direct_writes=True)  # no raise

    def test_identity_syntax_validated(self):
        s = ChunkStore(dim=4)
        with pytest.raises(ValueError, match="invalid host id"):
            s.init_local_identity("-bad/host")
        s.init_local_identity("h1")
        with pytest.raises(ValueError, match="fingerprint"):
            s.register_host("r2", "XYZ")

    def test_write_chunk_cross_host_clones_coexist(self):
        # C0 demotes chunk_id to non-unique: identical content on two
        # hosts shares chunk_id but both rows insert (uid dedup).
        s = _store_with_hosts("h1", "h2")
        text = "same bytes everywhere"
        assert s.write_chunk(_mk(text, host="h1")) is True
        assert s.write_chunk(_mk(text, host="h2")) is True
        assert s.write_chunk(_mk(text, host="h2")) is False  # true dup
        assert s.count() == 2

    def test_forged_uid_cannot_touch_other_partition(self):
        # Round-2 review blocker: identity is computed, not declared —
        # host-h1-authorized write carrying h2's chunk_uid must reject.
        s = _store_with_hosts("h1", "h2")
        victim = _mk("victim content", host="h2")
        s.write_chunk(victim)
        forged = dataclasses.replace(_mk("attacker text", host="h1"),
                                     chunk_uid=victim.chunk_uid)
        with pytest.raises(ValueError, match="chunk_uid mismatch"):
            s.write_chunk(forged)
        with pytest.raises(ValueError, match="chunk_uid mismatch"):
            s.upsert_snapshot([forged], host="h1", origin_repo="r1",
                              sealed=False)

    def test_forged_content_sha_rejected(self):
        s = _store_with_hosts("h1")
        bad = dataclasses.replace(_mk("real text", host="h1"),
                                  content_sha="0" * 64)
        with pytest.raises(ValueError, match="content_sha mismatch"):
            s.write_chunk(bad)

    def test_omitted_identity_fields_cannot_bypass_recompute(self):
        # Round-3 blocker: None identity inputs must not skip the uid
        # recompute — a foreign uid with content_sha=None must reject.
        s = _store_with_hosts("h1", "h2")
        victim = _mk("victim two", host="h2")
        s.write_chunk(victim)
        for hole in ({"content_sha": None}, {"chunk_ordinal": None},
                     {"origin_repo": None}):
            forged = dataclasses.replace(_mk("attack", host="h1"),
                                         chunk_uid=victim.chunk_uid, **hole)
            with pytest.raises(ValueError, match="identity fields required"):
                s.write_chunk(forged)
            with pytest.raises(ValueError):
                s.upsert_snapshot([forged], host="h1", origin_repo="r1",
                                  sealed=False)

    def test_host_none_rejected(self):
        s = _store_with_hosts("h1")
        legacy = dataclasses.replace(_mk("x", host="h1"), host=None)
        with pytest.raises(ValueError, match="host is required"):
            s.write_chunk(legacy)


class TestV1Migration:
    def _make_v1_db(self, path, rows):
        con = duckdb.connect(str(path))
        con.execute("""
            CREATE TABLE chunks (
                chunk_id TEXT PRIMARY KEY, source_path TEXT NOT NULL,
                source_type TEXT NOT NULL, text TEXT NOT NULL,
                start_line INTEGER NOT NULL, end_line INTEGER NOT NULL,
                embedded_at TIMESTAMP NOT NULL, embedding FLOAT[4],
                artifact_type TEXT, heading_path TEXT, language TEXT,
                quality_tag TEXT, raw_text TEXT, host TEXT,
                origin_repo TEXT, content_sha TEXT, chunk_ordinal INTEGER,
                chunk_uid TEXT)
        """)
        for r in rows:
            con.execute(
                "INSERT INTO chunks (chunk_id, source_path, source_type, "
                "text, start_line, end_line, embedded_at, host, "
                "origin_repo, content_sha, chunk_ordinal, chunk_uid, "
                "raw_text) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                r)
        con.close()

    def _row(self, text, uid):
        return [_cid("d.md", text), "d.md", "note", text, 1, 2,
                datetime(2026, 1, 1), "h1", "r1", _csha(text), 0, uid, text]

    def test_v1_with_uids_migrates_to_uid_pk(self, tmp_path):
        db = tmp_path / "v1.db"
        self._make_v1_db(db, [self._row("alpha", "u" * 32)])
        s = ChunkStore(db, dim=4)
        assert s._schema_version() == 2
        assert s.count() == 1
        # PK now on chunk_uid: same uid insert must fail
        dup = dataclasses.replace(_mk("alpha", host="h1"),
                                  chunk_uid="u" * 32)
        with pytest.raises(duckdb.Error):
            s._insert_chunk_row(dup)
        got = s.query_fts("alpha", k=1)
        assert got and got[0][0].text == "alpha"
        # FTS doc id is chunk_uid on the MIGRATED table too: a second
        # host's clone of the same content is retrievable alongside.
        s.init_local_identity("h1")   # migrated stores carry no identity
        s.register_host("h2", _fp("h2"), allow_direct_writes=True)
        s.write_chunk(_mk("alpha", host="h2", path="d.md"))
        hosts = {c.host for c, _ in s.query_fts("alpha", k=5)}
        assert hosts == {"h1", "h2"}
        s.close()

    def test_v1_with_null_uid_requires_reingest(self, tmp_path):
        db = tmp_path / "v1null.db"
        self._make_v1_db(db, [self._row("beta", None)])
        with pytest.raises(ValueError, match="re-ingest"):
            ChunkStore(db, dim=4)


class TestSecondSnapshotSameHost:
    def test_same_host_second_snapshot_updates(self):
        s = _store_with_hosts("h1")
        c = _mk("stable text", host="h1")
        s.upsert_snapshot([c], host="h1", origin_repo="r1", sealed=False)
        shifted = dataclasses.replace(c, start_line=3, end_line=4)
        res = s.upsert_snapshot([shifted], host="h1", origin_repo="r1",
                                sealed=False)
        assert res.updated == 1 and res.inserted == 0
