"""OpenAI embedding wrapper with retry + cost log.

Design notes
- DI for the client: caller can pass a mock `client` into Embedder(...) to
  avoid network in tests. Production: client=None → lazy-init real OpenAI.
- Retry policy: exponential backoff on rate limit / transient errors;
  permanent failures raise immediately.
- Cost log: every successful call appends one row to .linecite/cost_log.jsonl
  (token count + model + ts). Caller can set log_path to tmp for tests.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional


DEFAULT_MODEL = "text-embedding-3-small"
DEFAULT_DIM = 1536
DEFAULT_COST_LOG = Path(".linecite/cost_log.jsonl")


class EmbedderError(RuntimeError):
    """Permanent embedder failure (not retryable)."""


class RateLimitError(Exception):
    """Transient; caller retries."""


@dataclass
class Embedder:
    client: object = None                 # injected for tests
    model: str = DEFAULT_MODEL
    dim: int = DEFAULT_DIM
    cost_log_path: Path = DEFAULT_COST_LOG
    max_retries: int = 3
    base_backoff: float = 1.0             # seconds
    sleep_fn: Callable[[float], None] = time.sleep

    def _ensure_client(self):
        if self.client is not None:
            return self.client
        try:
            from openai import OpenAI
        except ImportError as e:
            raise EmbedderError(f"openai not installed: {e}")
        self.client = OpenAI()
        return self.client

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Batch-embed. Raises EmbedderError on permanent failure."""
        if not texts:
            return []
        client = self._ensure_client()

        last_err: Optional[Exception] = None
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = client.embeddings.create(model=self.model, input=texts)
                vectors = [d.embedding for d in resp.data]
                if any(len(v) != self.dim for v in vectors):
                    raise EmbedderError(
                        f"dimension mismatch: expected {self.dim}, "
                        f"got {[len(v) for v in vectors][:3]}"
                    )
                self._log_cost(resp, len(texts))
                return vectors
            except RateLimitError as e:
                last_err = e
                if attempt >= self.max_retries:
                    break
                self.sleep_fn(self.base_backoff * (2 ** (attempt - 1)))
                continue
            except EmbedderError:
                raise
            except Exception as e:
                # Treat unknown as permanent (don't hide real bugs via retry)
                raise EmbedderError(f"embed failed: {type(e).__name__}: {e}")
        raise EmbedderError(f"rate limited {self.max_retries}× — giving up: {last_err}")

    def _log_cost(self, resp: object, batch_size: int) -> None:
        usage = getattr(resp, "usage", None)
        tokens = getattr(usage, "total_tokens", 0) if usage else 0
        self.cost_log_path.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "model": self.model,
            "batch_size": batch_size,
            "total_tokens": tokens,
        }
        with self.cost_log_path.open("a") as f:
            f.write(json.dumps(row) + "\n")
