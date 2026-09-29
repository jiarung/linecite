"""Generic line-window ingester + CLI (core, package-neutral).

The public ingest path : walk a
directory for text files, chunk with the heading/line-window chunker,
and reconcile into a ChunkStore via provenance-keyed upsert. Imports
core modules only; no OpenAI, no API key — chunks are written without
embeddings and served by the lexical (FTS) retrieval path. A deployment
that wants vectors passes an `embed_fn`.

CLI:
    python3 -m linecite.generic_ingester <root> --db kb.duckdb \
        [--glob '**/*.md' --glob '**/*.txt'] [--source-type doc] \
        [--host myhost] [--origin-repo myrepo] [--dim 1536]
"""
from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Optional

from linecite.chunker import chunk_text
from linecite.identity import chunk_id, chunk_uid, content_sha
from linecite.store import Chunk, ChunkStore, SnapshotResult

DEFAULT_GLOBS = ("**/*.md", "**/*.txt")


def _validate_globs(globs: Iterable[str]) -> tuple[str, ...]:
    """Reject glob patterns that could escape the ingest root."""
    out = []
    for g in globs:
        if Path(g).is_absolute() or ".." in Path(g).parts:
            raise ValueError(f"glob must be relative and stay under root: {g!r}")
        out.append(g)
    return tuple(out)


def ingest_paths(store: ChunkStore, repo_root: Path | str, *,
                 host: str, origin_repo: str,
                 globs: Iterable[str] = DEFAULT_GLOBS,
                 source_type: str = "doc",
                 embedder=None,
                 embed_fn: Optional[Callable[[list[str]], list[list[float]]]] = None,
                 embedding_model: Optional[str] = None,
                 scope: str = "full",
                 ) -> SnapshotResult:
    """Walk `repo_root` for files matching `globs`, chunk, and reconcile
    into `store` (provenance-keyed upsert).

    scope:
      "full" (default) — the scan declares the ENTIRE (host, origin_repo)
        partition: chunks of previously-indexed files that no longer exist
        are deleted. One partition == one ingest root.
      "files" — deletes are scoped to the files seen this scan only
        (chunks of removed files survive; use for partial ingests).

    Safety: the scan is sealed (deletes allowed) only when every matched
    file was read successfully — a read/decode failure downgrades to an
    unsealed scan so a transient error can never mass-delete
    (see the sealed-scan safety tests).

    Embedding (preferred): pass `embedder` — an object with
    `.embed(texts) -> vectors`, `.model: str`, `.dim: int` (the Embedder
    protocol). Legacy: `embed_fn` (bare callable) is still accepted but
    MUST be paired with `embedding_model=` — vectors without provenance
    are rejected at the write boundary. None (default) writes
    embedding-less chunks for the lexical path. Vectors are applied only
    to chunks not already embedded; stored vectors are never silently
    re-embedded (explicit migration: ChunkStore.reembed).
    """
    if scope not in ("full", "files"):
        raise ValueError(f"scope must be 'full' or 'files', got {scope!r}")
    if embedder is not None:
        if embed_fn is not None:
            raise ValueError("pass embedder= or embed_fn=, not both")
        embed_fn = embedder.embed
        embedding_model = embedder.model
    elif embed_fn is not None and embedding_model is None:
        raise ValueError(
            "embed_fn requires embedding_model= (provenance pairing "
            "invariant): a bare callable carries no model identity — "
            "or pass an embedder object instead")
    root = Path(repo_root).resolve()
    globs = _validate_globs(globs)
    candidates = sorted({p for g in globs for p in root.glob(g)
                         if p.is_file()})
    now = datetime.now()
    chunks: list[Chunk] = []
    scanned_rel: list[str] = []
    unreadable = 0
    for p in candidates:
        # Symlinks (or anything else) resolving outside root are skipped:
        # the citation contract anchors to paths under the ingest root.
        try:
            resolved = p.resolve()
            resolved.relative_to(root)
        except (OSError, ValueError):
            continue
        rel = p.relative_to(root).as_posix()
        try:
            text = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            unreadable += 1
            continue
        scanned_rel.append(rel)
        for ordinal, tc in enumerate(chunk_text(text)):
            csha = content_sha(tc.text)
            chunks.append(Chunk(
                chunk_id=chunk_id(rel, tc.text),
                source_path=rel,
                source_type=source_type,
                text=tc.text,
                start_line=tc.start_line,
                end_line=tc.end_line,
                embedded_at=now,
                embedding=None,
                raw_text=tc.raw_text,
                heading_path=tc.heading_path,
                host=host,
                origin_repo=origin_repo,
                content_sha=csha,
                chunk_ordinal=ordinal,
                chunk_uid=chunk_uid(host, origin_repo, rel, ordinal, csha),
            ))
    if embed_fn is not None and chunks:
        import dataclasses
        # Model-aware embed skip (plan E0.4): any stored vector — same
        # model (reusable), different model (untouched pending explicit
        # reembed), or NULL-model legacy (quarantined) — is left alone;
        # only vectorless chunks get embedded. Wrong-model rows are kept
        # out of results by query-side model isolation, never by silent
        # re-embedding here.
        embedded_uids: set = set()
        if scanned_rel:
            ph = ",".join("?" * len(scanned_rel))
            rows = store._con.execute(
                f"SELECT chunk_uid FROM chunks WHERE host = ? AND "
                f"origin_repo = ? AND source_path IN ({ph}) AND "
                f"embedding IS NOT NULL",
                [host, origin_repo, *scanned_rel]).fetchall()
            embedded_uids = {r[0] for r in rows}
        new_idx = [i for i, c in enumerate(chunks)
                   if c.chunk_uid not in embedded_uids]
        if new_idx:
            vectors = embed_fn([chunks[i].text for i in new_idx])
            if len(vectors) != len(new_idx):
                raise ValueError(
                    f"embed_fn returned {len(vectors)} vectors for "
                    f"{len(new_idx)} texts")
            for i, v in zip(new_idx, vectors):
                chunks[i] = dataclasses.replace(
                    chunks[i], embedding=v,
                    embedding_model=embedding_model)
    sealed = unreadable == 0
    return store.upsert_snapshot(
        chunks, host=host, origin_repo=origin_repo,
        scanned_paths=(None if scope == "full" else scanned_rel),
        sealed=sealed)


def _resolve_dim(db_path: str, cli_dim: Optional[int]) -> int:
    """CLI dim resolution (plan E0.6): an existing DB's persisted dim
    wins when --dim is omitted; --dim given still goes through the
    store's open-time mismatch check. New stores default to 1536."""
    if cli_dim is not None:
        return cli_dim
    if db_path != ":memory:" and Path(db_path).exists():
        import duckdb
        con = duckdb.connect(db_path, read_only=True)
        try:
            row = con.execute(
                "SELECT value FROM chunks_meta WHERE key = 'store_dim'"
            ).fetchone()
            if row:
                return int(row[0])
            col = con.execute(
                "SELECT data_type FROM information_schema.columns "
                "WHERE table_name = 'chunks' AND column_name = "
                "'embedding'").fetchone()
            if col:
                import re
                m = re.search(r"\[(\d+)\]", col[0])
                if m:
                    return int(m.group(1))
        except duckdb.Error:
            pass
        finally:
            con.close()
    return 1536


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="linecite-ingest",
        description="Ingest a directory of text files into a linecite "
                    "chunk store (lexical, no API key).")
    ap.add_argument("root", help="directory to ingest")
    ap.add_argument("--db", required=True, help="DuckDB store path")
    ap.add_argument("--glob", action="append", default=None,
                    help="glob pattern (repeatable; default **/*.md, **/*.txt)")
    ap.add_argument("--source-type", default="doc")
    ap.add_argument("--host", default="local")
    ap.add_argument("--origin-repo", default=None,
                    help="default: basename of root")
    ap.add_argument("--embed", choices=["none", "local"], default="none",
                    help="'local' embeds with the bundled local model "
                         "(requires the local-embeddings extra; no API "
                         "key). Default: lexical-only ingest.")
    ap.add_argument("--model-path", default=None,
                    help="local path to a pre-downloaded embedding "
                         "model (offline use; implies --embed local). "
                         "Must hold a copy of the default model unless "
                         "--model-id names what it actually is.")
    ap.add_argument("--model-id", default=None,
                    help="model identity stamped into embedding "
                         "provenance when --model-path holds a "
                         "non-default model")
    ap.add_argument("--dim", type=int, default=None,
                    help="embedding dimension. Default: the store's "
                         "persisted dim for an existing DB (inferred "
                         "from the embedding column on pre-E0 DBs); "
                         "1536 for a new store.")
    ap.add_argument("--scope", choices=["full", "files"], default=None,
                    help="delete-reconciliation scope. Default: 'full' "
                         "when using the default globs (the root owns its "
                         "whole host/origin-repo partition); 'files' when "
                         "--glob is given (a subset scan must not delete "
                         "chunks outside its globs). Override explicitly "
                         "when you know the partition layout.")
    args = ap.parse_args(argv)

    root = Path(args.root)
    if not root.is_dir():
        ap.error(f"not a directory: {root}")
    origin = args.origin_repo or root.resolve().name
    scope = args.scope or ("files" if args.glob else "full")
    embedder = None
    if args.model_id and not (args.embed == "local" or args.model_path):
        ap.error("--model-id only applies with --embed local or "
                 "--model-path (provenance flags must not silently "
                 "no-op on a lexical ingest)")
    if args.embed == "local" or args.model_path:
        from linecite.local_embedder import DEFAULT_LOCAL_MODEL, LocalEmbedder
        try:
            embedder = LocalEmbedder(
                model_id=args.model_id or DEFAULT_LOCAL_MODEL,
                model_path=args.model_path)
        except (ImportError, RuntimeError) as e:
            ap.error(str(e))
        if args.dim is not None:
            ap.error(f"--dim conflicts with --embed local: the "
                     f"dimension is taken from the model "
                     f"(dim={embedder.dim}) — omit --dim")
    dim = (embedder.dim if embedder is not None and args.dim is None
           and not Path(args.db).exists()
           else _resolve_dim(args.db, args.dim))
    try:
        store = ChunkStore(args.db, dim=dim)
    except ValueError as e:
        # Open-time dim mismatch (E0 guard) surfaces as CLI guidance,
        # not a stack trace — reachable e.g. with --dim equal to the
        # local model on a store built for another dimension.
        ap.error(f"{e} (use ChunkStore.reembed to migrate, or a new "
                 "--db)")
    if embedder is not None and store.dim != embedder.dim:
        store.close()
        ap.error(f"store dim {store.dim} != local model dim "
                 f"{embedder.dim}: this store was built for a "
                 "different embedding space (use ChunkStore.reembed "
                 "to migrate, or a new --db)")
    try:
        stats = ingest_paths(
            store, root, host=args.host, origin_repo=origin,
            globs=tuple(args.glob) if args.glob else DEFAULT_GLOBS,
            source_type=args.source_type, scope=scope,
            embedder=embedder)
    except (ValueError, NotImplementedError) as e:
        ap.error(str(e))
    finally:
        store.close()
    print(f"inserted={stats.inserted} updated={stats.updated} "
          f"unchanged={stats.unchanged} deleted={stats.deleted}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
