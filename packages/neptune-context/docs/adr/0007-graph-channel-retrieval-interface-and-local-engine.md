# 0007 — The graph channel, the retrieval channel interface, and the local engine

- Status: Accepted
- Date: 2026-10-06
- Issue: MVL-144

## Context

C1 fixed what a query says (ADR 0002), what a packet holds (ADR 0003), how callers reach an engine
(ADR 0004) and what an answer must satisfy (ADR 0006). Nothing answered a query yet: the SDK and the
MCP server served recorded packets only. Demo v1 needs Claude Code to ask "why did the arm-cell
incident happen, what changed" over a local Ledger and Memory and get cited answers.

The first channel is the graph: seed from the query's subjects, walk Memory's claims valid at the
snapshot and during the window, and return claims and the series windows and frames they reference.
Lexical (MVL-142) and vector (MVL-143) channels follow and must plug in without touching the graph
channel or the engine, and fusion (MVL-145) must replace the stand-in ranking behind the same calls.

Three upstream facts shape it:

- Context pinned graph-schema 1.0.0 and catalog-api 1.6.0. Memory's data already uses 1.4–1.6
  vocabulary (`at_site`, `has_clock`, `in_zone`, configuration lineage, events), and Demo v1's "what
  changed" needs `configuration_active_during`, `succeeds` and `authorised_configuration`. The
  Ledger's `query(spec)` with frame windows and budgets is catalog-api 1.7.0 (Ledger ADR 0016).
- Memory may still run ahead of any pin. The packet schema at the pin does not describe newer values,
  and the packet reader used Memory's live codec, so a packet with a 1.7 value decoded and passed.
- Memory's reader (`neptune_memory.schema.MemoryReader`) answers per node, per subject and by
  neighbourhood at one transaction; it has no by-kind index and no spatial view (G3). The Ledger's
  `query` reads a time window on one clock or a frame box, and defers series rows across clocks
  (Ledger ADR 0016 §9).

## Decision

1. **Pins move to graph-schema 1.6.0 and catalog-api 1.7.0; `query-packet` 1.1.0.** `pins.py`,
   `contracts/lock.toml`, `docs/contracts.md` and the pinned snapshot (`pinned.json`) move together.
   The query schema's subject-kind and predicate enums widen and the packet schema embeds 1.6.0's
   Memory definitions: additive, so `query-packet` 1.1.0 (ADR 0002 §9). Every 1.0 query keeps its
   bytes and id. The golden packets change only in `ledger_snapshot.catalog_api_version` (and so their
   packet ids); the planner recordings change only in `request_sha256`. Pins are restated here as
   `CATALOG_API_VERSION = "1.7.0"` and `GRAPH_SCHEMA_VERSION = "1.6.0"`; ADR 0001 keeps the first pins.
2. **One channel interface** (`neptune_context.retrieve.channel`).
   - `Snapshot(as_of, head, memory_as_of)`: resolved once per query by the engine, read by every channel.
   - `Retrieval(query, snapshot)`: what a channel is asked. The query is already validated.
   - `ChannelAnswer(channel, hits, gaps, findings, superseded)`: hits are packet items ranked by raw
     score then item id, each carrying only this channel's `ChannelHit` (rank = position). Gaps point
     into the query. Findings are Memory resolver findings that name a hit claim. `superseded` lists
     supersessions in `(memory_as_of, head]` of hit claims. A hit that names claims (scene,
     configuration) only names claims among the hits. `answer(channel, scored, ...)` builds one.
   - `RetrievalChannel`: `channel`, `config` (every setting that decides its answers; it enters the
     engine's config hash) and `retrieve(request) -> ChannelAnswer`. Deterministic, read-only, total:
     what a channel cannot answer is a gap, never an exception. The engine still turns a channel that
     raises into a `not_covered` gap from that channel, so the other channels' answers survive.

   A channel never fuses, cuts or assembles; nothing in the interface names a particular channel.
3. **The graph channel** (`retrieve.graph.GraphChannel(memory, catalog=None)`).
   - **Seeds.** Each subject with a `declared_id` whose kind is a graph-schema node type is the node
     `NodeRef(kind, declared_id)`. It widens along `same_as` up to its `same_as_depth`
     (`same_as_closure`, never candidates); the `same_as` claims that justify the widening are hits.
     With no such subject, the site and its zones are the seeds (ADR 0002 §5: the anchors). With one,
     the site scopes instead: a declared seed is kept only if admitted claims connect it to the site
     (or, given zones, to one of them) within two hops, else a `not_covered` gap at `/site` names it.
     A declared subject Memory holds nothing about is a `not_covered` gap and never falls back to the
     site's neighbourhood. A subject with a thread kind, or a kind-wide subject with no seed, is a
     `not_covered` gap: Memory's reader has no by-kind or thread index. A kind-wide subject next to
     seeds filters: only claims touching a node of that kind, or joining two seeds, are kept.
   - **Traversal.** Breadth first from the seeds, `hops` levels (default 1), following claims whose
     predicate is allowed and whose direction matches (`out`: the node is the subject; `in`: the
     object; `both`). `predicates: any` allows every pinned predicate except `same_as_candidate`;
     a candidate edge is followed only when the allow-list names it, and is shown as a claim, never
     used to widen identity. Only claims whose object is a node extend the walk.
   - **Snapshot and window.** Memory is read at `memory_as_of`. With a `during`, a claim on the
     window's clock is kept when its valid interval overlaps the window. A claim on a clock a query
     bridge joins to the window's clock is placed only through the bridge's mapping as Memory holds
     it: the `clock_map` claims citing the bridge's `mapping_id`, active at the snapshot. Memory
     splits a mapping into time-bounded pieces, so each piece is used only where it holds: its
     validity (on the source clock, carried onto the window's clock when the window is on the
     target) is clipped to the window, only that part is carried exactly through that piece's map
     (widened by its residual bound), and on the source clock the result is clipped to the piece's
     validity again. The pieces a carried claim was placed through are carried as hits. Any part of
     the window no piece covers, a composed or unknown map, or a bridge Memory does not hold at all
     is an `unknown` gap at that bridge: a map is never extended past where its evidence says it
     holds. Any other clock is an `other_clock` gap naming the claims, not walked through.
   - **Inference.** Memory is always read with inferred claims; with `include_inferred = false`
     they are named in one `inferred_withheld` gap and never walked through or carried.
   - **Beyond the pin.** A claim with a predicate, node type or value type the pinned graph-schema
     does not have is never carried or walked: a `not_covered` gap names it (ADR 0006 Consequences).
   - **Ledger windows.** With a catalog, the window and each region go to `CatalogApi.query`: a
     `TimeWindow` on the window's own clock (the `time_interval` R-tree) and a `FrameWindow` per region
     (the `spatial_extent` R-tree; a sphere as its bounding box, which only widens), kinds `stream` and
     `image`, at the packet's `as_of`, under a row budget. A row whose record a carried claim names, or
     whose source a carried claim cites, becomes a `SeriesWindowItem` (a stream: its stated interval
     clipped to the window, Arrow handle `series/<stream>.parquet` of its package) or a `FrameItem` (an
     image: its instant when the record states one). Its transform comes from the Ledger's lineage;
     a row with no source, transform or assertion kind is an `unknown` gap, never an assumed
     `observed`. A Ledger read that fails is a gap; the claims stay. A bridged window is not carried to the Ledger: a
     `not_covered` gap cites Ledger ADR 0016 §9. Without a catalog, a window or region is a
     `not_covered` gap; site and zones are graph seeds and need no catalog.
   - **Bounds.** A walk expands at most `max_nodes` (4096) nodes and follows at most `max_edges`
     (1024) claims from one node, by claim id; either cut is a `not_covered` gap at the walk it cut
     (`/graph`, or `/site` for the site check), naming the crowded nodes.
   - **Scores.** A claim's distance is the walk level at which it was reached (1 for a claim touching a
     seed). Its weight is 1 for observed and stated claims and its confidence for inferred ones
     (`Unknown`: 0.5). Raw score = weight × 0.5^(distance − 1). A series window or frame scores half its
     best citing claim. Ties break on item id.
   - **Supersessions and findings.** Hit claims are checked at head with one Memory read per
     subject and predicate; only a claim gone by then is bisected over `(memory_as_of, head]` (reads
     shared by its group) for the transaction its version stopped being current, and the versions
     current then that list it in `supersedes` are named. Findings are the reader's, filtered to hit
     claims and to codes at the pin.
4. **Fusion and cut, v0** (`retrieve.fusion`, replaced by MVL-145 behind the same signatures).
   Reciprocal-rank fusion (k = 60) by item id: ranks, not raw scores, are summed, so no channel's score
   scale is privileged. `cut` keeps the longest prefix of the fused order within items, bytes and
   tokens (measured as `BudgetUse` measures them) and drops any item whose claims did not fit.
5. **The local engine** (`neptune_context.engine.LocalEngine(memory, catalog=None, channels=None)`)
   implements the SDK `Engine` seam. `query` resolves the snapshot (`as_of` = the query's integer or the
   head; head = the larger of Memory's and the catalog's; Memory read at `min(as_of, memory head)`),
   runs every channel, fuses, cuts, and assembles one `ContextPacket`: `during` resolved to its domain
   id, the budget echoed, findings and supersessions restricted to carried claims, gaps sorted. Query
   members no channel serves yet (`text`: MVL-142/143; `explain`: MVL-149) are `not_covered` gaps.
   `produced_by` is `neptune-context.local`, version `1`, config hash over the channel list and
   fusion settings. `hydrate` is the catalog's `resolve`; without a catalog it is `unavailable`. The
   MCP CLI gains `--memory GRAPH.json`: a regular file of at most 256 MiB, read strictly (no duplicate
   keys, no NaN) and decoded by Memory's codec into its reference reader. A Ledger catalog plugs in programmatically
   (`build_server(AsyncClient(LocalEngine(reader, catalog)))`): Context may not construct the Ledger's
   PostgreSQL catalog (import boundary), so Platform wires it.
6. **Values beyond the pin are refused by the reader too.** `packets.codec.decode` checks every
   decoded claim, node and finding against `pinned` (`claim_beyond_pin`, `node_beyond_pin`,
   `finding_beyond_pin`) and refuses with `shape`: Memory's live codec accepting a value no longer
   means Context's reader does.

## Alternatives considered

- **Seed kind-wide subjects by scanning Memory.** The reader has no by-kind call, and walking every
  node would read the whole graph per query. A gap says so until Memory publishes an index. Lost.
- **Convert claims on other clocks through any mapping Memory holds.** The query must name the
  mapping (ADR 0002 §3); converting through an unnamed one is the silent assumption the lower layers
  refuse. Lost.
- **Score by raw channel scores in fusion.** A channel with larger scores would win by construction,
  breaking package rule 3. Lost.
- **Construct the Ledger's PostgreSQL catalog from the MCP CLI.** Context may import only
  `neptune_ledger.api`; a dynamic import would bypass the boundary test. Lost: Platform wires it.
- **Relabel claims placed through an estimated mapping as inferred.** A claim item's envelope must
  equal its claim's (ADR 0003 §3). The mapping claim is carried instead, itself inferred when Memory
  estimated it. Lost.
- **Keep the 1.0.0 pin and report every newer value as a gap.** Demo v1's configuration lineage is
  1.4+ vocabulary: the demo would answer nothing. Lost.

## Consequences

- The MCP server answers real queries over a local Memory graph document (and a Ledger when wired);
  MVL-147 (renderer) and MVL-149 (explain and diff) build on `LocalEngine`.
- MVL-142 and MVL-143 implement `RetrievalChannel` and pass themselves to `LocalEngine(channels=...)`;
  MVL-145 replaces `fuse` and `cut`.
- Deploy and Learn may raise their `query-packet` lock to 1.1.0 at will; 1.0.0 readers lose nothing.
- Revisit when Memory publishes a by-kind or thread index (kind-wide seeds), a spatial view (G3:
  zones become spatial, not only seeds), or the Ledger carries series rows across clocks.
