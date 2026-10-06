# 0013 — The Demo reads Memory's pipeline-built snapshot (`.json.gz`), frozen for the goldens

- Status: Accepted
- Date: 2026-10-06
- Issue: MVL-147

## Context

Context's Demo v1 path and tests read Deploy's hand-written test double of the acceptance corpus's graph
(`packages/neptune-deploy/tests/fixtures/packs/acceptance_corpus.graph.json`). It was never a valid Memory
document: `demo_document()` recomputed every claim id and the generation and sorted record lists so Memory's
strict codec would accept it (ADR 0009 §6 expected it to retire). Memory now publishes the real thing:
`packages/neptune-memory/tests/fixtures/acceptance_corpus.graph.json.gz`, built by the pipeline (harness
corpus, compiler, Deploy mapping, a real Ledger catalog, `memory rebuild`), graph-schema 2.0.0, gzipped
deterministically to stay under the repository's 512 KB fixture limit. Memory will regenerate it as the
corpus grows.

Two things follow. The server's reader must read gzip without opening a gzip-bomb hole. And Context's goldens
must not read a file another package regenerates, or every Memory regeneration breaks Context's CI.

## Decision

1. **`read_graph_document` reads a name ending `.gz` as one gzip member.** It keeps the regular-file check
   (a FIFO, device or directory is refused before it is opened; a symlink to a regular file is that file).
   `MAX_GRAPH_BYTES` bounds the compressed file and the decompressed bytes. The stream is decompressed with
   `zlib` in 64 KiB steps, each asking for no more than the bytes still allowed, so a bomb is refused after
   at most the cap in memory, never expanded first. A truncated stream, a flipped byte or bad CRC, bytes that
   are not gzip, a second member and trailing bytes are each a `ValueError` (the CLI's exit 2). Plain `.json`
   is read as before; gzip is chosen by name, never sniffed.
2. **Goldens read a frozen copy.** `tests/golden/demo-graph-2.0.0.json.gz` is a byte copy of Memory's snapshot
   as of this ADR, put there by `scripts/freeze_demo_graph.py`, which checks it decodes with the pinned codec
   and is under 512 KB. `DEMO_SNAPSHOT` points at the copy; the transcript and every demo test read it.
   Moving the demo to a newer Memory snapshot is a deliberate refreeze plus regenerating
   `tests/agent_goldens_context.py`, explained in its PR.
3. **One smoke test reads Memory's live snapshot** (`test_memory_snapshot_smoke_context.py`): it decodes with
   the pinned codec and answers the arm-cell query with cited output. It compares no bytes and finds its
   subjects in the document, so a regeneration that keeps the shape passes and one that breaks the contract
   fails loudly.
4. **The demo served to an agent is Memory's live file**, as it is: `mcp.sample.json`, the skill and
   `export_demo_graph.py` default to its path and `$NEPTUNE_MEMORY_GRAPH` overrides it.
5. **No fix-up of the graph.** The Deploy double's normalisation is deleted. What the snapshot states is read
   as written; what it does not state is not made up. In this snapshot the arm `ARM-3A` is declared under
   three source systems' names with no `same_as` between them, and the incident is an event node that no claim
   links to the arm. The demo queries therefore name the three arm identities and the incident node, and the
   planner (which does not offer content addresses as names) returns `needs_choice` for the bare `ARM-3A`.

## Alternatives considered

- **Keep reading Deploy's double.** It is not a Memory document and needs a normaliser that hides what Memory
  would actually emit. Rejected.
- **Goldens read Memory's live file.** Every regeneration would rewrite Context's transcript, so Memory's PRs
  would break Context's CI. Rejected for a frozen copy and a smoke test.
- **Decompress with `gzip.open` and read up to the cap.** `gzip.open` joins a multi-member file into one
  stream, tolerates zero padding after it and has no per-call limit, so the cap could only be checked after
  expansion; the explicit decoder refuses all three and applies the cap before expanding. Rejected.
- **Sniff gzip magic instead of the name.** A plain document that begins with `0x1f 0x8b` is not JSON anyway,
  but a name is what the operator wrote and the refusal says what was expected. Rejected as surprising.

## Consequences

- The Demo answer no longer carries the older-graph line (the snapshot is a 2.x document). The ten persona
  answers still do: they read 1.x fixtures on purpose.
- The Deploy double's calibration deltas are gone from the demo graph; the `delta` rendering test reads the
  explain fixture graph, which holds a `drift` claim, until Memory's snapshot has one.
- Memory's snapshot has two transactions and no inferred claims near the arm, so the demo no longer shows an
  `INFERRED` item; the persona goldens do.
- Revisit when Memory links the incident to the arm, or joins the arm's three names: refreeze, regenerate,
  and the demo queries can name one machine again.
