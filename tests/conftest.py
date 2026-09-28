"""Shared test helpers."""
from __future__ import annotations

import hashlib


def det_vec(text: str, dim: int) -> list[float]:
    """Deterministic pseudo-embedding derived from text (stable across runs)."""
    h = hashlib.sha256(text.encode()).digest()
    return [h[i % len(h)] / 255.0 for i in range(dim)]
