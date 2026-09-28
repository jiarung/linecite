"""linecite — verbatim, line-cited memory for AI agents.

Artifact-first knowledge store: chunks are exact line ranges of real
files, every citation is `path:Lstart-Lend` and verifiable against the
live source, and the default retrieval path (DuckDB FTS) needs no API
key and no services.
"""
from linecite.store import Chunk, ChunkStore, SnapshotResult
from linecite.retrieve import RetrievalResult, Retriever
from linecite.validate_citation import CitationCheck, validate_verbatim
from linecite.generic_ingester import ingest_paths
from linecite.snapshot import (ImportResult, SnapshotError,
                               export_snapshot, import_snapshot)

__version__ = "0.1.0.dev0"
__all__ = ["Chunk", "ChunkStore", "SnapshotResult", "RetrievalResult",
           "Retriever", "CitationCheck", "validate_verbatim",
           "ingest_paths", "ImportResult", "SnapshotError",
           "export_snapshot", "import_snapshot"]
