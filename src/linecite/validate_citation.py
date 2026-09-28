"""Verbatim citation validator.

A chunk's citation `source_path:L<start>-<end>` is *verifiable*: `raw_text`
holds the exact "\n".join of those source lines at ingest time. This module
compares raw_text against the live file and MARKS failures — it never
raises for a stale citation, because retrieval must keep working while the
caller decides what a stale citation means (re-ingest, warn, drop).

Reasons (closed enum — contract §9):
    source_missing     file no longer exists (or unreadable)
    content_drift      file changed; cited lines no longer match raw_text
    no_raw_text        legacy row without a verbatim slice — cannot
                       verify (treat as unverified)
    remote_unverified  chunk belongs to another host: its source file does
                       not live on this machine, so liveness cannot be
                       checked here (Phase C2; raw_text is still verbatim
                       as-of-export). Only returned when the caller passes
                       local_host.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from linecite.store import Chunk


@dataclass(frozen=True)
class CitationCheck:
    valid: bool
    # source_missing | content_drift | no_raw_text | remote_unverified
    reason: Optional[str] = None


def validate_verbatim(chunk: Chunk, repo_root: Path | str, *,
                      local_host: Optional[str] = None) -> CitationCheck:
    """Compare chunk.raw_text to the live slice of its source file.

    local_host: this store's own host id (authoritative source: the
    store's persisted local identity, C0). When given and the chunk
    belongs to a DIFFERENT host, the file is not on this machine —
    return `remote_unverified` WITHOUT touching the filesystem. When
    omitted, behavior is unchanged (single-host semantics). Legacy rows
    (chunk.host is None — defensive only; conforming stores don't
    produce them) keep local validation semantics either way.
    """
    if (local_host is not None and chunk.host is not None
            and chunk.host != local_host):
        return CitationCheck(False, "remote_unverified")
    if chunk.raw_text is None:
        return CitationCheck(False, "no_raw_text")
    path = Path(repo_root) / chunk.source_path
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return CitationCheck(False, "source_missing")
    lines = text.splitlines()
    # Impossible / out-of-file ranges are drift, not "fresh": Python slice
    # clamping would otherwise let start-beyond-EOF with raw_text == ""
    # validate as clean.
    if (chunk.start_line < 1 or chunk.end_line < chunk.start_line
            or chunk.start_line > len(lines)
            or chunk.end_line > len(lines)):
        return CitationCheck(False, "content_drift")
    live = "\n".join(lines[chunk.start_line - 1:chunk.end_line])
    if live != chunk.raw_text:
        return CitationCheck(False, "content_drift")
    return CitationCheck(True, None)
