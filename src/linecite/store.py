"""DuckDB VSS-backed chunk store with HNSW index.

"""
from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

import duckdb

EMBEDDING_DIM = 1536

# Store schema version (chunks_meta key 'schema_version').
# v2 (Phase C0): chunk_uid is the NOT NULL primary key; chunk_id is a
# non-unique legacy dedup column; hosts registry table added.
SCHEMA_VERSION = 2

# User-defined source vocabulary. Deployments pick their own values; the
# core store treats it as an opaque tag.
SourceType = str


@dataclass(frozen=True)
class Chunk:
    chunk_id: str                 # content-hash (dedup key)
    source_path: str              # relative path
    source_type: SourceType       # user-defined tag (see SourceType note)
    text: str
    start_line: int
    end_line: int
    embedded_at: datetime
    embedding: Optional[list[float]] = field(default=None, repr=False)
    # Which model produced `embedding` (e.g. "text-embedding-3-small",
    # "model2vec/potion-base-8M"). Write-boundary invariant for NEW
    # writes: embedding IS NOT NULL <=> embedding_model IS NOT NULL.
    # Pre-provenance rows (vector with NULL model) are quarantined
    # legacy: excluded from model-scoped vector queries until reembed.
    embedding_model: Optional[str] = None
    # Metadata columns All nullable
    # for backwards compat with legacy rows and to avoid blocking ingest
    # when a field cannot be inferred.
    artifact_type: Optional[str] = None        # user vocabulary, e.g. spec | doc | note (deployment-defined)
    heading_path: Optional[str] = None         # e.g. "results/v3 > Why hypothesis did not survive"
    language: Optional[str] = None             # en | zh | mixed | None
    quality_tag: Optional[str] = None          # e.g. verified | draft (deployment-defined)
    # Verbatim source slice for start_line..end_line (citation ground
    # truth; None on legacy rows). `text` remains the search input.
    raw_text: Optional[str] = None
    # Extension metadata (domain-specific fields registered by a deployment
    # adapter). Persisted + filterable only for keys registered via
    # ChunkStore(extension_columns=...); unregistered keys are ignored.
    ext: Optional[dict] = None
    # Provenance columns. Nullable for
    # backwards compat with legacy rows; ingester + backfill populate them.
    # chunk_uid is the cross-host identity / dedup+upsert key (128-bit hex).
    host: Optional[str] = None                 # stable host id (not FQDN), deployment-chosen
    origin_repo: Optional[str] = None          # repo basename the chunk came from
    content_sha: Optional[str] = None          # sha256(text) full hex — change detection
    chunk_ordinal: Optional[int] = None        # 0-based index of chunk within its file (stable)
    chunk_uid: Optional[str] = None            # sha256(host\0repo\0path\0ordinal\0content_sha)[:32]


@dataclass
class SnapshotResult:
    """Outcome of a upsert_snapshot() call."""
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    deleted: int = 0


import re as _re

_EXT_NAME_RE = _re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_EXT_TYPE_ALLOWLIST = frozenset({"TEXT", "INTEGER", "DOUBLE", "BOOLEAN"})


class ChunkStore:
    """Persistent DuckDB store for chunks + HNSW vector index.

    Compatibility note: a DB written by a store WITH extension columns
    opened by a bare ChunkStore keeps those columns physically present but
    invisible — not returned in Chunk.ext and not filterable. Callers that
    need them must open the DB through the registering deployment adapter."""

    def __init__(self, db_path: Path | str = ":memory:", dim: int = EMBEDDING_DIM,
                 extension_columns: Optional[list[tuple[str, str]]] = None,
                 require_embedding: bool = False):
        self.db_path = str(db_path)
        self.dim = dim
        # Domain extension columns registered by the deployment (adapter).
        # Each entry is (column_name, sql_type). Registered columns are
        # persisted from Chunk.ext, returned inside Chunk.ext, and allowed
        # as query_topk filter keys.
        self._ext_columns: list[tuple[str, str]] = list(extension_columns or [])
        core_names = ({c for c, _ in self._METADATA_COLUMNS}
                      | {c for c, _ in self._CONTENT_COLUMNS}
                      | {c for c, _ in self._PROVENANCE_COLUMNS}
                      | {"chunk_id", "source_path", "source_type", "text",
                         "start_line", "end_line", "embedded_at", "embedding"})
        # Extension names/types are spliced into SQL — validate before any
        # SQL is built (identifier regex, type allowlist, no dups/shadows).
        seen_lower: set[str] = set()
        for name, sqltype in self._ext_columns:
            if not _EXT_NAME_RE.match(name):
                raise ValueError(f"invalid extension column name: {name!r}")
            if sqltype.upper() not in _EXT_TYPE_ALLOWLIST:
                raise ValueError(
                    f"extension column {name}: type {sqltype!r} not in "
                    f"{sorted(_EXT_TYPE_ALLOWLIST)}")
            if name.lower() in seen_lower:
                raise ValueError(f"duplicate extension column: {name}")
            seen_lower.add(name.lower())
            if name.lower() in {c.lower() for c in core_names}:
                raise ValueError(f"extension column shadows core column: {name}")
        # Lexical deployments may run without embeddings entirely; setting
        # require_embedding=True restores the strict legacy contract
        # (every inserted chunk must carry a dim-length vector) — the domain
        # adapter does this.
        self.require_embedding = require_embedding
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._con = duckdb.connect(self.db_path)
        # VSS is optional (extra): vector queries brute-force fine without
        # it; the HNSW index is only an accelerator. A no-network machine
        # without the extension still gets the full lexical path.
        self._vss_loaded = False
        try:
            self._con.execute("INSTALL vss")
            self._con.execute("LOAD vss")
            # Enable experimental persistence of HNSW index on on-disk DBs
            self._con.execute("SET hnsw_enable_experimental_persistence = true")
            self._vss_loaded = True
        except duckdb.Error:
            pass
        # FTS index state: rebuilt lazily on the next query_fts after any
        # write (DuckDB FTS indexes are static snapshots). Staleness is
        # tracked via a DB-persisted generation counter (chunks_meta) so a
        # later connection to the same file sees earlier writes; -1 forces
        # a rebuild on first query of every new instance.
        self._fts_loaded = False
        self._fts_built_gen = -1
        self._init_schema()

    # Nullable metadata columns Domain-neutral
    # only; domain fields belong in extension columns.
    _METADATA_COLUMNS: list[tuple[str, str]] = [
        ("artifact_type", "TEXT"),
        ("heading_path", "TEXT"),
        ("language", "TEXT"),
        ("quality_tag", "TEXT"),
    ]

    # Content columns (non-filterable). raw_text is the verbatim
    # citation slice; nullable for legacy rows. embedding_model is
    # vector provenance (local storage metadata — never exported in
    # snapshots); model scoping goes through the dedicated
    # query_topk(embedding_model=...) parameter, NOT user filters.
    _CONTENT_COLUMNS: list[tuple[str, str]] = [
        ("raw_text", "TEXT"),
        ("embedding_model", "TEXT"),
    ]

    # Provenance columns (idempotent ALTER, same backfill loop as metadata).
    _PROVENANCE_COLUMNS: list[tuple[str, str]] = [
        ("host", "TEXT"),
        ("origin_repo", "TEXT"),
        ("content_sha", "TEXT"),
        ("chunk_ordinal", "INTEGER"),
        ("chunk_uid", "TEXT"),
    ]

    # Only host/origin_repo are exposed as query_topk filter keys;
    # content_sha/chunk_ordinal/chunk_uid are identity internals, not filters.
    _FILTERABLE_PROVENANCE: frozenset = frozenset({"host", "origin_repo"})

    def _init_schema(self) -> None:
        # chunks_meta first: it carries both the FTS generation counter and
        # the schema version the migration logic below depends on.
        self._con.execute(
            "CREATE TABLE IF NOT EXISTS chunks_meta "
            "(key TEXT PRIMARY KEY, value BIGINT)")
        self._con.execute(
            "INSERT INTO chunks_meta VALUES ('fts_gen', 0) "
            "ON CONFLICT DO NOTHING")

        table_exists = bool(self._con.execute(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_name = 'chunks'").fetchone())
        if not table_exists:
            self._create_chunks_v2()
            self._set_schema_version(SCHEMA_VERSION)
            # Fresh store: the constructor dim becomes the persisted
            # store dim (plan E0.6).
            self._con.execute(
                "INSERT INTO chunks_meta VALUES ('store_dim', ?) "
                "ON CONFLICT DO NOTHING", [self.dim])
        else:
            self._check_store_dim()
            # Pre-v2 DBs: relax legacy embedding NOT NULL (idempotent) and
            # backfill any missing columns before the v2 key migration.
            try:
                self._con.execute(
                    "ALTER TABLE chunks ALTER embedding DROP NOT NULL")
            except duckdb.Error:
                pass
            existing = {
                row[0] for row in self._con.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'chunks'"
                ).fetchall()
            }
            for col, sqltype in (self._METADATA_COLUMNS + self._CONTENT_COLUMNS
                                 + self._PROVENANCE_COLUMNS + self._ext_columns):
                if col not in existing:
                    self._con.execute(
                        f"ALTER TABLE chunks ADD COLUMN {col} {sqltype}"
                    )
            if self._schema_version() < SCHEMA_VERSION:
                self._migrate_to_v2()
                self._set_schema_version(SCHEMA_VERSION)

        # Host identity registry (Phase C0). local identity lives here as
        # the single row with origin='local'; imported/registered hosts get
        # origin='import'/'direct'. The host id <-> fingerprint 1:1
        # invariant is enforced by _check_host_writable on EVERY write path.
        self._con.execute("""
            CREATE TABLE IF NOT EXISTS hosts (
                host                TEXT PRIMARY KEY,
                fingerprint         TEXT NOT NULL,
                origin              TEXT NOT NULL,
                allow_direct_writes BOOLEAN NOT NULL DEFAULT FALSE,
                first_seen          TIMESTAMP NOT NULL
            )
        """)
        # Sync-stream state (plan C0.3 "manifests table"; contract §7):
        # per (host, origin_repo, direction) — direction 'export' holds the
        # producer's snapshot_seq/last content digest, 'import' holds the
        # importer's replay/audit state. Phase C1 fills the logic; the
        # durable home is created here so collision/update semantics have
        # a place to live before import exists.
        self._con.execute("""
            CREATE TABLE IF NOT EXISTS manifests (
                host                      TEXT NOT NULL,
                origin_repo               TEXT NOT NULL,
                direction                 TEXT NOT NULL,
                host_identity_fingerprint TEXT NOT NULL,
                last_seq                  BIGINT,
                last_content_digest       TEXT,
                last_exported_at          TEXT,
                last_manifest_digest      TEXT,
                last_imported_at          TIMESTAMP,
                forced_prev               TEXT,
                PRIMARY KEY (host, origin_repo, direction)
            )
        """)
        # Import-context token (set only by import_snapshot while its
        # transaction runs): the one path allowed to write a registered
        # remote host's partition.
        self._import_ctx_host: Optional[str] = None
        # HNSW index for cosine similarity; safe no-op if exists. Only
        # attempted when VSS loaded — without it, brute-force scan still
        # answers query_topk correctly.
        if self._vss_loaded:
            try:
                self._con.execute("""
                    CREATE INDEX IF NOT EXISTS chunks_hnsw
                    ON chunks USING HNSW (embedding) WITH (metric = 'cosine')
                """)
            except duckdb.Error:
                # Index creation can fail on in-memory with experimental
                # flags; brute-force scan still works for correctness.
                pass

    def _create_chunks_v2(self) -> None:
        """Fresh v2 layout: chunk_uid is the primary key (cross-host
        identity, identity.py); chunk_id is a NON-UNIQUE legacy dedup
        column — two hosts ingesting identical content share chunk_id but
        never chunk_uid."""
        self._con.execute(f"""
            CREATE TABLE chunks (
                chunk_uid     TEXT PRIMARY KEY,
                chunk_id      TEXT NOT NULL,
                source_path   TEXT NOT NULL,
                source_type   TEXT NOT NULL,
                text          TEXT NOT NULL,
                start_line    INTEGER NOT NULL,
                end_line      INTEGER NOT NULL,
                embedded_at   TIMESTAMP NOT NULL,
                embedding     FLOAT[{self.dim}],
                artifact_type TEXT, heading_path TEXT,
                language TEXT, quality_tag TEXT,
                raw_text TEXT, embedding_model TEXT,
                host TEXT, origin_repo TEXT, content_sha TEXT,
                chunk_ordinal INTEGER
            )
        """)
        for col, sqltype in self._ext_columns:
            self._con.execute(
                f"ALTER TABLE chunks ADD COLUMN {col} {sqltype}")

    def _check_store_dim(self) -> None:
        """Persisted-dim guard (plan E0.6): opening an existing DB with
        the wrong constructor dim must fail HERE with a clear message,
        not later at cast/write time. Pre-E0 DBs carry no record — the
        dim is inferred from the physical embedding column type
        (FLOAT[N]) and persisted before comparison."""
        row = self._con.execute(
            "SELECT value FROM chunks_meta WHERE key = 'store_dim'"
        ).fetchone()
        if row is None:
            col = self._con.execute(
                "SELECT data_type FROM information_schema.columns "
                "WHERE table_name = 'chunks' AND column_name = "
                "'embedding'").fetchone()
            m = _re.search(r"\[(\d+)\]", col[0]) if col else None
            if m is None:      # no inferable dim — trust constructor
                self._con.execute(
                    "INSERT INTO chunks_meta VALUES ('store_dim', ?) "
                    "ON CONFLICT DO NOTHING", [self.dim])
                return
            inferred = int(m.group(1))
            self._con.execute(
                "INSERT INTO chunks_meta VALUES ('store_dim', ?) "
                "ON CONFLICT DO NOTHING", [inferred])
            stored = inferred
        else:
            stored = int(row[0])
        if stored != self.dim:
            raise ValueError(
                f"store dim mismatch: DB persists dim={stored}, "
                f"constructor got dim={self.dim} — open this store with "
                f"dim={stored} (embedding dims cannot mix in one store)")

    def _schema_version(self) -> int:
        row = self._con.execute(
            "SELECT value FROM chunks_meta WHERE key = 'schema_version'"
        ).fetchone()
        return int(row[0]) if row else 1

    def _set_schema_version(self, v: int) -> None:
        self._con.execute(
            "INSERT INTO chunks_meta VALUES ('schema_version', ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value", [v])

    def _migrate_to_v2(self) -> None:
        """Rebuild the chunks table with chunk_uid as primary key.

        Pre-release migration policy (plan C0.2): rows without chunk_uid
        cannot be keyed — the DB must be re-ingested. Rows WITH uid are
        carried over verbatim (uid uniqueness was already index-enforced,
        so the PK swap cannot conflict)."""
        n_null = self._con.execute(
            "SELECT COUNT(*) FROM chunks WHERE chunk_uid IS NULL"
        ).fetchone()[0]
        if n_null:
            raise ValueError(
                f"schema v2 migration: {n_null} row(s) lack chunk_uid; "
                "pre-release policy is re-ingest (delete the DB and run "
                "linecite-ingest again) — see CHANGELOG")
        cols = self._con.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_name = 'chunks' ORDER BY ordinal_position"
        ).fetchall()
        try:
            self._con.execute("DROP INDEX IF EXISTS chunks_uid_uq")
        except duckdb.Error:
            pass
        col_defs = ", ".join(
            f"{name} {dtype} PRIMARY KEY" if name == "chunk_uid"
            else f"{name} {dtype}"
            for name, dtype in cols)
        names = ", ".join(name for name, _ in cols)
        self._con.execute(f"CREATE TABLE chunks_v2_tmp ({col_defs})")
        self._con.execute(
            f"INSERT INTO chunks_v2_tmp ({names}) SELECT {names} FROM chunks")
        self._con.execute("DROP TABLE chunks")
        self._con.execute("ALTER TABLE chunks_v2_tmp RENAME TO chunks")
        self._bump_fts_gen()   # FTS index must rebuild on the new table

    # ------------------------------------------------------------------
    # Host identity registry (Phase C0)

    # Contract §2.2 host grammar / §6 fingerprint format.
    _HOST_RE = _re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    _FP_RE = _re.compile(r"^[0-9a-f]{32}$")

    def _check_identity_syntax(self, host_id: str, fingerprint: str) -> None:
        if not self._HOST_RE.match(host_id):
            raise ValueError(f"invalid host id (contract §2.2): {host_id!r}")
        if not self._FP_RE.match(fingerprint):
            raise ValueError(
                f"invalid fingerprint (32 lowercase hex): {fingerprint!r}")

    def local_identity(self) -> Optional[tuple[str, str]]:
        """(host_id, fingerprint) of this store's own host, or None."""
        row = self._con.execute(
            "SELECT host, fingerprint FROM hosts WHERE origin = 'local'"
        ).fetchone()
        return (row[0], row[1]) if row else None

    def init_local_identity(self, host_id: str) -> tuple[str, str]:
        """Set this store's local host identity (immutable once set).

        The fingerprint is generated here (random 128-bit, 32 lowercase
        hex) and persisted; it is the stable machine identity that rides
        every exported manifest (contract §6)."""
        current = self.local_identity()
        if current is not None:
            if current[0] != host_id:
                raise ValueError(
                    f"local identity already set to {current[0]!r} "
                    f"(immutable); cannot re-init as {host_id!r}")
            return current
        if self._con.execute(
                "SELECT 1 FROM hosts WHERE host = ?", [host_id]).fetchone():
            raise ValueError(
                f"host {host_id!r} already registered as remote; "
                "cannot claim it as local identity")
        fp = secrets.token_hex(16)
        self._check_identity_syntax(host_id, fp)
        self._con.execute(
            "INSERT INTO hosts VALUES (?, ?, 'local', FALSE, ?)",
            [host_id, fp, datetime.now()])
        return (host_id, fp)

    def register_host(self, host_id: str, fingerprint: str, *,
                      allow_direct_writes: bool = False) -> None:
        """Register a remote host identity (import path, or — with
        allow_direct_writes=True — the test/simulation escape hatch that
        BYPASSES replay protection; contract §9)."""
        self._check_identity_syntax(host_id, fingerprint)
        row = self._con.execute(
            "SELECT fingerprint, origin FROM hosts WHERE host = ?",
            [host_id]).fetchone()
        if row is not None:
            if row[1] == "local":
                raise ValueError(
                    f"host collision: {host_id!r} is this store's local "
                    "identity; a remote host may not claim the same id")
            if row[0] != fingerprint:
                raise ValueError(
                    f"host collision: {host_id!r} already registered with "
                    "a different identity fingerprint")
            self._con.execute(
                "UPDATE hosts SET allow_direct_writes = ? WHERE host = ?",
                [allow_direct_writes, host_id])
            return
        # 1:1 both directions (contract §6): a fingerprint identifies ONE
        # machine — the same fingerprint under a second host id is a
        # mis-configured clone, not a new host.
        other = self._con.execute(
            "SELECT host FROM hosts WHERE fingerprint = ?",
            [fingerprint]).fetchone()
        if other is not None:
            raise ValueError(
                f"fingerprint collision: already bound to host "
                f"{other[0]!r}; host<->fingerprint must be 1:1")
        origin = "direct" if allow_direct_writes else "import"
        self._con.execute(
            "INSERT INTO hosts VALUES (?, ?, ?, ?, ?)",
            [host_id, fingerprint, origin, allow_direct_writes,
             datetime.now()])

    def _check_host_writable(self, host: Optional[str]) -> None:
        """Registry gate on EVERY write path (plan C0.5).

        - host == local identity          -> allowed
        - registered + import context     -> allowed (import_snapshot only)
        - registered + allow_direct_writes-> allowed (escape hatch)
        - anything else                   -> reject

        Ergonomics: the very first write to a fresh store auto-initializes
        the local identity to that host (immutable afterwards)."""
        if host is None:
            raise ValueError(
                "chunk host is required: conforming stores do not produce "
                "host-less rows (contract §9 legacy clause is read-side "
                "only)")
        local = self.local_identity()
        if local is None:
            self.init_local_identity(host)
            return
        if host == local[0]:
            return
        row = self._con.execute(
            "SELECT allow_direct_writes FROM hosts WHERE host = ?",
            [host]).fetchone()
        if row is None:
            raise ValueError(
                f"unregistered host {host!r}: writes require the local "
                "identity, an import context, or register_host(..., "
                "allow_direct_writes=True)")
        if getattr(self, "_import_ctx_host", None) == host:
            return
        if bool(row[0]):
            return
        raise ValueError(
            f"direct write to registered remote host {host!r} rejected: "
            "remote partitions are written only via import_snapshot "
            "(replay protection); use allow_direct_writes only for "
            "tests/simulation")

    def _backfill_legacy_provenance(self, repo_root=None, *,
                                    host: str,
                                    origin_repo: str) -> int:
        """In-place provenance backfill for legacy rows (host IS NULL).

        Assigns host/origin_repo, derives content_sha = sha256(text), assigns a
        per-source_path chunk_ordinal, and recomputes chunk_uid. Preserves
        existing embedding + embedded_at (no re-embed). Idempotent: rows that
        already have host are skipped.

        chunk_ordinal reconstruction:
          - When `repo_root` is given AND the file is readable, re-chunk the
            file with the SAME chunk_text() sequence ingest uses, and give each
            legacy chunk the ordinal of the matching chunk (by content_sha) in
            that fresh sequence. This makes the backfilled chunk_uid identical
            to a fresh ingest, so a later re-ingest of an UNCHANGED file stays a
            no-op (critical for windowed chunks that share a line range, where a
            line-order sort would scramble ordinals).
          - Fallback (documented, acceptable): when `repo_root` is None, the
            file is missing/unreadable, or a legacy chunk's text is not in the
            current chunk sequence (i.e. the file CHANGED), assign ordinals by
            line order. Those rows will simply re-ingest on the next pass, which
            is correct precisely because the file changed.
        """
        from collections import defaultdict, deque
        from pathlib import Path
        # Lazy import to avoid a store<->chunker import cycle at load time.
        from linecite.chunker import chunk_text
        from linecite.identity import chunk_uid as _chunk_uid
        from linecite.identity import content_sha as _content_sha

        rows = self._con.execute(
            "SELECT chunk_id, source_path, text, start_line, end_line "
            "FROM chunks WHERE host IS NULL"
        ).fetchall()
        if not rows:
            return 0
        by_path: dict[str, list] = defaultdict(list)
        for r in rows:
            by_path[r[1]].append(r)

        n = 0
        for path, group in by_path.items():
            # Canonical fresh chunk sequence for this file, if available.
            fresh_by_sha: dict[str, deque] | None = None
            fresh_max_ord = -1
            if repo_root is not None:
                try:
                    ftext = (Path(repo_root) / path).read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    ftext = None
                if ftext is not None:
                    fresh_by_sha = defaultdict(deque)
                    for ordinal, tc in enumerate(chunk_text(ftext)):
                        fresh_by_sha[_content_sha(tc.text)].append(ordinal)
                        fresh_max_ord = ordinal

            # Deterministic legacy iteration order (also the fallback order).
            group_sorted = sorted(group, key=lambda r: (r[3], r[4], r[0]))
            assignments: list[tuple[tuple, int, str]] = []
            if fresh_by_sha is not None:
                unmatched = []
                used_max = -1
                for r in group_sorted:
                    csha = _content_sha(r[2])
                    dq = fresh_by_sha.get(csha)
                    if dq:
                        ordv = dq.popleft()
                        assignments.append((r, ordv, csha))
                        used_max = max(used_max, ordv)
                    else:
                        unmatched.append(r)   # file changed → fallback
                next_ord = max(used_max, fresh_max_ord) + 1
                for r in unmatched:
                    assignments.append((r, next_ord, _content_sha(r[2])))
                    next_ord += 1
            else:
                for idx, r in enumerate(group_sorted):
                    assignments.append((r, idx, _content_sha(r[2])))

            for r, ordv, csha in assignments:
                uid = _chunk_uid(host, origin_repo, path, ordv, csha)
                self._con.execute(
                    "UPDATE chunks SET host=?, origin_repo=?, content_sha=?, "
                    "chunk_ordinal=?, chunk_uid=? WHERE chunk_id=?",
                    [host, origin_repo, csha, ordv, uid, r[0]],
                )
                n += 1
        return n

    def _insert_chunk_row(self, c: Chunk) -> None:
        """Low-level INSERT of a fully-formed chunk (all columns)."""
        cols = ["chunk_id", "source_path", "source_type", "text",
                "start_line", "end_line", "embedded_at", "embedding",
                "artifact_type", "heading_path", "language", "quality_tag",
                "host", "origin_repo", "content_sha", "chunk_ordinal",
                "chunk_uid", "raw_text", "embedding_model"]
        vals = [c.chunk_id, c.source_path, c.source_type, c.text,
                c.start_line, c.end_line, c.embedded_at, c.embedding,
                c.artifact_type, c.heading_path, c.language, c.quality_tag,
                c.host, c.origin_repo, c.content_sha, c.chunk_ordinal,
                c.chunk_uid, c.raw_text, c.embedding_model]
        ext = c.ext or {}
        for name, _ in self._ext_columns:
            cols.append(name)
            vals.append(ext.get(name))
        placeholders = ", ".join("?" * len(cols))
        self._con.execute(
            f"INSERT INTO chunks ({', '.join(cols)}) VALUES ({placeholders})",
            vals,
        )
        self._bump_fts_gen()

    def write_chunk(self, c: Chunk) -> bool:
        """Insert a single chunk (insert-only path, chunk_uid dedup).

        Returns True if new, False if duplicate. Kept for backwards compat;
        provenance-aware ingest uses upsert_snapshot() (also uid-keyed).
        """
        self._check_embedding(c)
        self._check_lines(c)
        self._check_host_writable(c.host)
        self._check_identity(c)
        if c.chunk_uid is None:
            raise ValueError(
                "write_chunk requires chunk_uid (schema v2 primary key)")
        # Dedup keys on chunk_uid (C0: chunk_id is demoted to a
        # NON-UNIQUE legacy column — two hosts holding identical content
        # share chunk_id but are distinct rows).
        existing = self._con.execute(
            "SELECT 1 FROM chunks WHERE chunk_uid = ?", [c.chunk_uid]
        ).fetchone()
        if existing:
            return False
        self._insert_chunk_row(c)
        return True

    def upsert_chunk(self, c: Chunk) -> str:
        """Insert-or-update a single chunk keyed on chunk_uid (no deletes).

        Returns 'inserted' | 'updated' | 'unchanged'.
        """
        res = self.upsert_snapshot([c], host=c.host, origin_repo=c.origin_repo,
                                   sealed=False)
        if res.inserted:
            return "inserted"
        if res.updated:
            return "updated"
        return "unchanged"

    def upsert_snapshot(self, chunks: list[Chunk], *, host: Optional[str],
                        origin_repo: Optional[str],
                        scanned_paths: Optional[Iterable[str]] = None,
                        sealed: bool = True) -> SnapshotResult:
        """Reconcile a scanned snapshot into the store, keyed on chunk_uid.

        For each incoming chunk:
          - uid absent  → INSERT.
          - uid present → MATCHED: overwrite EVERY mutable contract field
            (start/end_line, heading_path, source_type, raw_text,
            artifact_type, language, quality_tag, registered ext columns;
            incoming NULL writes NULL — snapshot is authoritative). A
            stored vector is NEVER overwritten; a NULL stored vector may
            be backfilled. No change → counted 'unchanged' (no write).

        Deletion (orphans + removed files) runs only when `sealed=True`
        (completion seal — an aborted/partial scan passes sealed=False so it
        can never trigger a destructive delete). Delete scope is the
        (host, origin_repo) partition, further narrowed to `scanned_paths`
        when given (file-scoped mode); chunks outside scope are never touched.

        Args:
            chunks: the complete chunk set for the scanned scope.
            host, origin_repo: provenance/delete-scope binding.
            scanned_paths: repo-relative paths actually scanned (file-scoped).
                None → full-partition scan (whole host/origin_repo).
            sealed: only a sealed (complete) scan may delete.
        """
        if host is None or origin_repo is None:
            raise ValueError(
                "upsert_snapshot requires host and origin_repo (the "
                "delete-scope partition)")
        self._check_host_writable(host)
        res = SnapshotResult()
        incoming_uids: set[str] = set()
        for c in chunks:
            uid = c.chunk_uid
            if uid is None:
                raise ValueError("upsert_snapshot requires chunk_uid on every chunk")
            # Partition consistency (contract §8.4): a snapshot writes
            # exactly the partition it declares.
            if c.host != host or c.origin_repo != origin_repo:
                raise ValueError(
                    f"chunk {uid} partition ({c.host!r}, {c.origin_repo!r}) "
                    f"does not match declared ({host!r}, {origin_repo!r})")
            self._check_identity(c)
            # Line contract applies to matched updates too (plan C1):
            # upsert previously revalidated lines only on the insert path.
            self._check_lines(c)
            # Pairing invariant applies to matched backfill too (E0):
            # the single enforcement point runs for EVERY incoming chunk,
            # not only inserts — otherwise backfill could write a vector
            # with NULL model.
            self._check_embedding(c)
            incoming_uids.add(uid)
            ext_names = [n for n, _ in self._ext_columns]
            ext_select = "".join(f", {n}" for n in ext_names)
            row = self._con.execute(
                f"SELECT start_line, end_line, heading_path, source_type, "
                f"raw_text, embedding IS NULL, host, origin_repo, "
                f"artifact_type, language, quality_tag{ext_select} "
                f"FROM chunks WHERE chunk_uid = ?", [uid]
            ).fetchone()
            if row is None:
                self._insert_chunk_row(c)
                res.inserted += 1
            else:
                # Partition safety: a stored row matched by uid must belong
                # to the declared partition (defense in depth on top of
                # _check_identity — a pre-existing forged row must not be
                # updatable through another partition's authorization).
                if row[6] != host or row[7] != origin_repo:
                    raise ValueError(
                        f"stored chunk {uid} belongs to partition "
                        f"({row[6]!r}, {row[7]!r}), not declared "
                        f"({host!r}, {origin_repo!r})")
                ext = c.ext or {}
                ext_vals = [ext.get(n) for n in ext_names]
                stored_embedding_null = bool(row[5])
                stored_meta = list(row[8:11])
                stored_ext = list(row[11:11 + len(ext_names)])
                meta_vals = [c.artifact_type, c.language, c.quality_tag]
                # Vector backfill: a matched lexical row (stored embedding
                # NULL) accepts an incoming vector; a stored vector is
                # NEVER overwritten (no silent re-embed).
                backfill_vec = (stored_embedding_null
                                and c.embedding is not None)
                if backfill_vec and len(c.embedding) != self.dim:
                    raise ValueError(f"embedding must be length {self.dim}")
                # Matched update overwrites EVERY mutable contract field
                # (contract §8: snapshot is authoritative; NULL writes
                # NULL — no keep-old-on-missing path).
                changed = (row[0] != c.start_line or row[1] != c.end_line
                           or row[2] != c.heading_path or row[3] != c.source_type
                           or row[4] != c.raw_text
                           or stored_meta != meta_vals
                           or stored_ext != ext_vals
                           or backfill_vec)
                if changed:
                    ext_set = "".join(f", {n}=?" for n in ext_names)
                    # Provenance rides the vector: backfill writes model
                    # together with the embedding (pairing invariant).
                    vec_set = (", embedding=?, embedded_at=?, "
                               "embedding_model=?" if backfill_vec else "")
                    vec_vals = ([c.embedding, c.embedded_at,
                                 c.embedding_model]
                                if backfill_vec else [])
                    self._con.execute(
                        f"UPDATE chunks SET start_line=?, end_line=?, "
                        f"heading_path=?, source_type=?, raw_text=?, "
                        f"artifact_type=?, language=?, quality_tag=?"
                        f"{ext_set}{vec_set} WHERE chunk_uid=?",
                        [c.start_line, c.end_line, c.heading_path,
                         c.source_type, c.raw_text, *meta_vals, *ext_vals,
                         *vec_vals, uid],
                    )
                    self._bump_fts_gen()
                    res.updated += 1
                else:
                    res.unchanged += 1

        if sealed:
            scope = "host = ? AND origin_repo = ?"
            params: list = [host, origin_repo]
            if scanned_paths is not None:
                paths = list(scanned_paths)
                if paths:
                    ph = ",".join("?" * len(paths))
                    scope += f" AND source_path IN ({ph})"
                    params.extend(paths)
                else:
                    scope += " AND 1 = 0"   # empty scan → nothing in scope
            in_scope = self._con.execute(
                f"SELECT chunk_uid FROM chunks WHERE {scope}", params
            ).fetchall()
            for (uid,) in in_scope:
                if uid not in incoming_uids:
                    self._con.execute(
                        "DELETE FROM chunks WHERE chunk_uid = ?", [uid])
                    self._bump_fts_gen()
                    res.deleted += 1
        return res

    def _bump_fts_gen(self) -> None:
        self._con.execute(
            "UPDATE chunks_meta SET value = value + 1 WHERE key = 'fts_gen'")

    def _fts_current_gen(self) -> int:
        row = self._con.execute(
            "SELECT value FROM chunks_meta WHERE key = 'fts_gen'").fetchone()
        return int(row[0]) if row else 0

    def _check_identity(self, c: Chunk) -> None:
        """Contract §4 at the write boundary: identity is COMPUTED, not
        declared. Without this, a forged chunk_uid naming another host's
        row would slip past the §9 registry gate (write authorized for
        h1 must not be able to touch h2's partition)."""
        from linecite.identity import (chunk_id as _cid, chunk_uid as _cuid,
                                       content_sha as _csha)
        # Identity inputs are MANDATORY at the write boundary: tolerating
        # None here would let a caller omit content_sha/ordinal and slip an
        # arbitrary foreign chunk_uid past the recompute (round-3 blocker).
        missing = [f for f in ("host", "origin_repo", "content_sha",
                               "chunk_ordinal", "chunk_uid")
                   if getattr(c, f) is None]
        if missing:
            raise ValueError(
                f"identity fields required at write boundary "
                f"(contract §4), missing: {', '.join(missing)}")
        if c.content_sha != _csha(c.text):
            raise ValueError(
                f"content_sha mismatch for {c.source_path}: identity "
                "fields must be computed from text (contract §4)")
        if c.chunk_id != _cid(c.source_path, c.text):
            raise ValueError(
                f"chunk_id mismatch for {c.source_path} (contract §4)")
        if c.chunk_uid != _cuid(c.host, c.origin_repo, c.source_path,
                                c.chunk_ordinal, c.content_sha):
            raise ValueError(
                f"chunk_uid mismatch for {c.source_path} (contract §4: "
                "uid derives from host/repo/path/ordinal/content_sha)")

    def _check_lines(self, c: Chunk) -> None:
        """Citation contract at the write boundary: line anchors must be
        a valid 1-indexed inclusive range."""
        if c.start_line < 1 or c.end_line < c.start_line:
            raise ValueError(
                f"invalid citation line range: start_line={c.start_line}, "
                f"end_line={c.end_line}")

    def _check_embedding(self, c: Chunk) -> None:
        """Embedding is optional (lexical mode) unless require_embedding;
        when present it must match the store dimension. Single
        write-boundary enforcement point for the provenance pairing
        invariant (NEW writes): embedding <=> embedding_model."""
        if c.embedding is None:
            if c.embedding_model is not None:
                raise ValueError(
                    "embedding_model without embedding: provenance rides "
                    "the vector (pairing invariant)")
            if self.require_embedding:
                raise ValueError(
                    f"store requires embedding of length {self.dim} "
                    f"(require_embedding=True)")
            return
        if c.embedding_model is None:
            raise ValueError(
                "embedding without embedding_model: new vectors must "
                "carry provenance (pairing invariant; legacy NULL-model "
                "rows are read-side quarantine only)")
        if len(c.embedding) != self.dim:
            raise ValueError(f"embedding must be length {self.dim}")

    def count(self) -> int:
        return self._con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]

    def reembed(self, embedder, *, filters: Optional[dict] = None,
                batch_size: int = 64) -> int:
        """Re-embed chunks in scope with `embedder` — the ONE sanctioned
        path that may overwrite stored vectors ("never SILENTLY
        re-embedded": explicit migration is allowed, silent overwrite is
        not). Covers model switches, legacy NULL-model reclaim, and
        first-time embedding of lexical rows.

        Contract §9 exemption: updates ONLY local storage metadata
        (embedding / embedded_at / embedding_model), so it MAY run over
        imported remote partitions and MUST NOT touch contract fields
        or manifests state (regression-tested).

        embedder: Embedder protocol — .embed(texts), .model, .dim.
        filters: same columns as query_topk filters (host, origin_repo,
        source_path, metadata, registered ext columns).
        Returns the number of rows re-embedded.
        """
        if embedder.dim != self.dim:
            raise ValueError(
                f"embedder dim {embedder.dim} != store dim {self.dim}")
        model = getattr(embedder, "model", None)
        if not isinstance(model, str) or not model:
            raise ValueError(
                "embedder.model must be a non-empty string — reembed "
                "writes ride the pairing invariant too")
        filter_sql, params = self._build_filter_sql(filters)
        where = f"WHERE {filter_sql}" if filter_sql else ""
        rows = self._con.execute(
            f"SELECT chunk_uid, text FROM chunks {where} "
            "ORDER BY chunk_uid", params).fetchall()
        n = 0
        now = datetime.now()
        for i in range(0, len(rows), batch_size):
            batch = rows[i:i + batch_size]
            vectors = embedder.embed([t for _, t in batch])
            if len(vectors) != len(batch):
                raise ValueError(
                    f"embedder returned {len(vectors)} vectors for "
                    f"{len(batch)} texts")
            for (uid, _), vec in zip(batch, vectors):
                if len(vec) != self.dim:
                    raise ValueError(
                        f"embedding must be length {self.dim}")
                self._con.execute(
                    "UPDATE chunks SET embedding=?, embedded_at=?, "
                    "embedding_model=? WHERE chunk_uid=?",
                    [vec, now, model, uid])
                n += 1
        return n

    def query_topk(self, vector: list[float], k: int = 5,
                   filters: Optional[dict] = None, *,
                   embedding_model: Optional[str] = None,
                   allow_mixed_embedding_models: bool = False
                   ) -> list[tuple[Chunk, float]]:
        """Return k nearest chunks by cosine distance, with optional metadata filters.

        Score = 1 - cosine_distance (higher = more similar). Results sorted
        descending by score.

        filters: optional dict of metadata column -> value (or list of values).
            Supported: the core metadata columns (artifact_type, language,
            quality_tag, heading_path), source_type, source_path, the
            provenance keys host, origin_repo, plus any registered
            extension columns. Applied as SQL WHERE clause BEFORE scoring
            so we only score filtered candidates (enables path-filter fast
            path for scoped queries).
        """
        if len(vector) != self.dim:
            raise ValueError(f"query vector must be length {self.dim}")
        # Model-space isolation (hard rule): cosine across different
        # embedding models is meaningless, so the caller must either
        # name the model space or explicitly opt into mixing. NULL-model
        # legacy vectors are excluded from model-scoped queries.
        if embedding_model is None and not allow_mixed_embedding_models:
            raise ValueError(
                "query_topk requires embedding_model= (the model the "
                "query vector came from), or "
                "allow_mixed_embedding_models=True to opt into "
                "cross-model comparison")
        filter_sql, params = self._build_filter_sql(filters)
        # Nullable embeddings (lexical rows) must not reach the scorer:
        # array_cosine_similarity(NULL, v) is NULL and would crash result
        # materialization. Vector search only sees embedded rows.
        vec_guard = "embedding IS NOT NULL"
        model_params: list = []
        if embedding_model is not None:
            vec_guard += " AND embedding_model = ?"
            model_params = [embedding_model]
        where_sql = (f"WHERE {vec_guard} AND ({filter_sql})"
                     if filter_sql else f"WHERE {vec_guard}")
        # Parameter order must match ? positions left-to-right in the SQL:
        # (1) vector for array_cosine_similarity in SELECT, (2) WHERE clause
        # params, (3) LIMIT.
        ext_names = [c for c, _ in self._ext_columns]
        ext_select = "".join(f", {c}" for c in ext_names)
        rows = self._con.execute(f"""
            SELECT {", ".join(self._BASE_COLS)}
                   {ext_select},
                   array_cosine_similarity(embedding, ?::FLOAT[{self.dim}])
                       AS score
            FROM chunks
            {where_sql}
            ORDER BY score DESC, chunk_uid ASC
            LIMIT ?
        """, [vector, *model_params, *params, k]).fetchall()
        return [self._row_to_scored_chunk(r, ext_names) for r in rows]

    # Column order shared by every retrieval SELECT; _row_to_scored_chunk
    # depends on this exact order (score comes last, after extensions).
    _BASE_COLS: tuple = (
        "chunk_id", "source_path", "source_type", "text",
        "start_line", "end_line", "embedded_at", "embedding",
        "artifact_type", "heading_path", "language", "quality_tag",
        "host", "origin_repo", "content_sha", "chunk_ordinal", "chunk_uid",
        "raw_text", "embedding_model")

    def _row_to_scored_chunk(self, r, ext_names: list[str]
                             ) -> tuple[Chunk, float]:
        n_base = len(self._BASE_COLS)
        ext = ({name: r[n_base + i] for i, name in enumerate(ext_names)}
               if ext_names else None)
        c = Chunk(
            chunk_id=r[0], source_path=r[1], source_type=r[2],
            text=r[3], start_line=r[4], end_line=r[5],
            embedded_at=r[6],
            embedding=list(r[7]) if r[7] is not None else None,
            artifact_type=r[8], heading_path=r[9],
            language=r[10], quality_tag=r[11],
            host=r[12], origin_repo=r[13], content_sha=r[14],
            chunk_ordinal=r[15], chunk_uid=r[16], raw_text=r[17],
            embedding_model=r[18],
            ext=ext,
        )
        return (c, float(r[n_base + len(ext_names)]))

    def _build_filter_sql(self, filters: Optional[dict]
                          ) -> tuple[str, list]:
        """Shared filter validation + WHERE-clause builder (no leading
        WHERE keyword; caller decides how to splice)."""
        allowed = ({c for c, _ in self._METADATA_COLUMNS}
                   | {"source_type", "source_path"}
                   | self._FILTERABLE_PROVENANCE
                   | {c for c, _ in self._ext_columns})
        clauses, params = [], []
        for col, val in (filters or {}).items():
            if col not in allowed:
                raise ValueError(f"unknown filter column: {col}")
            if isinstance(val, (list, tuple)):
                if not val:
                    continue
                placeholders = ",".join("?" * len(val))
                clauses.append(f"{col} IN ({placeholders})")
                params.extend(val)
            else:
                clauses.append(f"{col} = ?")
                params.append(val)
        return (" AND ".join(clauses), params)

    def query_fts(self, query: str, k: int = 5,
                  filters: Optional[dict] = None
                  ) -> list[tuple[Chunk, float]]:
        """Lexical top-k over chunk text via DuckDB FTS (BM25). The no-key
        default retrieval path: no embeddings, no network, no API key.

        The FTS index is a static snapshot — it is (re)built lazily on the
        first query after any write. Score = BM25 (higher = better); rows
        with no term overlap are excluded.
        """
        if not query or not query.strip():
            raise ValueError("query must be non-empty")
        if k <= 0:
            raise ValueError("k must be > 0")
        if not self._fts_loaded:
            try:
                self._con.execute("INSTALL fts")
                self._con.execute("LOAD fts")
                self._fts_loaded = True
            except duckdb.Error as e:
                raise RuntimeError(
                    f"DuckDB fts extension unavailable: {e}") from e
        cur_gen = self._fts_current_gen()
        if self._fts_built_gen != cur_gen:
            # FTS document id is chunk_uid (schema v2): chunk_id is not
            # unique across hosts, so BM25 must key on the true PK.
            self._con.execute(
                "PRAGMA create_fts_index('chunks', 'chunk_uid', 'text', "
                "overwrite=1)")
            self._fts_built_gen = cur_gen
        ext_names = [c for c, _ in self._ext_columns]
        ext_select = "".join(f", {c}" for c in ext_names)
        filter_sql, params = self._build_filter_sql(filters)
        and_filter = f"WHERE {filter_sql}" if filter_sql else ""
        rows = self._con.execute(f"""
            SELECT * FROM (
                SELECT {", ".join(self._BASE_COLS)}
                       {ext_select},
                       fts_main_chunks.match_bm25(chunk_uid, ?) AS score
                FROM chunks
                {and_filter}
            ) WHERE score IS NOT NULL
            ORDER BY score DESC, chunk_uid ASC
            LIMIT ?
        """, [query, *params, k]).fetchall()
        return [self._row_to_scored_chunk(r, ext_names) for r in rows]

    def close(self) -> None:
        self._con.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
