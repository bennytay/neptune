# 0003 — The context packet: a typed, cited, content-addressed answer with budgets and an inference flag

- Status: Accepted
- Date: 2026-10-05
- Issue: MVL-109

## Context

A query (ADR 0002) says what to retrieve; the context packet is what comes back, and it is the one
thing Deploy's console, Learn's dataset builder, the SDK, the MCP server, VLA policies and simulators
read. If the packet can drop provenance, blur inferred into stated, hide that a budget cut it, mix a
stale snapshot with a current one, or let a renderer add a sentence of its own, every consumer
inherits the defect and none of them can detect it. The packet must therefore be a record with the
same discipline as the compiler's and Memory's: typed, provenance-carrying, explicit about
missingness, deterministic and content-addressed, so that one packet is reproducible from its query
and its snapshots and can be cited by id.

Two inputs are fixed upstream: Memory's claim (graph-schema 1.0.0: subject, predicate, object, valid
interval, `recorded_at`, `assertion_kind`, confidence, provenance) and the Ledger's transaction order
and `resolve` call (catalog-api 1.6.0). The query model (MVL-108) is being written in parallel.

## Decision

1. **What a packet is.** `neptune_context.packets.ContextPacket`: one answer to one query at one
   snapshot. It names its query only by `query_id`, `query:sha256:<hex>` of the query's canonical JSON
   (ADR 0002's `query_id`), and never imports the query model, so the two contracts evolve and merge
   independently. Nothing in a packet depends on the query beyond that hash: a consumer that holds the
   query can recompute it; one that does not still has a complete, self-describing answer.
2. **Header.** `query_id`; `as_of`, the Ledger transaction the packet answers at (never `head`
   unresolved); `head`, the latest transaction known when it was assembled (`as_of <= head`);
   `during`, the world-time window resolved to one clock (`TimestampDomain` id, `[start, end)`, end
   `open` allowed); `memory_snapshot` (graph-schema major, resolver generation, its own `as_of <=`
   the packet's: Memory may trail the Ledger, never lead it); `ledger_snapshot` (catalog API version;
   the Ledger snapshot id is that version at `as_of`); `produced_by` (engine id, version, config hash:
   a new engine is a new lineage); `inference_included`; `budget`. Body: `items`, `superseded_since`,
   `findings`, `gaps`.
3. **Items.** Seven kinds, one envelope. Every item carries `assertion_kind`
   (`observed | stated | inferred`), `confidence`, `provenance` (at least one `EvidenceRef`, the
   Ledger records read, the producing transform as id + version + config hash, and the model exactly
   when inferred) and `relevance` (fused score and every channel hit: channel, rank, raw score).
   Memory's rule binds every kind: deterministic content has confidence `NotApplicable` and no model;
   inferred content names its model and has `Known(p)`, `0 <= p <= 1`, or `Unknown`. Kinds:
   `ClaimItem` (a Memory claim as known at `as_of`; its envelope must equal the claim's own),
   `EvidenceItem` (an evidence ref, the `resolve` status at the snapshot and the size; hydrate is
   `CatalogApi.resolve(evidence, as_of)`; never inferred; fetch locations never enter a packet),
   `SeriesWindowItem` (stream, clock, `[start, end)` ticks, Arrow handle = package id +
   `series/<stream>.parquet`, filtered by interval, no row offsets), `FrameItem` (one sensor sample:
   stream, instant, evidence, encoding, coordinate frame), `DocumentSpanItem` (document record,
   evidence, text exactly as extracted), `SceneItem` (a named frame, site, placed nodes, placement
   claims, frame-graph and geometry records) and `ConfigurationItem` (record, compiler record kind,
   what it configures, the claims saying so). Scene and configuration items reference claims by id;
   those claims must be carried as `ClaimItem`s in the same packet. An item's id is
   `item:sha256:` over its content without relevance: equal content, equal id, whichever query found it.
4. **Evidence is not interpretation; missingness is explicit.** A packet with
   `inference_included = false` cannot hold an inferred item, and inferred matches it leaves out are
   named by id in an `inferred_withheld` gap (never carried). An item's optional facts are
   `Knowledge` states that inherit the item's provenance (`KnownAbsent`, which needs its own grounding,
   is not used). A part of the question with no item is a `Gap`: `not_covered`, `unknown`,
   `ambiguous`, `other_clock` (claims on a clock the query did not bridge, named not compared),
   `inferred_withheld` or `unresolvable`, with a JSON pointer into the query, the reporting channel,
   the ids concerned and a detail line. `superseded_since` lists every claim item superseded in
   `(as_of, head]`, with the transaction and the superseding claim ids, so a stale `as_of` is visible.
   `findings` carries Memory's resolver findings active at the snapshot that name a claim item
   (conflicts and clock mismatches stay with the claims they qualify).
5. **Budgets.** `budget` echoes the query's limits and records use that must match the items exactly:
   item count, bytes of the items' canonical JSON, and tokens under the named estimator
   `neptune-context.utf8-bytes-div-4/1` (ceil(bytes / 4); no model, same everywhere). Use never
   exceeds a limit; `dropped` counts candidates the budget cut and `exhausted` names the limits that
   cut them, and one is non-zero exactly when the other is non-empty: a truncated packet says so.
   Latency is a limit on the call, measured by `eval/`, never written into a packet.
6. **A packet is a record.** `to_json` has one fixed shape (lists always present, absent single
   members omitted, no `null`); `canonical_bytes` is the compiler's canonical JSON of it; the id is
   `packet:sha256:` over everything but `id` under scheme `neptune-context.packet-id/1`. Items are
   ordered by fused score (descending) then item id; every other list has a canonical order. The same
   query, engine and snapshots give byte-identical packets.
7. **Renderer contract.** A renderer formats; it adds no fact, score or reading. Text for language
   models cites evidence with keys `[E1]..[En]` that end an item's line, and ends with an `Evidence:`
   footer mapping each key to the evidence ref's canonical JSON; values from evidence are written as
   JSON literals with every line break escaped, so source text cannot forge a citation or an item.
   Property, tested on every golden: `parse_citations(render_text(p)) == p.evidence_refs()`, and every
   identifier in the text occurs in the packet. Every inferred item is marked `INFERRED` with its model
   and confidence. Policy renderers project items to tensors and metadata, and simulator renderers to
   scene and configuration bundles; both carry each value's item id so it traces back, and neither may
   synthesise a value the packet does not hold (MVL-147, MVL-148).
8. **Errors are findings.** Constructors raise `PacketError` with a `PacketFindingCode`;
   `packets.codec.decode` reads any JSON text of a packet strictly (exact keys and types, no duplicate
   keys, no NaN, at most 64 MiB), rebuilds it through the constructors, recomputes every id, and
   returns `PacketRefused` with one finding (code, JSON pointer, message) instead of raising.
   Upstream values are read by their owners' codecs (Memory's claims and findings, the compiler's
   evidence refs, frames and timestamps).
9. **Version, schema and conformance.** `PACKET_VERSION = 1`, carried in every document as
   `packet_version`; a reader refuses any other value. `packets.schema.packet_schema()` exports the
   JSON Schema (draft 2020-12), copying Memory's and the compiler's definitions under their own names.
   `packets.conformance.check(document)` is the consumer contract test: a document decodes, its
   canonical bytes and ids are stable, every evidence ref survives rendering, inference stays marked.
   The registry's `query-packet` contract bundles the query and the packet; its first version is
   published by the C1 gate (MVL-111) once both this ADR and ADR 0002 have merged, with
   `neptune_context.contract` as the owner module, the two schema exports and these goldens. Until
   then `contracts/query-packet/` stays `planned` and neither PR edits it.
10. **Goldens.** Ten worked queries from the consumer personas (fleet engineer, safety lead, VLA
    policy at inference, simulator setup, auditor, training-data curator), each a query document in
    ADR 0002's canonical JSON and the packet answering it over the example graph (Memory's golden
    graph, the compiler's worked examples and their golden package manifests), live in
    `tests/golden/`; `tests/context_packet_goldens.py` regenerates them and the tests fail on drift.
    They cover every item kind, inference included and withheld, supersession, findings, truncation,
    a clock that the query did not bridge and an unresolvable citation.

| # | Persona | Question | Shows |
|---|---|---|---|
| q01 | fleet engineer | which machine recorded the drone's run, now, evidence only | claims, evidence, findings |
| q02 | fleet engineer | the same at transaction 3 | `superseded_since` |
| q03 | safety lead | who recorded the quadruped's run at tx 2, inferences included | an inferred item, marked |
| q04 | auditor | why do we believe UAV-0043 flew it | `why`, `unresolvable` gap |
| q05 | auditor | who flew it during the log, on the log's clock | `during`, `other_clock` gap |
| q06 | VLA policy | configuration and a sensor window for QUAD-03, two items | truncation, `inferred_withheld` |
| q07 | simulator setup | scene around the arm's tool frame and its calibration | scene, `not_covered` spatial |
| q08 | fleet engineer | the site register on Berth 4 and the camera image | document span, frame |
| q09 | curator | runs QUAD-03 recorded, with sensor windows | claims + series, episodes `not_covered` |
| q10 | safety lead | risk assessment and commissioning for AMR-07 | span with text, configuration |

## Alternatives considered

- **Embed the query in the packet.** Self-contained, but couples the packet schema to the query
  schema and to MVL-108's code; the hash names the question exactly and lets the two contracts merge
  and version independently. Lost.
- **Claim items without an envelope (read epistemics from the claim).** Less duplication, but every
  consumer then needs a per-kind rule to find `assertion_kind`; one envelope, checked equal to the
  claim, keeps reading uniform. Lost.
- **Row offsets in the Arrow handle.** Faster hydration, but offsets depend on the package's layout
  while the interval does not, and the planner cannot know them without reading Parquet. Lost.
- **Model token counts in the budget.** Exact for one model, wrong for every other, and needs a
  tokenizer inside a deterministic record; a named byte estimator is the same everywhere, and a
  renderer counts real tokens against its own limit. Lost.
- **Latency in the packet.** A wall-clock measurement breaks byte-identical packets; it is an `eval/`
  assertion on the call. Lost.
- **Drop inferred matches silently when inference is excluded.** Simpler, but a policy could not
  tell "none" from "withheld"; naming them by id says so without exposing their content. Lost.
- **Publish `query-packet` from this PR.** The contract bundles both halves; publishing either half
  first would force the other into an immediate bump and makes the two PRs conflict on `contracts/`.
  Lost.

## Consequences

- Consumers read one envelope for every item, can verify a packet's integrity offline (ids, budget,
  references) and can cite a packet or item by id.
- Context items reuse Memory's claim, finding and model types and the compiler's evidence refs,
  frames and timestamps verbatim; a graph-schema or package-schema major bump is a packet change.
- The engine (C2) must resolve `as_of` to a transaction, record `head`, compute `superseded_since`,
  name withheld inferences and unbridged clocks, and measure the budget before it hands a packet out.
- Coupling to MVL-108 is only the `query_id` format and the query JSON shape the golden query
  documents use; the C1 gate adds the test that decodes them with the query reader and checks each
  packet's `query_id` against `query_id(query)`.
- Revisit when Memory's episodes or spatial views land (G3: scene items gain placed nodes and claims),
  when the Ledger exposes series reads (the Arrow handle may gain a catalog call), or if a consumer
  needs a packet split across pages (a continuation token would be a `PACKET_VERSION` bump).
