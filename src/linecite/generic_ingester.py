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
                 embed_fn: Optional[Callable[[list[str]], list[list[float]]]] = None,
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

    embed_fn: optional batch embedder `texts -> vectors`, applied only to
    chunks whose chunk_uid is not already stored (matched chunks keep
    their stored vectors — no re-embed cost). None (default) writes
    embedding-less chunks for the lexical path.
    """
    if scope not in ("full", "files"):
        raise ValueError(f"scope must be 'full' or 'files', got {scope!r}")
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
        # Embed chunks that are new OR stored without a vector (lexical
        # rows get backfilled; stored vectors are never re-embedded).
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
                chunks[i] = dataclasses.replace(chunks[i], embedding=v)
    sealed = unreadable == 0
    return store.upsert_snapshot(
        chunks, host=host, origin_repo=origin_repo,
        scanned_paths=(None if scope == "full" else scanned_rel),
        sealed=sealed)


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
    ap.add_argument("--dim", type=int, default=1536)
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
    store = ChunkStore(args.db, dim=args.dim)
    try:
        stats = ingest_paths(
            store, root, host=args.host, origin_repo=origin,
            globs=tuple(args.glob) if args.glob else DEFAULT_GLOBS,
            source_type=args.source_type, scope=scope)
    except (ValueError, NotImplementedError) as e:
        ap.error(str(e))
    finally:
        store.close()
    print(f"inserted={stats.inserted} updated={stats.updated} "
          f"unchanged={stats.unchanged} deleted={stats.deleted}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
