# Evals

`goldset.jsonl` is a small goldset over this repository's own docs —
enough to smoke-test the retrieval + scoring loop end to end:

    linecite-ingest . --db /tmp/kb.duckdb
    # produce results.jsonl with your retrieval loop, then:
    linecite-score results.jsonl evals/goldset.jsonl --k 5

Row schema: `query_id`, `query` (free text), `expected` (result ids or
path prefixes), `query_class` (metric slice label). The scorer treats
goldset rows missing from the results file as misses.
