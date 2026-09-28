# Scoring retrieval quality

## The goldset format

A goldset is a JSONL file where each row has a query_id, the expected
result items (exact ids or path prefixes), and a query_class label used
to slice metrics.

## Running the scorer

linecite-score takes a retrieval-results JSONL and a goldset and prints
hit@k, precision@1, MRR, and nDCG overall and per query class (the
bootstrap confidence interval applies to the overall hit@k only):

    linecite-score results.jsonl evals/goldset.jsonl --k 5
