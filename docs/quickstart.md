# Quickstart

## Install

pip install linecite. The only required dependency is DuckDB; embeddings
are an optional extra.

## Ingest a directory

Run linecite-ingest on any folder of markdown or text files:

    linecite-ingest ./my-notes --db kb.duckdb

Re-running reconciles: unchanged files are no-ops, edited files are
re-chunked, and files you deleted are reclaimed (full scope).

## Query without an API key

The default retrieval path is lexical (BM25 full-text search):

    from linecite import ChunkStore, Retriever
    store = ChunkStore("kb.duckdb")
    result = Retriever(store).retrieve("how do refunds work", k=5)
