"""Canonical chunk identity helpers (core).

Single source of truth for the three computed identity keys — every
ingest path and the snapshot import pipeline recompute through these
exact functions (contract §4: identity is computed, never declared).

content_sha is the change-detection key; chunk_uid is the 128-bit global
identity (dedup + upsert key) for the cross-host shared index. See
docs/design/snapshot-contract.md.
"""
from __future__ import annotations

import hashlib


def chunk_id(source_path: str, text: str) -> str:
    """Legacy per-row dedup key (path + text hash, 64-bit hex)."""
    return hashlib.sha256(f"{source_path}\n{text}".encode()).hexdigest()[:16]


def content_sha(text: str) -> str:
    """Full sha256 hex of chunk text — change-detection key."""
    return hashlib.sha256(text.encode()).hexdigest()


def chunk_uid(host: str, origin_repo: str, source_path: str,
              chunk_ordinal: int, content_sha: str) -> str:
    """128-bit global chunk identity (dedup + upsert key)."""
    key = (f"{host}\0{origin_repo}\0{source_path}\0"
           f"{chunk_ordinal}\0{content_sha}")
    return hashlib.sha256(key.encode()).hexdigest()[:32]
