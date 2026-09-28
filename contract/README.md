# linecite snapshot contract — v0 (UNSTABLE)

Frozen at Phase K from real `export_snapshot` output. **v0 is marked
unstable**: breaking changes are still allowed pre-release, but every
change MUST bump `format_version` and regenerate the vectors.

Contents (no implementation code lives here — by design):

- `schema/manifest.v0.schema.json` — machine-readable manifest schema
- `schema/chunk-row.v0.schema.json` — machine-readable chunks.jsonl
  row schema
- `clauses.md` — the normative MUST clauses (validation pipeline
  order, replay rules, identity invariants, status enum)
- `vectors/` — golden test vectors: real frozen snapshot packages plus
  `expected.json` per vector; `ADJUDICATION.md` records the
  freeze-gate verdicts (each vector checked against the prose spec —
  code is not the default authority)

The authoritative prose specification is
`docs/design/snapshot-contract.md`; **if this directory is ever moved
to its own repo, that file moves with it** (clauses.md is an
enumeration aid, not a standalone replacement).

Conformance: any importer should pass
`linecite-conformance contract/vectors` — the default vectors path is
relative to the current directory, so run it from the repo root (or
pass the path explicitly). Runner lives in core
(`linecite/conformance.py`; contract/ itself holds no code);
regeneration: `scripts/gen_vectors.py`.
