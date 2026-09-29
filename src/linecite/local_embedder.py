"""Local embedding adapter (Phase E1) — semantic retrieval, still no
API key.

Backed by model2vec static embeddings (pure numpy, no torch, ~30MB
model, millisecond CPU inference). Implements the Embedder protocol
(`embed(texts) -> vectors`, `.model`, `.dim`) so it drops into
`ingest_paths(embedder=...)`, `Retriever`, and `ChunkStore.reembed`.

Install: pip install 'linecite[local-embeddings]'

Offline note (honest, mirrors the README's DuckDB-extension note): the
model is downloaded from Hugging Face ONCE on first use and cached;
fully offline machines should pre-download or pass `model_path=` to a
local copy. This is a one-time model fetch, not a runtime service —
queries and ingest never touch the network.
"""
from __future__ import annotations

from typing import Optional

DEFAULT_LOCAL_MODEL = "minishlab/potion-base-8M"

_INSTALL_HINT = (
    "local embeddings require the optional extra: "
    "pip install 'linecite[local-embeddings]'")


class LocalEmbedder:
    """model2vec-backed Embedder-protocol implementation."""

    def __init__(self, model_id: str = DEFAULT_LOCAL_MODEL,
                 model_path: Optional[str] = None):
        """model_id is the model's IDENTITY (stamped into
        embedding_model provenance); model_path is only an alternate
        LOAD SOURCE for offline use and must contain a copy of
        model_id — pass a matching model_id when loading a different
        local model, or vectors from unrelated local directories could
        silently share a provenance name."""
        try:
            from model2vec import StaticModel
        except ImportError as e:
            raise ImportError(f"{_INSTALL_HINT} (import failed: {e})") from e
        source = model_path or model_id
        try:
            # Newer model2vec defaults force_download=True — without
            # opting out, every construction re-downloads instead of
            # using the one-time HF cache the README promises. Older
            # releases lack the kwarg (and never force-downloaded), so
            # feature-detect instead of pinning a guessed version.
            import inspect
            kwargs = {}
            if "force_download" in inspect.signature(
                    StaticModel.from_pretrained).parameters:
                kwargs["force_download"] = False
            self._model = StaticModel.from_pretrained(source, **kwargs)
        except Exception as e:
            if model_path:
                raise RuntimeError(
                    f"failed to load local embedding model from "
                    f"model_path={model_path!r}: {type(e).__name__}: {e}. "
                    "Check the path holds a pre-downloaded model2vec "
                    "model directory.") from e
            raise RuntimeError(
                f"failed to load local embedding model {model_id!r}: "
                f"{type(e).__name__}: {e}. First use downloads the model "
                "from Hugging Face once and caches it — offline machines "
                "should pre-download it or pass model_path= to a local "
                "copy.") from e
        self.model = f"model2vec/{model_id}"
        # Static models are deterministic; probe once for the dimension.
        probe = self._model.encode(["dimension probe"])
        self.dim = int(probe.shape[1])

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Batch-embed (single encode call — no per-text loop)."""
        if not texts:
            return []
        arr = self._model.encode(list(texts))
        return [[float(x) for x in row] for row in arr]
