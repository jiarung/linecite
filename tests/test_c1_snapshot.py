"""Phase C1/C1.5 acceptance: snapshot export/import (contract §2-§8).

Covers plan C1.4: contract-field roundtrip (heading_path included,
embedding/embedded_at excluded), matched-import metadata semantics,
six reject scenarios, no-partial-mutation, determinism, replay matrix,
reconciliation deletes + empty-snapshot guard; and C1.5.3: deny hook.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from linecite.snapshot import (_ROW_FIELDS, _canonical_json, _sha256,
                               DEFAULT_DENY, SnapshotError,
                               export_snapshot, import_snapshot)
from linecite.store import ChunkStore
from linecite.generic_ingester import ingest_paths

FROZEN = datetime(2026, 9, 28, 12, 0, 0, tzinfo=timezone.utc)


def _repo(tmp_path, name="repo", files=None):
    root = tmp_path / name
    root.mkdir(exist_ok=True)
    files = files or {"docs/a.md": "# Alpha\nalpha body line\n",
                      "docs/b.md": "# Beta\nbeta body line\n"}
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return root


def _exporter(tmp_path, host="h1", repo_name="repo", files=None):
    root = _repo(tmp_path, repo_name, files)
    s = ChunkStore(dim=4)
    s.init_local_identity(host)
    ingest_paths(s, root, host=host, origin_repo="r1")
    return s, root


def _importer(host="recv"):
    s = ChunkStore(dim=4)
    s.init_local_identity(host)
    return s


def _partition_rows(store, host, repo):
    return store._con.execute(
        f"SELECT {', '.join(_ROW_FIELDS)} FROM chunks "
        "WHERE host = ? AND origin_repo = ? ORDER BY chunk_uid",
        [host, repo]).fetchall()


def _rewrite(pkg: Path, *, mutate_rows=None, mutate_manifest=None,
             recompute=True):
    """Craft a tampered/adjusted package with (optionally) valid digests."""
    rows = [json.loads(l) for l in
            (pkg / "chunks.jsonl").read_bytes().splitlines() if l]
    manifest = json.loads((pkg / "manifest.json").read_bytes())
    if mutate_rows:
        rows = mutate_rows(rows)
    chunks_bytes = b"".join(_canonical_json(r) + b"\n" for r in rows)
    if recompute:
        manifest["chunk_count"] = len(rows)
        manifest["content_digest"] = _sha256(chunks_bytes)
    if mutate_manifest:
        mutate_manifest(manifest)
    if recompute:
        probe = {k: v for k, v in manifest.items() if k != "manifest_digest"}
        manifest["manifest_digest"] = _sha256(
            chunks_bytes + _canonical_json(probe))
    (pkg / "chunks.jsonl").write_bytes(chunks_bytes)
    (pkg / "manifest.json").write_bytes(_canonical_json(manifest))
    return pkg


class TestRoundtrip:
    def test_contract_fields_equal_including_heading_path(self, tmp_path):
        src, _ = _exporter(tmp_path)
        pkg = export_snapshot(src, "r1", tmp_path / "pkg")
        dst = _importer()
        res = import_snapshot(dst, pkg)
        assert res.status == "accepted" and res.reconcile.inserted > 0
        assert _partition_rows(src, "h1", "r1") == \
            _partition_rows(dst, "h1", "r1")
        assert any(r[_ROW_FIELDS.index("heading_path")]
                   for r in _partition_rows(dst, "h1", "r1"))

    def test_matched_import_overwrites_all_contract_fields(self, tmp_path):
        src, _ = _exporter(tmp_path)
        # Stamp metadata on the source rows, re-export, import twice.
        src._con.execute(
            "UPDATE chunks SET quality_tag='verified', language='en', "
            "artifact_type='doc' WHERE host='h1'")
        pkg = export_snapshot(src, "r1", tmp_path / "p1")
        dst = _importer()
        import_snapshot(dst, pkg)
        got = dst._con.execute(
            "SELECT DISTINCT quality_tag, language, artifact_type FROM "
            "chunks WHERE host='h1'").fetchall()
        assert got == [("verified", "en", "doc")]
        # Snapshot with NULL metadata is authoritative: NULL overwrites.
        src._con.execute(
            "UPDATE chunks SET quality_tag=NULL, language=NULL, "
            "artifact_type=NULL WHERE host='h1'")
        pkg2 = export_snapshot(src, "r1", tmp_path / "p2")
        res = import_snapshot(dst, pkg2)
        assert res.status == "accepted"
        got = dst._con.execute(
            "SELECT DISTINCT quality_tag, language, artifact_type FROM "
            "chunks WHERE host='h1'").fetchall()
        assert got == [(None, None, None)]


class TestRejects:
    def _fresh(self, tmp_path):
        src, _ = _exporter(tmp_path)
        pkg = export_snapshot(src, "r1", tmp_path / "pkg")
        return src, pkg, _importer()

    def _assert_untouched(self, dst):
        assert dst.count() == 0
        assert dst._con.execute(
            "SELECT COUNT(*) FROM manifests").fetchone()[0] == 0

    def test_truncated_chunks_rejected(self, tmp_path):
        _, pkg, dst = self._fresh(tmp_path)
        data = (pkg / "chunks.jsonl").read_bytes().splitlines(True)
        (pkg / "chunks.jsonl").write_bytes(b"".join(data[:-1]))
        with pytest.raises(SnapshotError) as e:
            import_snapshot(dst, pkg)
        assert e.value.code == "package_integrity"
        self._assert_untouched(dst)

    def test_tampered_manifest_rejected(self, tmp_path):
        _, pkg, dst = self._fresh(tmp_path)
        m = json.loads((pkg / "manifest.json").read_bytes())
        m["origin_repo"] = "evil"          # digest not recomputed
        (pkg / "manifest.json").write_bytes(_canonical_json(m))
        with pytest.raises(SnapshotError) as e:
            import_snapshot(dst, pkg)
        assert e.value.code == "package_integrity"
        self._assert_untouched(dst)

    def test_self_import_rejected(self, tmp_path):
        src, pkg, _ = self._fresh(tmp_path)
        with pytest.raises(SnapshotError) as e:
            import_snapshot(src, pkg)
        assert e.value.code == "self_import"

    def test_same_host_id_different_machine_rejected(self, tmp_path):
        _, pkg, dst = self._fresh(tmp_path)
        import_snapshot(dst, pkg)
        clone, _ = _exporter(tmp_path, host="h1", repo_name="repo2",
                             files={"docs/c.md": "# C\nzzz\n"})
        pkg2 = export_snapshot(clone, "r1", tmp_path / "pkg2")
        with pytest.raises(SnapshotError) as e:
            import_snapshot(dst, pkg2)
        assert e.value.code == "host_collision"

    def test_row_identity_tamper_rejected(self, tmp_path):
        _, pkg, dst = self._fresh(tmp_path)

        def tamper(rows):
            rows[0]["text"] = rows[0]["text"] + " EVIL"
            return rows                      # content_sha now stale
        _rewrite(pkg, mutate_rows=tamper)
        with pytest.raises(SnapshotError) as e:
            import_snapshot(dst, pkg)
        assert e.value.code == "row_identity"
        self._assert_untouched(dst)

    def test_mixed_partition_row_rejected(self, tmp_path):
        _, pkg, dst = self._fresh(tmp_path)

        def cross(rows):
            from linecite.identity import chunk_uid as _cuid
            r = rows[0]
            r["origin_repo"] = "other"
            r["chunk_uid"] = _cuid(r["host"], "other", r["source_path"],
                                   r["chunk_ordinal"], r["content_sha"])
            return sorted(rows, key=lambda x: x["chunk_uid"])
        _rewrite(pkg, mutate_rows=cross)
        with pytest.raises(SnapshotError) as e:
            import_snapshot(dst, pkg)
        assert e.value.code == "partition"
        self._assert_untouched(dst)

    def test_no_partial_mutation_on_late_row_failure(self, tmp_path):
        _, pkg, dst = self._fresh(tmp_path)

        def break_last(rows):
            rows[-1]["start_line"] = 0       # row_safety on a LATER row
            return rows
        _rewrite(pkg, mutate_rows=break_last)
        with pytest.raises(SnapshotError) as e:
            import_snapshot(dst, pkg)
        assert e.value.code in ("row_safety", "row_identity")
        self._assert_untouched(dst)

    @staticmethod
    def _redigest_raw(pkg: Path):
        """Recompute both digests over chunks.jsonl AS-IS (no row
        re-serialization) so crafted non-canonical bytes reach the row
        validators with valid digests."""
        cb = (pkg / "chunks.jsonl").read_bytes()
        m = json.loads((pkg / "manifest.json").read_bytes())
        m["chunk_count"] = len([l for l in cb.split(b"\n") if l])
        m["content_digest"] = _sha256(cb)
        probe = {k: v for k, v in m.items() if k != "manifest_digest"}
        m["manifest_digest"] = _sha256(cb + _canonical_json(probe))
        (pkg / "manifest.json").write_bytes(_canonical_json(m))

    def test_non_canonical_row_bytes_rejected(self, tmp_path):
        _, pkg, dst = self._fresh(tmp_path)
        lines = (pkg / "chunks.jsonl").read_bytes().splitlines(True)
        # semantically identical, extra whitespace → not canonical bytes
        loose = json.dumps(json.loads(lines[0]), sort_keys=True,
                           ensure_ascii=False).encode() + b"\n"
        assert loose != lines[0]
        (pkg / "chunks.jsonl").write_bytes(loose + b"".join(lines[1:]))
        self._redigest_raw(pkg)
        with pytest.raises(SnapshotError) as e:
            import_snapshot(dst, pkg)
        assert e.value.code == "package_integrity"
        self._assert_untouched(dst)

    def test_row_key_set_enforced(self, tmp_path):
        for mut, err in (
                (lambda r: {**r, "extra_key": 1}, "extra"),
                (lambda r: {k: v for k, v in r.items()
                            if k != "language"}, "missing")):
            # fresh package per case
            import tempfile
            with tempfile.TemporaryDirectory() as td:
                tdp = Path(td)
                src, _ = _exporter(tdp)
                pkg = export_snapshot(src, "r1", tdp / "pkg")
                dst = _importer()
                _rewrite(pkg, mutate_rows=lambda rows: (
                    [mut(rows[0])] + rows[1:]))
                with pytest.raises(SnapshotError) as e:
                    import_snapshot(dst, pkg)
                assert e.value.code == "row_safety"

    def test_non_object_row_rejected(self, tmp_path):
        _, pkg, dst = self._fresh(tmp_path)
        body = (pkg / "chunks.jsonl").read_bytes().splitlines(True)
        (pkg / "chunks.jsonl").write_bytes(b"[1,2]\n" + b"".join(body[1:]))
        _rewrite2 = (pkg / "manifest.json")
        m = json.loads(_rewrite2.read_bytes())
        cb = (pkg / "chunks.jsonl").read_bytes()
        m["content_digest"] = _sha256(cb)
        probe = {k: v for k, v in m.items() if k != "manifest_digest"}
        m["manifest_digest"] = _sha256(cb + _canonical_json(probe))
        _rewrite2.write_bytes(_canonical_json(m))
        with pytest.raises(SnapshotError) as e:
            import_snapshot(dst, pkg)
        assert e.value.code in ("row_safety", "package_integrity")

    def test_meta_type_validation(self, tmp_path):
        _, pkg, dst = self._fresh(tmp_path)
        _rewrite(pkg, mutate_manifest=lambda m: m["meta"].update(
            ingest_globs="not-a-list"))
        with pytest.raises(SnapshotError) as e:
            import_snapshot(dst, pkg)
        assert e.value.code == "package_integrity"

    def test_impossible_exported_at_rejected(self, tmp_path):
        _, pkg, dst = self._fresh(tmp_path)
        _rewrite(pkg, mutate_manifest=lambda m: m.update(
            exported_at="2026-99-99T99:99:99Z"))
        with pytest.raises(SnapshotError) as e:
            import_snapshot(dst, pkg)
        assert e.value.code == "package_integrity"

    def test_root_level_key_files_denied(self, tmp_path):
        src, _ = _exporter(tmp_path, files={
            "docs/a.md": "# A\nok\n", "server.pem": "FAKE ROOT PEM\n",
            "id_rsa": "FAKE ROOT RSA\n"})
        ingest_paths(src, tmp_path / "repo", host="h1", origin_repo="r1",
                     globs=("**/*.md", "*.pem", "id_rsa"))
        pkg = export_snapshot(src, "r1", tmp_path / "pkg")
        body = (pkg / "chunks.jsonl").read_text(encoding="utf-8")
        assert "FAKE ROOT PEM" not in body and "FAKE ROOT RSA" not in body

    def test_negative_format_version_rejected(self, tmp_path):
        _, pkg, dst = self._fresh(tmp_path)
        _rewrite(pkg, mutate_manifest=lambda m: m.update(format_version=-1))
        with pytest.raises(SnapshotError) as e:
            import_snapshot(dst, pkg)
        assert e.value.code == "package_integrity"

    def test_empty_true_with_rows_rejected(self, tmp_path):
        _, pkg, dst = self._fresh(tmp_path)
        _rewrite(pkg, mutate_manifest=lambda m: m.update(empty=True))
        with pytest.raises(SnapshotError) as e:
            import_snapshot(dst, pkg)
        assert e.value.code == "package_integrity"

    def test_empty_without_flag_rejected(self, tmp_path):
        _, pkg, dst = self._fresh(tmp_path)
        _rewrite(pkg, mutate_rows=lambda rows: [],
                 mutate_manifest=lambda m: m.update(empty=False))
        with pytest.raises(SnapshotError) as e:
            import_snapshot(dst, pkg)
        assert e.value.code == "package_integrity"


class TestDeterminismAndSeq:
    def test_unchanged_store_exports_identical_bytes_and_seq(self, tmp_path):
        src, _ = _exporter(tmp_path)
        p1 = export_snapshot(src, "r1", tmp_path / "e1",
                             now_fn=lambda: FROZEN)
        p2 = export_snapshot(src, "r1", tmp_path / "e2",
                             now_fn=lambda: FROZEN)
        assert (p1 / "chunks.jsonl").read_bytes() == \
            (p2 / "chunks.jsonl").read_bytes()
        assert (p1 / "manifest.json").read_bytes() == \
            (p2 / "manifest.json").read_bytes()   # frozen clock: all bytes

    def test_seq_advances_only_on_content_change(self, tmp_path):
        src, root = _exporter(tmp_path)
        m1 = json.loads((export_snapshot(src, "r1", tmp_path / "e1")
                         / "manifest.json").read_bytes())
        (root / "docs/a.md").write_text("# Alpha\nchanged line\n",
                                        encoding="utf-8")
        ingest_paths(src, root, host="h1", origin_repo="r1")
        m2 = json.loads((export_snapshot(src, "r1", tmp_path / "e2")
                         / "manifest.json").read_bytes())
        assert m1["snapshot_seq"] == 1 and m2["snapshot_seq"] == 2


class TestReplay:
    def test_replay_matrix(self, tmp_path):
        src, root = _exporter(tmp_path)
        v1 = export_snapshot(src, "r1", tmp_path / "v1")
        dst = _importer()
        assert import_snapshot(dst, v1).status == "accepted"
        # same seq, same content, later exported_at → idempotent no-op
        v1b = export_snapshot(src, "r1", tmp_path / "v1b",
                              now_fn=lambda: datetime(
                                  2027, 1, 1, tzinfo=timezone.utc))
        assert import_snapshot(dst, v1b).status == "noop"
        # advance content → v2 accepted
        (root / "docs/a.md").write_text("# Alpha\nnewer\n", encoding="utf-8")
        ingest_paths(src, root, host="h1", origin_repo="r1")
        v2 = export_snapshot(src, "r1", tmp_path / "v2")
        assert import_snapshot(dst, v2).status == "accepted"
        # old snapshot replay → reject
        with pytest.raises(SnapshotError) as e:
            import_snapshot(dst, v1)
        assert e.value.code == "replay"
        # force override → accepted, prior state recorded
        res = import_snapshot(dst, v1, force=True)
        assert res.status == "accepted"
        forced = dst._con.execute(
            "SELECT forced_prev FROM manifests WHERE direction='import'"
        ).fetchone()[0]
        rec = json.loads(forced)
        assert rec["last_seq"] == 2
        assert rec["last_exported_at"]      # §7: overwritten exported_at
        assert rec["forced_at"]

    def test_same_seq_different_content_rejected(self, tmp_path):
        src, _ = _exporter(tmp_path)
        v1 = export_snapshot(src, "r1", tmp_path / "v1")
        dst = _importer()
        import_snapshot(dst, v1)

        def tweak(rows):
            from linecite.identity import (chunk_id, chunk_uid, content_sha)
            r = rows[0]
            r["text"] = r["text"] + " x"
            r["raw_text"] = r["raw_text"] + " x"
            r["content_sha"] = content_sha(r["text"])
            r["chunk_id"] = chunk_id(r["source_path"], r["text"])
            r["chunk_uid"] = chunk_uid(r["host"], r["origin_repo"],
                                       r["source_path"], r["chunk_ordinal"],
                                       r["content_sha"])
            return sorted(rows, key=lambda x: x["chunk_uid"])
        _rewrite(v1, mutate_rows=tweak)      # seq unchanged, content new
        with pytest.raises(SnapshotError) as e:
            import_snapshot(dst, v1)
        assert e.value.code == "replay"


class TestReconcileDeletes:
    def test_snapshot_is_full_authoritative_state(self, tmp_path):
        src, root = _exporter(tmp_path)
        v1 = export_snapshot(src, "r1", tmp_path / "v1")
        dst = _importer()
        import_snapshot(dst, v1)
        n1 = dst.count()
        (root / "docs/b.md").unlink()        # file removed upstream
        ingest_paths(src, root, host="h1", origin_repo="r1")
        v2 = export_snapshot(src, "r1", tmp_path / "v2")
        res = import_snapshot(dst, v2)
        assert res.reconcile.deleted > 0 and dst.count() < n1
        assert not dst._con.execute(
            "SELECT 1 FROM chunks WHERE source_path='docs/b.md'").fetchone()

    def test_explicit_empty_snapshot_clears_partition(self, tmp_path):
        src, root = _exporter(tmp_path)
        dst = _importer()
        import_snapshot(dst, export_snapshot(src, "r1", tmp_path / "v1"))
        for f in (root / "docs").iterdir():
            f.unlink()
        ingest_paths(src, root, host="h1", origin_repo="r1")
        v2 = export_snapshot(src, "r1", tmp_path / "v2")
        m = json.loads((v2 / "manifest.json").read_bytes())
        assert m["empty"] is True
        import_snapshot(dst, v2)
        assert dst._con.execute(
            "SELECT COUNT(*) FROM chunks WHERE host='h1'").fetchone()[0] == 0


class TestDenyHook:
    def test_secret_file_excluded_and_recorded(self, tmp_path):
        src, _ = _exporter(tmp_path, files={
            "docs/a.md": "# A\nnormal\n",
            ".env": "AWS_SECRET_ACCESS_KEY=fake123\n",
            "keys/service.pem": "-----BEGIN FAKE KEY-----\n"})
        # .env/.pem match default globs only if ingested; ingest .env too
        ingest_paths(src, tmp_path / "repo", host="h1", origin_repo="r1",
                     globs=("**/*.md", ".env", "keys/*.pem"))
        pkg = export_snapshot(src, "r1", tmp_path / "pkg")
        m = json.loads((pkg / "manifest.json").read_bytes())
        assert m["excluded_count"] >= 2
        body = (pkg / "chunks.jsonl").read_text(encoding="utf-8")
        assert "fake123" not in body and "FAKE KEY" not in body

    def test_deny_change_without_output_change_is_idempotent(self, tmp_path):
        src, _ = _exporter(tmp_path)
        v1 = export_snapshot(src, "r1", tmp_path / "v1")
        dst = _importer()
        import_snapshot(dst, v1)
        wider = tuple(DEFAULT_DENY) + ("**/*.sqlite",)
        v2 = export_snapshot(src, "r1", tmp_path / "v2", deny=wider)
        m1 = json.loads((v1 / "manifest.json").read_bytes())
        m2 = json.loads((v2 / "manifest.json").read_bytes())
        assert m1["snapshot_seq"] == m2["snapshot_seq"]
        assert m1["content_digest"] == m2["content_digest"]
        assert import_snapshot(dst, v2).status == "noop"
