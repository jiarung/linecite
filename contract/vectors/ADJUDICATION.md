# Vector freeze adjudication (Phase K.2.5)

Each vector was checked clause-by-clause against
docs/design/snapshot-contract.md BEFORE freezing. One divergence was
found and adjudicated (see "Spec-bug record" below); code is not the
default authority.

## Checks applied to every vector (2026-09-28)

- **Digests (§3, clauses 2-3)**: content_digest recomputed over
  chunks.jsonl bytes; manifest_digest recomputed over
  chunks-bytes ‖ canonical(manifest − manifest_digest). Match for
  v1/v2/v4; v3 mismatches BY DESIGN (its error case).
- **Row canonical bytes + key set (§2.1, clause 1/8)**: every line
  re-serializes to identical bytes; key set == the 16 §2.1 fields;
  machine-checked via jsonschema in tests/test_k_conformance.py
  (v3/v4 included — their JSON SHAPE is valid; rejection is semantic).
- **Identity formulas (§4, clause 4)**: content_sha/chunk_id/chunk_uid
  recomputed per formula for v1/v2 rows — match. v4 row 1 mismatches
  BY DESIGN (its error case).
- **Ordering/uniqueness (§2.1)**: rows ascending by chunk_uid; no
  duplicate chunk_uid or (source_path, chunk_ordinal).
- **Partition consistency (§8.4, clause 9)**: all rows carry
  host=vechost, origin_repo=vecrepo == manifest declaration.
- **source_path grammar (§5)**: all paths relative POSIX, no
  dot-segments/backslash/NUL (schema pattern enforced).
- **Empty guard (§8, clause 11)**: v2 has chunk_count 0 AND
  empty:true; v1/v3/v4 have empty:false with rows.
- **exported_at (§2.2)**: exact YYYY-MM-DDTHH:MM:SSZ and a real UTC
  instant (2026-09-28T00:00:00Z).
- **Expected error-code ordering (§8, clause 7)**: v3 fails at step 1
  (package_integrity) BEFORE any row/identity/partition check could
  run; v4 passes steps 1-2 and fails at step 3 (row_identity) — both
  verified by the conformance run.

## Per-vector verdicts

- **v1-basic** — accept; seq 1 on first export; manifest == §2.2
  required 12 keys exactly; meta == 4 keys. No divergence.
- **v2-empty** — accept; mass-delete guard satisfied on the accept
  side (empty:true + 0 rows). No divergence.
- **v3-tampered-manifest** — reject package_integrity (origin_repo
  edited post-digest; transport integrity). No divergence.
- **v4-row-identity-tamper** — reject row_identity (text edited,
  BOTH digests validly recomputed): proves transport integrity alone
  is insufficient — the two-digest split's rationale. No divergence.

## Spec-bug record

- **Row-level canonical JSON**: during freeze review it was found the
  prose spec required canonical serialization explicitly only for the
  MANIFEST (§3) — the per-row rule existed in implementation, clauses
  and schema but had been lost from §2.1 in a rewrite. Adjudicated as
  a **spec bug** (the rule is load-bearing: golden-vector byte
  stability and cross-implementation digest agreement depend on it);
  §2.1/§3 amended BEFORE freezing these vectors. No code change.

## Generation notes

Producer fingerprint pinned (`ab`*16) and clock frozen
(2026-09-28T00:00:00Z) for regeneration stability — generation
conveniences, not contract requirements.
