"""Phase E1 acceptance: local embedder adapter (model2vec).

Unit tests (always run, no network): import-guard message, protocol
shape via injected fakes. Integration tests (marker: local_embeddings)
run only when the extra is installed and the model is cached — they
prove real semantic retrieval with zero API keys and zero openai
imports.
"""
from __future__ import annotations

import builtins
import sys

import pytest

from linecite.store import ChunkStore


def _model_cached_offline() -> bool:
    """True only when the extra is installed AND the default model
    loads from the local HF cache with networking forbidden — neither
    the predicate nor the tests may ever trigger a download (E1 CI
    strategy). To run these tests, pre-download the model once:
    python -c 'from linecite.local_embedder import LocalEmbedder; LocalEmbedder()'"""
    import os
    # Forbid networking BEFORE importing model2vec/huggingface_hub so
    # the proof is genuinely cache-only.
    old = os.environ.get("HF_HUB_OFFLINE")
    os.environ["HF_HUB_OFFLINE"] = "1"
    try:
        try:
            from model2vec import StaticModel
        except ImportError:
            return False
        from linecite.local_embedder import DEFAULT_LOCAL_MODEL
        import inspect
        kwargs = ({"force_download": False}
                  if "force_download" in inspect.signature(
                      StaticModel.from_pretrained).parameters else {})
        StaticModel.from_pretrained(DEFAULT_LOCAL_MODEL, **kwargs)
        return True
    except Exception:
        return False
    finally:
        if old is None:
            os.environ.pop("HF_HUB_OFFLINE", None)
        else:
            os.environ["HF_HUB_OFFLINE"] = old


needs_local = pytest.mark.local_embeddings
skip_unless_installed = pytest.mark.skipif(
    not _model_cached_offline(),
    reason="local-embeddings extra not installed or model not in the "
           "local HF cache (pre-download once to enable)")


class TestImportGuard:
    def test_missing_extra_gives_install_hint(self, monkeypatch):
        real_import = builtins.__import__

        def deny_model2vec(name, *a, **kw):
            if name == "model2vec" or name.startswith("model2vec."):
                raise ImportError("denied for test")
            return real_import(name, *a, **kw)
        monkeypatch.delitem(sys.modules, "model2vec", raising=False)
        monkeypatch.setattr(builtins, "__import__", deny_model2vec)
        from linecite.local_embedder import LocalEmbedder
        with pytest.raises(ImportError, match="linecite\\[local-embeddings\\]"):
            LocalEmbedder()


class _FakeArr(list):
    """Numpy-free stand-in for the encode() result: a list of row
    lists exposing .shape — exactly what LocalEmbedder consumes."""

    @property
    def shape(self):
        return (len(self), len(self[0]) if self else 0)


class _FakeStaticModel:
    calls: list = []
    fail_with: Exception = None

    @classmethod
    def from_pretrained(cls, source, force_download=True):
        # The fake DECLARES the kwarg, so feature detection must opt
        # out — any regression back to forced downloads fails here.
        assert force_download is False, \
            "LocalEmbedder must pass force_download=False (cache reuse)"
        if cls.fail_with is not None:
            raise cls.fail_with
        inst = cls()
        inst.source = source
        return inst

    def encode(self, texts):
        _FakeStaticModel.calls.append(list(texts))
        return _FakeArr([[float(len(t)), 1.0, 2.0] for t in texts])


class TestFakeBackedUnits:
    """Always-run protocol coverage — no network, no numpy, no extra."""

    @pytest.fixture(autouse=True)
    def fake_model2vec(self, monkeypatch):
        import types
        _FakeStaticModel.calls = []
        _FakeStaticModel.fail_with = None
        mod = types.ModuleType("model2vec")
        mod.StaticModel = _FakeStaticModel
        monkeypatch.setitem(sys.modules, "model2vec", mod)

    def _mk(self, **kw):
        from linecite.local_embedder import LocalEmbedder
        return LocalEmbedder(**kw)

    def test_protocol_shape_and_dim_probe(self):
        e = self._mk()
        assert e.model == "model2vec/minishlab/potion-base-8M"
        assert e.dim == 3                       # probed from fake encode
        vecs = e.embed(["ab", "cdef"])
        assert vecs == [[2.0, 1.0, 2.0], [4.0, 1.0, 2.0]]

    def test_single_batched_encode_call(self):
        e = self._mk()
        _FakeStaticModel.calls.clear()
        e.embed(["a", "b", "c"])
        assert len(_FakeStaticModel.calls) == 1   # one batch, no loop
        assert _FakeStaticModel.calls[0] == ["a", "b", "c"]

    def test_empty_batch_short_circuits(self):
        e = self._mk()
        _FakeStaticModel.calls.clear()
        assert e.embed([]) == []
        assert _FakeStaticModel.calls == []

    def test_model_path_is_load_source_identity_is_model_id(self):
        e = self._mk(model_id="custom/mini", model_path="/tmp/copy")
        assert e._model.source == "/tmp/copy"     # loaded from path
        assert e.model == "model2vec/custom/mini"  # identity from id

    def test_load_failure_messages_branch_by_source(self):
        _FakeStaticModel.fail_with = OSError("nope")
        with pytest.raises(RuntimeError, match="Hugging Face once"):
            self._mk()
        with pytest.raises(RuntimeError, match="model_path"):
            self._mk(model_path="/missing/dir")

    def test_deterministic_conversion(self):
        e = self._mk()
        assert e.embed(["same"]) == e.embed(["same"])


@needs_local
@skip_unless_installed
class TestLocalEmbedderIntegration:
    @pytest.fixture(scope="class")
    @staticmethod
    def embedder():
        from linecite.local_embedder import LocalEmbedder
        return LocalEmbedder()

    def test_protocol_shape(self, embedder):
        assert embedder.model.startswith("model2vec/")
        assert embedder.dim > 0
        vecs = embedder.embed(["hello world", "second text"])
        assert len(vecs) == 2 and len(vecs[0]) == embedder.dim

    def test_deterministic(self, embedder):
        a = embedder.embed(["stable input text"])
        b = embedder.embed(["stable input text"])
        assert a == b

    def test_no_openai_import_in_local_mode(self, embedder, tmp_path,
                                            monkeypatch):
        # No-key ladder rung: local semantic mode must never touch the
        # openai package (same denial technique as the lexical no-key
        # acceptance test).
        for mod in [m for m in sys.modules if m == "openai"
                    or m.startswith("openai.")]:
            monkeypatch.delitem(sys.modules, mod, raising=False)
        real_import = builtins.__import__

        def deny_openai(name, *a, **kw):
            if name == "openai" or name.startswith("openai."):
                raise ImportError("openai denied")
            return real_import(name, *a, **kw)
        monkeypatch.setattr(builtins, "__import__", deny_openai)

        from linecite.generic_ingester import ingest_paths
        from linecite.retrieve import Retriever
        docs = {
            "cars.md": "# Notes\nThe vehicle accelerates quickly on "
                       "the highway and corners smoothly.\n",
            "pasta.md": "# Notes\nSimmer the tomato sauce and boil "
                        "spaghetti until al dente.\n",
            "finance.md": "# Notes\nQuarterly revenue grew while "
                          "operating expenses declined.\n",
        }
        for rel, text in docs.items():
            (tmp_path / rel).write_text(text, encoding="utf-8")
        s = ChunkStore(dim=embedder.dim)
        ingest_paths(s, tmp_path, host="h1", origin_repo="r1",
                     embedder=embedder)
        # zero lexical overlap with cars.md body ("fast car" appears
        # nowhere) — BM25 would structurally miss; the semantic leg
        # must place the driving chunk on top.
        res = Retriever(s, embedder=embedder).retrieve("fast car", k=1)
        assert res.chunks and res.chunks[0].source_path == "cars.md"
        assert res.chunks[0].embedding_model == embedder.model
