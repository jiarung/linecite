# Normative MUST clauses — snapshot contract v0 (UNSTABLE)

Extracted from docs/design/snapshot-contract.md for conformance
enumeration; the prose spec governs on any wording conflict.

## Canonical bytes (§3)

1. chunks.jsonl rows MUST be UTF-8 canonical JSON (code-point-sorted
   keys, compact separators), one `\n`-terminated line per row, no
   blank lines, rows sorted ascending by `chunk_uid`.
2. `content_digest` = sha256(chunks.jsonl bytes) and is the ONLY
   replay/idempotence content key.
3. `manifest_digest` = sha256(chunks.jsonl bytes ‖ canonical JSON of
   the manifest minus `manifest_digest`, unknown keys INCLUDED);
   importers MUST recompute both digests and reject mismatches;
   `manifest_digest` MUST NOT participate in replay decisions.

## Identity (§4, §6)

4. `content_sha`, `chunk_id`, `chunk_uid` are COMPUTED, never trusted
   as declared: importers MUST recompute per the §4 formulas (UTF-8,
   no unicode normalization, lowercase hex) and reject any mismatch.
5. Within one store, host id ↔ host_identity_fingerprint MUST be 1:1
   in both directions, enforced on EVERY write path (not only import).
6. Export host identity MUST come from the store's persisted local
   identity, never from the caller; v0 exports the local partition
   only.

## Import pipeline (§8 — order is normative)

7. Steps MUST run in order: package integrity → row safety → row
   identity → partition consistency → identity checks (self_import,
   then host_collision) → replay. Any failure rejects the WHOLE
   package with no observable store mutation (single transaction).
8. Row safety MUST validate: canonical POSIX relative source_path
   (§5), integer chunk_ordinal ≥ 0, 1-indexed start_line ≤ end_line,
   exact §2.1 key set, per-line canonical bytes, non-null raw_text,
   no duplicate chunk_uid or (source_path, chunk_ordinal).
9. Every row's host/origin_repo MUST equal the manifest declaration.
10. Replay per (host, origin_repo) stream: seq greater → accept;
    seq equal + content_digest equal → idempotent no-op (audit state
    still updated); seq equal + digest different, or seq smaller →
    reject. force=True MUST record the overwritten prior state
    (seq, content_digest, exported_at, forced_at).
11. An accepted snapshot is the stream's FULL authoritative state:
    matched rows overwrite every contract field (NULL writes NULL);
    absent rows are deleted. chunk_count 0 MUST carry empty:true.

## Behavior (§9)

12. With local_host given, a chunk whose host differs MUST validate
    as `remote_unverified` without filesystem access; legacy
    host-NULL rows keep local semantics; without local_host,
    single-host behavior is unchanged.
13. Status enum is CLOSED (source_missing, content_drift,
    no_raw_text, remote_unverified): unknown values reject; expansion
    requires a format_version bump.
14. Direct writes to a registered remote host MUST be rejected
    outside the import transaction context (allow_direct_writes is a
    test/simulation escape hatch that bypasses replay protection).
    Exemption: explicit operations that update ONLY local storage
    metadata (embedding, embedded_at, embedding_model — none of which
    are contract fields or participate in replay/identity) MAY touch
    any partition, and MUST NOT alter contract fields or manifests
    state.
