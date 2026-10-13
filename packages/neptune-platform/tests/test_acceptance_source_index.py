"""The source index pinned beside the answers (Context ADR 0016) fits the corpus and the graph.

The context stage builds the index from the Ledger's catalog and fails when it differs from
``harness/acceptance/sources.json``; this test checks the committed copy without a catalog: it
reads with Context's own reader, every path it names holds exactly the bytes the corpus lock pins
for that path, and it places every source Memory's committed snapshot cites (the graph the sample
``.mcp.json`` serves by default).
"""

import gzip
import json
from typing import Any

from harness import acceptance
from neptune_context.sources import dumps_source_index, read_source_index

SOURCES = acceptance.ANSWERS.with_name("sources.json")


def _cited(graph: dict[str, Any]) -> set[str]:
    return {
        str(ref["source"])
        for claim in graph["claims"]
        for ref in claim.get("provenance", {}).get("evidence", [])
        if isinstance(ref.get("source"), str)
    }


def test_the_pinned_index_reads_and_is_written_as_the_stage_writes_it() -> None:
    index = read_source_index(SOURCES)
    assert SOURCES.read_text(encoding="utf-8") == dumps_source_index(index)


def test_every_path_holds_the_bytes_the_corpus_lock_pins() -> None:
    files = json.loads(acceptance.LOCK.read_text(encoding="utf-8"))["files"]
    for source, paths in read_source_index(SOURCES).paths.items():
        for path in paths:
            assert files.get(path, {}).get("sha256") == source, path


def test_it_places_every_source_memorys_committed_snapshot_cites() -> None:
    graph = json.loads(gzip.decompress(acceptance.MEMORY_SNAPSHOT.read_bytes()))
    placed = set(read_source_index(SOURCES).paths)
    assert sorted(_cited(graph) - placed) == []
