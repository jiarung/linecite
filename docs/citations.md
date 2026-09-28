# Verifiable citations

## The contract

Every chunk stores raw_text: the exact newline-joined slice of its
source lines. A citation is `source_path:Lstart-Lend`, and it can be
checked mechanically against the live file.

## Validating at read time

Pass verify_root to retrieve() and stale citations are marked in
RetrievalResult.stale_citations. Reasons a citation can fail:
content_drift (file changed), source_missing (file gone), and
no_raw_text (legacy rows ingested before verbatim slices existed).

## Write-time enforcement

A chunk with an invalid line range (start below 1, or end before start)
is rejected when written to the store.
