"""Snapshot export/import — the cross-host exchange surface (Phase C1).

Implements docs/design/snapshot-contract.md (DRAFT v0): a snapshot
package is a directory holding `chunks.jsonl` (canonical, chunk_uid
sorted, contract fields only) and `manifest.json`. Export always ships
the LOCAL partition of one origin_repo; import runs the §8 validation
pipeline (package integrity → row safety → row identity → partition
consistency → identity checks → replay) and reconciles the declared
stream as its full authoritative state — all-or-nothing.

Zero network: files in, files out. Transport belongs to the satellite.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from fnmatch import fnmatch
from pathlib import Path
from typing import Callable, Iterable, Optional

import duckdb

from linecite.identity import chunk_id as _cid, chunk_uid as _cuid, content_sha as _csha
from linecite.store import Chunk, ChunkStore, SnapshotResult

FORMAT_VERSION = 0

# Contract §2.1 row fields, in the fixed serialization set. raw_text is
# NOT nullable in snapshots; the four metadata fields are.
_ROW_FIELDS = ("chunk_uid", "chunk_id", "host", "origin_repo",
               "source_path", "source_type", "start_line", "end_line",
               "text", "raw_text", "content_sha", "chunk_ordinal",
               "heading_path", "artifact_type", "language", "quality_tag")
_NULLABLE_ROW_FIELDS = frozenset(
    {"heading_path", "artifact_type", "language", "quality_tag"})

_MANIFEST_REQUIRED = ("format_version", "host", "host_identity_fingerprint",
                      "origin_repo", "content_digest", "snapshot_seq",
                      "manifest_digest", "chunk_count", "exported_at",
                      "excluded_count", "empty", "meta")
_META_KEYS = ("repo_url", "repo_sha", "ingest_globs", "ingested_at")

_HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_HEX32_RE = re.compile(r"^[0-9a-f]{32}$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_HEX16_RE = re.compile(r"^[0-9a-f]{16}$")
_EXPORTED_AT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

# Export-side deny list (Phase C1.5): matched chunks are NOT written out.
DEFAULT_DENY = (".env", "**/.env",
                "*.pem", "**/*.pem", "*.key", "**/*.key",
                "id_rsa", "**/id_rsa", "id_ed25519", "**/id_ed25519",
                "credentials*", "**/credentials*",
                "secrets*", "**/secrets*")


class SnapshotError(ValueError):
    """Snapshot package rejected; `code` names the contract error class."""

    def __init__(self, code: str, msg: str):
        super().__init__(f"{code}: {msg}")
        self.code = code


@dataclass(frozen=True)
class ImportResult:
    status: str                      # 'accepted' | 'noop'
    snapshot_seq: int
    content_digest: str
    reconcile: Optional[SnapshotResult] = None


def _canonical_json(obj) -> bytes:
    """Contract §3 canonical JSON: UTF-8, code-point-sorted keys, compact
    separators, no trailing newline."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _now_utc_str(now_fn: Callable[[], datetime]) -> str:
    return now_fn().astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Export


def export_snapshot(store: ChunkStore, origin_repo: str,
                    out_dir: Path | str, *,
                    meta: Optional[dict] = None,
                    deny: Iterable[str] = DEFAULT_DENY,
                    now_fn: Callable[[], datetime] = None) -> Path:
    """Export the LOCAL partition of `origin_repo` as a snapshot package.

    The host identity is the store's persisted local identity — never
    caller-supplied (contract §6). v0 exports the local partition only;
    forwarding a remote partition is explicitly excluded.
    """
    now_fn = now_fn or (lambda: datetime.now(timezone.utc))
    ident = store.local_identity()
    if ident is None:
        raise SnapshotError("no_local_identity",
                            "store has no local identity; ingest first")
    host, fingerprint = ident
    meta = dict(meta or {})
    meta_obj = {k: meta.get(k) for k in _META_KEYS}

    rows = store._con.execute(
        f"SELECT {', '.join(_ROW_FIELDS)} FROM chunks "
        "WHERE host = ? AND origin_repo = ? ORDER BY chunk_uid ASC",
        [host, origin_repo]).fetchall()

    deny = tuple(deny)
    lines: list[bytes] = []
    excluded = 0
    for r in rows:
        rec = dict(zip(_ROW_FIELDS, r))
        if any(fnmatch(rec["source_path"], pat) for pat in deny):
            excluded += 1
            continue
        if rec["raw_text"] is None:
            raise SnapshotError(
                "legacy_row", f"{rec['source_path']}: raw_text is NULL — "
                "legacy rows are not exportable (contract §2.3)")
        lines.append(_canonical_json(rec) + b"\n")
    chunks_bytes = b"".join(lines)
    content_digest = _sha256(chunks_bytes)

    # snapshot_seq (contract §7): per (host, origin_repo, 'export');
    # advances only when content_digest changed vs the last export.
    prev = store._con.execute(
        "SELECT last_seq, last_content_digest FROM manifests "
        "WHERE host = ? AND origin_repo = ? AND direction = 'export'",
        [host, origin_repo]).fetchone()
    if prev is None:
        seq = 1
    elif prev[1] == content_digest:
        seq = int(prev[0])
    else:
        seq = int(prev[0]) + 1

    exported_at = _now_utc_str(now_fn)
    manifest = {
        "format_version": FORMAT_VERSION,
        "host": host,
        "host_identity_fingerprint": fingerprint,
        "origin_repo": origin_repo,
        "content_digest": content_digest,
        "snapshot_seq": seq,
        "chunk_count": len(lines),
        "exported_at": exported_at,
        "excluded_count": excluded,
        "empty": len(lines) == 0,
        "meta": meta_obj,
    }
    manifest["manifest_digest"] = _sha256(
        chunks_bytes + _canonical_json(manifest))

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "chunks.jsonl").write_bytes(chunks_bytes)
    (out / "manifest.json").write_bytes(_canonical_json(manifest))

    store._con.execute("BEGIN TRANSACTION")
    try:
        store._con.execute(
            "INSERT INTO manifests (host, origin_repo, direction, "
            "host_identity_fingerprint, last_seq, last_content_digest, "
            "last_exported_at, last_manifest_digest) "
            "VALUES (?, ?, 'export', ?, ?, ?, ?, ?) "
            "ON CONFLICT (host, origin_repo, direction) DO UPDATE SET "
            "last_seq = excluded.last_seq, "
            "last_content_digest = excluded.last_content_digest, "
            "last_exported_at = excluded.last_exported_at, "
            "last_manifest_digest = excluded.last_manifest_digest",
            [host, origin_repo, fingerprint, seq, content_digest,
             exported_at, manifest["manifest_digest"]])
        store._con.execute("COMMIT")
    except duckdb.Error:
        store._con.execute("ROLLBACK")
        raise
    return out


# ---------------------------------------------------------------------------
# Import — contract §8 pipeline (order is normative)


def _load_json_strict(data: bytes, what: str):
    def no_dups(pairs):
        keys = [k for k, _ in pairs]
        if len(keys) != len(set(keys)):
            raise SnapshotError("package_integrity",
                                f"{what}: duplicate JSON keys")
        return dict(pairs)

    def no_nonfinite(tok):
        raise SnapshotError("package_integrity",
                            f"{what}: non-finite number {tok!r}")
    try:
        return json.loads(data.decode("utf-8"),
                          object_pairs_hook=no_dups,
                          parse_constant=no_nonfinite)
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise SnapshotError("package_integrity", f"{what}: {e}") from e


def _valid_exported_at(v) -> bool:
    """Exactly YYYY-MM-DDTHH:MM:SSZ AND a real calendar instant — the
    regex alone would admit 2026-99-99T99:99:99Z."""
    if not isinstance(v, str) or not _EXPORTED_AT_RE.match(v):
        return False
    try:
        datetime.strptime(v, "%Y-%m-%dT%H:%M:%SZ")
        return True
    except ValueError:
        return False


def _validate_manifest(manifest, chunks_bytes: bytes) -> None:
    if not isinstance(manifest, dict):
        raise SnapshotError("package_integrity", "manifest: not an object")
    for key in _MANIFEST_REQUIRED:
        if key not in manifest:
            raise SnapshotError("package_integrity",
                                f"manifest: missing required key {key!r}")
    fv = manifest["format_version"]
    if not isinstance(fv, int) or isinstance(fv, bool) or fv < 0:
        raise SnapshotError("package_integrity",
                            "manifest: format_version must be int >= 0")
    if fv > FORMAT_VERSION:
        raise SnapshotError("unknown_version",
                            f"format_version {fv} > known {FORMAT_VERSION}")
    checks = [
        ("host", lambda v: isinstance(v, str) and _HOST_RE.match(v)),
        ("host_identity_fingerprint",
         lambda v: isinstance(v, str) and _HEX32_RE.match(v)),
        ("origin_repo", lambda v: isinstance(v, str) and v),
        ("content_digest",
         lambda v: isinstance(v, str) and _HEX64_RE.match(v)),
        ("manifest_digest",
         lambda v: isinstance(v, str) and _HEX64_RE.match(v)),
        ("snapshot_seq",
         lambda v: isinstance(v, int) and not isinstance(v, bool) and v >= 0),
        ("chunk_count",
         lambda v: isinstance(v, int) and not isinstance(v, bool) and v >= 0),
        ("excluded_count",
         lambda v: isinstance(v, int) and not isinstance(v, bool) and v >= 0),
        ("exported_at", _valid_exported_at),
        ("empty", lambda v: isinstance(v, bool)),
        ("meta", lambda v: isinstance(v, dict)),
    ]
    for key, ok in checks:
        if not ok(manifest[key]):
            raise SnapshotError("package_integrity",
                                f"manifest: invalid {key!r}")
    for k in _META_KEYS:
        if k not in manifest["meta"]:
            raise SnapshotError("package_integrity",
                                f"manifest: meta missing key {k!r}")
    for k in ("repo_url", "repo_sha", "ingested_at"):
        v = manifest["meta"][k]
        if v is not None and not isinstance(v, str):
            raise SnapshotError("package_integrity",
                                f"manifest: meta.{k} must be string|null")
    g = manifest["meta"]["ingest_globs"]
    if g is not None and not (isinstance(g, list)
                              and all(isinstance(x, str) for x in g)):
        raise SnapshotError("package_integrity",
                            "manifest: meta.ingest_globs must be "
                            "string array|null")
    if manifest["empty"] is True and manifest["chunk_count"] != 0:
        raise SnapshotError("package_integrity",
                            "manifest: empty:true with nonzero chunk_count")
    # Digests (contract §3): manifest_digest covers ALL other keys —
    # unknown keys included — recomputed over canonical bytes.
    if _sha256(chunks_bytes) != manifest["content_digest"]:
        raise SnapshotError("package_integrity", "content_digest mismatch")
    probe = {k: v for k, v in manifest.items() if k != "manifest_digest"}
    if _sha256(chunks_bytes + _canonical_json(probe)) != \
            manifest["manifest_digest"]:
        raise SnapshotError("package_integrity", "manifest_digest mismatch")
    if manifest["chunk_count"] == 0 and manifest["empty"] is not True:
        raise SnapshotError(
            "package_integrity",
            "empty snapshot without empty:true (mass-delete guard, §8)")


_SAFE_SEGMENT = re.compile(r"^[^/\0\\]+$")


def _check_source_path(p) -> None:
    if not isinstance(p, str) or not p:
        raise SnapshotError("row_safety", f"source_path invalid: {p!r}")
    if p.startswith("/") or p.endswith("/") or "\\" in p or "\0" in p:
        raise SnapshotError("row_safety", f"source_path invalid: {p!r}")
    for seg in p.split("/"):
        if not seg or seg in (".", "..") or not _SAFE_SEGMENT.match(seg):
            raise SnapshotError("row_safety",
                                f"source_path segment invalid: {p!r}")


def _validate_rows(chunks_bytes: bytes, manifest) -> list[dict]:
    if chunks_bytes and not chunks_bytes.endswith(b"\n"):
        raise SnapshotError("package_integrity",
                            "chunks.jsonl: missing final newline")
    raw_lines = chunks_bytes.split(b"\n")[:-1] if chunks_bytes else []
    if manifest["chunk_count"] != len(raw_lines):
        raise SnapshotError("package_integrity",
                            f"chunk_count {manifest['chunk_count']} != "
                            f"{len(raw_lines)} rows")
    rows: list[dict] = []
    seen_uid: set = set()
    seen_po: set = set()
    prev_uid = ""
    for i, line in enumerate(raw_lines):
        if not line:
            raise SnapshotError("package_integrity",
                                f"chunks.jsonl: blank line {i + 1}")
        rec = _load_json_strict(line, f"chunks.jsonl line {i + 1}")
        loc = f"row {i + 1}"
        if not isinstance(rec, dict):
            raise SnapshotError("row_safety", f"{loc}: not a JSON object")
        # Canonical wire form (§2.1): exactly the contract key set, and
        # the line bytes must equal their canonical re-serialization —
        # unsorted keys, extra whitespace, or unknown/missing keys reject.
        if set(rec) != set(_ROW_FIELDS):
            extra = set(rec) - set(_ROW_FIELDS)
            missing_k = set(_ROW_FIELDS) - set(rec)
            raise SnapshotError(
                "row_safety",
                f"{loc}: key set mismatch (extra={sorted(extra)}, "
                f"missing={sorted(missing_k)})")
        if _canonical_json(rec) != line:
            raise SnapshotError("package_integrity",
                                f"{loc}: not canonical JSON bytes")
        # -- row safety (§8.2)
        _check_source_path(rec.get("source_path"))
        for f_ in ("chunk_uid", "chunk_id", "host", "origin_repo",
                   "source_type", "text", "raw_text", "content_sha"):
            if not isinstance(rec.get(f_), str):
                raise SnapshotError("row_safety", f"{loc}: {f_} not a string")
        if not rec["source_type"]:
            raise SnapshotError("row_safety", f"{loc}: source_type empty")
        for f_ in ("start_line", "end_line", "chunk_ordinal"):
            v = rec.get(f_)
            if not isinstance(v, int) or isinstance(v, bool):
                raise SnapshotError("row_safety", f"{loc}: {f_} not an int")
        for f_ in _NULLABLE_ROW_FIELDS:
            v = rec.get(f_)
            if v is not None and not isinstance(v, str):
                raise SnapshotError("row_safety",
                                    f"{loc}: {f_} must be string or null")
        if rec["chunk_ordinal"] < 0:
            raise SnapshotError("row_safety", f"{loc}: chunk_ordinal < 0")
        if rec["start_line"] < 1 or rec["end_line"] < rec["start_line"]:
            raise SnapshotError("row_safety", f"{loc}: invalid line range")
        if not _HEX32_RE.match(rec["chunk_uid"]):
            raise SnapshotError("row_safety", f"{loc}: chunk_uid not 32 hex")
        if not _HEX16_RE.match(rec["chunk_id"]):
            raise SnapshotError("row_safety", f"{loc}: chunk_id not 16 hex")
        if not _HEX64_RE.match(rec["content_sha"]):
            raise SnapshotError("row_safety", f"{loc}: content_sha not 64 hex")
        # -- row identity (§8.3): computed, not declared
        if rec["content_sha"] != _csha(rec["text"]):
            raise SnapshotError("row_identity",
                                f"{loc}: content_sha mismatch")
        if rec["chunk_id"] != _cid(rec["source_path"], rec["text"]):
            raise SnapshotError("row_identity", f"{loc}: chunk_id mismatch")
        if rec["chunk_uid"] != _cuid(rec["host"], rec["origin_repo"],
                                     rec["source_path"],
                                     rec["chunk_ordinal"],
                                     rec["content_sha"]):
            raise SnapshotError("row_identity", f"{loc}: chunk_uid mismatch")
        # -- partition consistency (§8.4)
        if rec["host"] != manifest["host"] or \
                rec["origin_repo"] != manifest["origin_repo"]:
            raise SnapshotError("partition",
                                f"{loc}: row partition != manifest")
        # -- duplicates + ordering (§2.1)
        if rec["chunk_uid"] in seen_uid:
            raise SnapshotError("row_safety", f"{loc}: duplicate chunk_uid")
        po = (rec["source_path"], rec["chunk_ordinal"])
        if po in seen_po:
            raise SnapshotError("row_safety",
                                f"{loc}: duplicate (source_path, ordinal)")
        if rec["chunk_uid"] <= prev_uid:
            raise SnapshotError("package_integrity",
                                f"{loc}: rows not sorted by chunk_uid")
        seen_uid.add(rec["chunk_uid"])
        seen_po.add(po)
        prev_uid = rec["chunk_uid"]
        rows.append(rec)
    return rows


def import_snapshot(store: ChunkStore, package_dir: Path | str, *,
                    force: bool = False) -> ImportResult:
    """Validate and reconcile a snapshot package (contract §8).

    Any rejection leaves the store untouched (single transaction);
    acceptance treats the snapshot as the stream's full authoritative
    state — matched rows overwrite every contract field, absent rows
    are deleted.
    """
    pkg = Path(package_dir)
    try:
        manifest_bytes = (pkg / "manifest.json").read_bytes()
        chunks_bytes = (pkg / "chunks.jsonl").read_bytes()
    except OSError as e:
        raise SnapshotError("package_integrity", f"unreadable package: {e}")

    manifest = _load_json_strict(manifest_bytes, "manifest.json")
    _validate_manifest(manifest, chunks_bytes)          # §8.1
    rows = _validate_rows(chunks_bytes, manifest)       # §8.2-8.4

    # §8.5 identity checks (order + distinct error classes)
    local = store.local_identity()
    if local is None:
        raise SnapshotError("no_local_identity",
                            "importing store has no local identity")
    m_host, m_fp = manifest["host"], manifest["host_identity_fingerprint"]
    if m_host == local[0]:
        if m_fp == local[1]:
            raise SnapshotError("self_import",
                                "snapshot identity equals local identity")
        raise SnapshotError("host_collision",
                            f"host {m_host!r} is the local id with a "
                            "different fingerprint")
    reg = store._con.execute(
        "SELECT fingerprint FROM hosts WHERE host = ?", [m_host]).fetchone()
    if reg is not None and reg[0] != m_fp:
        raise SnapshotError("host_collision",
                            f"host {m_host!r} registered with a different "
                            "fingerprint")
    other = store._con.execute(
        "SELECT host FROM hosts WHERE fingerprint = ? AND host != ?",
        [m_fp, m_host]).fetchone()
    if other is not None:
        raise SnapshotError("host_collision",
                            f"fingerprint already bound to host "
                            f"{other[0]!r} (1:1 invariant, §6)")

    # §8.6 replay / idempotence (per import stream)
    stream = store._con.execute(
        "SELECT last_seq, last_content_digest, last_exported_at "
        "FROM manifests WHERE "
        "host = ? AND origin_repo = ? AND direction = 'import'",
        [m_host, manifest["origin_repo"]]).fetchone()
    seq = manifest["snapshot_seq"]
    forced_prev = None
    is_noop = False
    if stream is not None and not force:
        last_seq, last_digest = int(stream[0]), stream[1]
        if seq == last_seq and manifest["content_digest"] == last_digest:
            is_noop = True
        elif seq <= last_seq:
            raise SnapshotError(
                "replay",
                f"snapshot_seq {seq} <= last accepted {last_seq} with "
                "different content (rollback/replay); force=True overrides")
    elif stream is not None and force:
        # §7 force audit: the full overwritten prior state, incl. its
        # exported_at.
        forced_prev = _canonical_json(
            {"last_seq": int(stream[0]), "last_content_digest": stream[1],
             "last_exported_at": stream[2],
             "forced_at": datetime.now(timezone.utc).strftime(
                 "%Y-%m-%dT%H:%M:%SZ")}).decode()

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    store._con.execute("BEGIN TRANSACTION")
    store._import_ctx_host = m_host
    try:
        if reg is None:
            store.register_host(m_host, m_fp)
        # Audit state updates on every accept AND idempotent no-op (§7).
        store._con.execute(
            "INSERT INTO manifests (host, origin_repo, direction, "
            "host_identity_fingerprint, last_seq, last_content_digest, "
            "last_exported_at, last_manifest_digest, last_imported_at, "
            "forced_prev) VALUES (?, ?, 'import', ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (host, origin_repo, direction) DO UPDATE SET "
            "host_identity_fingerprint = excluded.host_identity_fingerprint, "
            "last_seq = excluded.last_seq, "
            "last_content_digest = excluded.last_content_digest, "
            "last_exported_at = excluded.last_exported_at, "
            "last_manifest_digest = excluded.last_manifest_digest, "
            "last_imported_at = excluded.last_imported_at, "
            "forced_prev = COALESCE(excluded.forced_prev, forced_prev)",
            [m_host, manifest["origin_repo"], m_fp, seq,
             manifest["content_digest"], manifest["exported_at"],
             manifest["manifest_digest"], now, forced_prev])
        reconcile = None
        if not is_noop:
            chunks = [Chunk(
                chunk_id=r["chunk_id"], source_path=r["source_path"],
                source_type=r["source_type"], text=r["text"],
                start_line=r["start_line"], end_line=r["end_line"],
                embedded_at=now,          # local storage metadata (§2.1)
                embedding=None,
                raw_text=r["raw_text"], heading_path=r["heading_path"],
                artifact_type=r["artifact_type"], language=r["language"],
                quality_tag=r["quality_tag"], host=r["host"],
                origin_repo=r["origin_repo"], content_sha=r["content_sha"],
                chunk_ordinal=r["chunk_ordinal"], chunk_uid=r["chunk_uid"],
            ) for r in rows]
            reconcile = store.upsert_snapshot(
                chunks, host=m_host, origin_repo=manifest["origin_repo"],
                sealed=True)
        store._con.execute("COMMIT")
    except Exception:
        store._con.execute("ROLLBACK")
        raise
    finally:
        store._import_ctx_host = None
    return ImportResult(status="noop" if is_noop else "accepted",
                        snapshot_seq=seq,
                        content_digest=manifest["content_digest"],
                        reconcile=reconcile)
