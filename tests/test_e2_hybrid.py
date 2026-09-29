"""Phase E2 acceptance: hybrid RRF retrieval (fixture-bound).

A ControlledEmbedder maps designed texts/queries onto designed unit
vectors, so every assertion is about THIS fixture's construction —
RRF gives no cross-corpus theorems (plan E2.4 disclaimer).
"""
from __future__ import annotations

import hashlib
from datetime import datetime

import pytest

from linecite.identity import chunk_id as _cid, chunk_uid as _cuid, content_sha as _csha
from linecite.retrieve import Retriever
from linecite.store import Chunk, ChunkStore

MODEL = "fake/controlled"

# Designed 4-d unit vectors.
_AXES = {
    "car": [1.0, 0.0, 0.0, 0.0],
    "food": [0.0, 1.0, 0.0, 0.0],
    "flurb": [0.0, 0.0, 1.0, 0.0],
    "near_flurb": [0.1, 0.0, 0.995, 0.0],
    "other": [0.0, 0.0, 0.0, 1.0],
}

# text/query -> semantic axis. Zero lexical overlap between
# "fast car" and the vehicle doc is the synonym case; the exact-term
# case gives the WRONG doc the closest vector for its query.
_MEANING = {
    "the vehicle accelerates on the highway": "car",
    "fast car": "car",
    "simmer the tomato sauce gently": "food",
    "flurbization threshold table": "near_flurb",     # right doc, close
    "unrelated widget catalog entry": "flurb",        # WRONG doc, closest
    "flurbization": "flurb",
    "shared boilerplate line": "other",
    "shared boilerplate": "other",       # scope-test query
}


class ControlledEmbedder:
    model = MODEL
    dim = 4

    def embed(self, texts):
        return [list(_AXES[_MEANING[t]]) for t in texts]


def _mk(text: str, *, host="h1", path: str, emb=None) -> Chunk:
    csha = _csha(text)
    return Chunk(
        chunk_id=_cid(path, text), source_path=path, source_type="note",
        text=text, start_line=1, end_line=1,
        embedded_at=datetime(2026, 9, 29),
        embedding=emb, embedding_model=MODEL if emb else None,
        raw_text=text, host=host, origin_repo="r1", content_sha=csha,
        chunk_ordinal=0, chunk_uid=_cuid(host, "r1", path, 0, csha),
    )


@pytest.fixture()
def corpus():
    e = ControlledEmbedder()
    s = ChunkStore(dim=4)
    s.init_local_identity("h1")
    s.register_host("h2", hashlib.sha256(b"h2").hexdigest()[:32],
                    allow_direct_writes=True)
    docs = [
        ("docs/car.md", "the vehicle accelerates on the highway", "h1"),
        ("docs/pasta.md", "simmer the tomato sauce gently", "h1"),
        ("docs/flurb.md", "flurbization threshold table", "h1"),
        ("docs/widget.md", "unrelated widget catalog entry", "h1"),
        ("docs/h2.md", "shared boilerplate line", "h2"),
    ]
    for path, text, host in docs:
        s.write_chunk(_mk(text, path=path, host=host,
                          emb=e.embed([text])[0]))
    return s, e


class TestModes:
    def test_auto_resolves_by_embedder(self, corpus):
        s, e = corpus
        assert Retriever(s).retrieve("tomato", k=2).retrieval_mode \
            == "lexical"
        assert Retriever(s, embedder=e).retrieve(
            "fast car", k=2).retrieval_mode == "hybrid"

    def test_explicit_modes_and_errors(self, corpus):
        s, e = corpus
        r = Retriever(s, embedder=e)
        assert r.retrieve("fast car", k=2,
                          mode="vector").retrieval_mode == "vector"
        assert r.retrieve("tomato", k=2,
                          mode="lexical").retrieval_mode == "lexical"
        with pytest.raises(ValueError, match="mode"):
            r.retrieve("x", mode="bogus")
        with pytest.raises(ValueError, match="requires an embedder"):
            Retriever(s).retrieve("x", mode="hybrid")
        with pytest.raises(ValueError, match="requires an embedder"):
            Retriever(s).retrieve("x", mode="vector")

    def test_lexical_only_paths_unchanged(self, corpus):
        s, _ = corpus
        res = Retriever(s).retrieve("tomato sauce", k=2)
        assert res.chunks[0].source_path == "docs/pasta.md"


class TestFixtureBoundGains:
    def test_synonym_query_lexical_miss_hybrid_hit(self, corpus):
        s, e = corpus
        # zero content-word overlap: FTS drops zero-overlap rows
        lex = Retriever(s).retrieve("fast car", k=5)
        assert all(c.source_path != "docs/car.md" for c in lex.chunks)
        hyb = Retriever(s, embedder=e).retrieve("fast car", k=5)
        assert hyb.chunks[0].source_path == "docs/car.md"

    def test_exact_term_survives_wrong_vector(self, corpus):
        s, e = corpus
        # vector leg ranks the WRONG doc first (widget owns the axis);
        # the lexical leg rescues the true flurbization doc to top-1.
        vec = Retriever(s, embedder=e).retrieve("flurbization", k=2,
                                                mode="vector")
        assert vec.chunks[0].source_path == "docs/widget.md"
        hyb = Retriever(s, embedder=e).retrieve("flurbization", k=2)
        assert hyb.chunks[0].source_path == "docs/flurb.md"

    def test_hybrid_scores_descend_and_mode_stamped(self, corpus):
        s, e = corpus
        res = Retriever(s, embedder=e).retrieve("fast car", k=5)
        assert res.retrieval_mode == "hybrid"
        assert res.scores == sorted(res.scores, reverse=True)


class TestDeterminismAndLegs:
    def test_bitwise_deterministic_including_ties(self, corpus):
        s, e = corpus
        r = Retriever(s, embedder=e)
        a = r.retrieve("fast car", k=5)
        b = r.retrieve("fast car", k=5)
        assert [c.chunk_uid for c in a.chunks] == \
            [c.chunk_uid for c in b.chunks]
        assert a.scores == b.scores

    def test_scope_applies_inside_both_legs(self, corpus):
        s, e = corpus
        res = Retriever(s, embedder=e).retrieve(
            "shared boilerplate", k=5, scope=["h2"])
        assert res.chunks and \
            {c.host for c in res.chunks} == {"h2"}
        res_h1 = Retriever(s, embedder=e).retrieve(
            "shared boilerplate", k=5, scope=["h1"])
        assert all(c.host == "h1" for c in res_h1.chunks)

    def test_scoped_leg_not_starved_at_small_k(self, corpus):
        s, e = corpus
        # k=1 with scope: leg-internal filtering must still surface the
        # scoped host even though unscoped fused heads would differ.
        res = Retriever(s, embedder=e).retrieve(
            "shared boilerplate", k=1, scope=["h2"])
        assert len(res.chunks) == 1 and res.chunks[0].host == "h2"

    def test_citation_checks_on_fused_list(self, corpus, tmp_path):
        s, e = corpus
        res = Retriever(s, embedder=e).retrieve(
            "fast car", k=5, verify_root=tmp_path, local_host="h1")
        assert len(res.citation_checks) == len(res.chunks)
        by_reason = {u: reason for u, _, reason in res.citation_checks}
        for c in res.chunks:
            expected = ("remote_unverified" if c.host != "h1"
                        else "source_missing")   # tmp_path has no files
            assert by_reason[c.chunk_uid] == expected

    def test_hybrid_lexical_leg_includes_unembedded_rows(self, corpus):
        # DESIGN DECISION pinned as regression (plan E2.4): the lexical
        # leg is embedding-model-agnostic. BM25 relevance does not live
        # in a vector space — and if the lexical leg were model-scoped,
        # every not-yet-embedded row would vanish from hybrid results,
        # making hybrid strictly worse than lexical on partially
        # embedded corpora.
        s, e = corpus
        _MEANING["flurbization archive notes"] = "other"
        lex_only = _mk("flurbization archive notes",
                       path="docs/lexonly.md")     # NO embedding
        s.write_chunk(lex_only)
        res = Retriever(s, embedder=e).retrieve("flurbization", k=5)
        assert any(c.source_path == "docs/lexonly.md"
                   for c in res.chunks)

    def test_duplicate_groups_on_fused_list(self, corpus):
        s, e = corpus
        # a cross-host clone of the boilerplate line: same content_sha,
        # both hosts — hybrid result must mark, not dedup.
        clone = _mk("shared boilerplate line", path="docs/h2.md",
                    host="h1",
                    emb=e.embed(["shared boilerplate line"])[0])
        s.write_chunk(clone)
        res = Retriever(s, embedder=e).retrieve("shared boilerplate",
                                                k=5)
        assert len(res.duplicate_groups) == 1
        idxs = res.duplicate_groups[0]
        assert {res.chunks[i].host for i in idxs} == {"h1", "h2"}
