"""Token-window chunker for RAG corpus, with heading-path awareness .

R1 → R2 upgrade :
- Primary strategy for markdown: split on ##/### heading boundaries so each
  chunk is semantically coherent (one subsection per chunk). Each chunk
  carries its `heading_path` ("H1 > H2 > H3") for metadata-in-embedding
  context hinting.
- Token-window remains the fallback for sections that exceed budget or for
  non-markdown content (yaml, unstructured text).

Pure function. No I/O, no network, no state. Token = whitespace-split word
(cheap proxy; can be swapped for tiktoken later without changing the API).
Returns line-range citations so retrieval can cite `source_path:L<start>-<end>`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional


DEFAULT_TOKENS = 512
DEFAULT_OVERLAP = 50

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")


@dataclass(frozen=True)
class TextChunk:
    start_line: int                     # 1-indexed, inclusive
    end_line: int                       # 1-indexed, inclusive
    text: str                           # search text (may be normalized)
    heading_path: Optional[str] = None  # "H1 > H2 > H3" for md chunks
    # Verbatim slice "\n".join(lines[start_line-1:end_line]) — the citation
    # contract's ground truth; validate_citation compares it to the live
    # file. `text` stays the (possibly whitespace-joined) search input.
    raw_text: Optional[str] = None


def _tokenize(text: str) -> list[tuple[int, str]]:
    """Return [(line_no_1based, token), ...] using whitespace split."""
    out: list[tuple[int, str]] = []
    for i, line in enumerate(text.splitlines(), start=1):
        for tok in line.split():
            out.append((i, tok))
    return out


def _window_chunks(toks: list[tuple[int, str]],
                   tokens: int, overlap: int,
                   heading_path: Optional[str] = None,
                   lines: Optional[list[str]] = None) -> list[TextChunk]:
    """Token-window fallback. Returns list of TextChunk over the given tokens.

    `lines` (full source, 0-indexed) supplies the verbatim raw_text slice."""
    if not toks:
        return []
    step = tokens - overlap
    chunks: list[TextChunk] = []
    i = 0
    n = len(toks)
    while i < n:
        window = toks[i : i + tokens]
        start_line = window[0][0]
        end_line = window[-1][0]
        chunk_text_str = " ".join(t[1] for t in window)
        raw = ("\n".join(lines[start_line - 1:end_line])
               if lines is not None else None)
        chunks.append(TextChunk(
            start_line=start_line,
            end_line=end_line,
            text=chunk_text_str,
            heading_path=heading_path,
            raw_text=raw,
        ))
        if i + tokens >= n:
            break
        i += step
    return chunks


def _split_markdown_by_headings(
        text: str, tokens: int, overlap: int
) -> list[TextChunk]:
    """Split markdown text on heading boundaries. Falls back to window within
    any section that exceeds `tokens`."""
    lines = text.splitlines()
    # Build (line_no_1based, level, title, None) for each heading, plus the
    # boundaries as (start_line, end_line) spans between headings.
    heading_positions: list[tuple[int, int, str]] = []  # (line_no, level, title)
    for lineno, line in enumerate(lines, start=1):
        m = _HEADING_RE.match(line)
        if m:
            level = len(m.group(1))
            title = m.group(2).strip()
            heading_positions.append((lineno, level, title))

    if not heading_positions:
        return []  # signal: no headings, caller should use window fallback

    # Build sections: (start_line, end_line_exclusive, heading_stack)
    # heading_stack is the list of (level, title) from root to current.
    sections: list[tuple[int, int, list[tuple[int, str]]]] = []
    # Pre-heading content (before first heading) — treat as its own chunk
    # with heading_path=None if non-empty.
    first_heading_line = heading_positions[0][0]
    if first_heading_line > 1:
        sections.append((1, first_heading_line, []))

    stack: list[tuple[int, str]] = []
    for idx, (lineno, level, title) in enumerate(heading_positions):
        # Pop stack until top has level < current (we're entering a sub/sibling)
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, title))
        next_lineno = (
            heading_positions[idx + 1][0]
            if idx + 1 < len(heading_positions)
            else len(lines) + 1
        )
        sections.append((lineno, next_lineno, list(stack)))

    chunks: list[TextChunk] = []
    for start_line, end_line_excl, hstack in sections:
        section_lines = lines[start_line - 1 : end_line_excl - 1]
        section_text = "\n".join(section_lines).strip()
        if not section_text:
            continue
        heading_path = " > ".join(t for _, t in hstack) if hstack else None

        # Tokenize section. If under budget → single chunk for this section.
        # If over budget → token-window fallback within the section.
        toks: list[tuple[int, str]] = []
        for rel_i, ln in enumerate(section_lines):
            abs_line = start_line + rel_i
            for tok in ln.split():
                toks.append((abs_line, tok))

        if len(toks) <= tokens:
            end_line = max(start_line, end_line_excl - 1)
            chunks.append(TextChunk(
                start_line=start_line,
                end_line=end_line,
                text=section_text,
                heading_path=heading_path,
                raw_text="\n".join(lines[start_line - 1:end_line]),
            ))
        else:
            chunks.extend(_window_chunks(toks, tokens, overlap, heading_path,
                                         lines=lines))
    return chunks


def chunk_text(text: str,
               tokens: int = DEFAULT_TOKENS,
               overlap: int = DEFAULT_OVERLAP) -> list[TextChunk]:
    """Split text into chunks. Heading-aware for markdown, window-based
    fallback for everything else.

    Returns at least 1 chunk for any non-empty text with tokens. Empty → [].
    """
    if tokens <= 0:
        raise ValueError("tokens must be > 0")
    if overlap < 0 or overlap >= tokens:
        raise ValueError("0 <= overlap < tokens required")

    if not text.strip():
        return []

    # try heading-aware first (markdown). If no headings, fall back to
    # pure token window (preserves pre-R2 behavior for non-md content).
    md_chunks = _split_markdown_by_headings(text, tokens, overlap)
    if md_chunks:
        return md_chunks

    toks = _tokenize(text)
    if not toks:
        return []
    return _window_chunks(toks, tokens, overlap, heading_path=None,
                          lines=text.splitlines())
