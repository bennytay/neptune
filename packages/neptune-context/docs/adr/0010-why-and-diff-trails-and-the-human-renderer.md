# 0010 — Why and diff trails: provenance trees, what-changed lists, and the human renderer

- Status: Accepted
- Date: 2026-10-06
- Issue: MVL-149

## Context

ADR 0002 gave the query two `explain` clauses: `Why(claim_id)` (evidence, transform, supersession and
findings of one claim) and `Diff(subject, before, after)` (what changed about one subject between two
transactions or two world-time instants). ADR 0004 put `why` and `diff` on the SDK and the MCP server as
queries with those clauses, and ADR 0007's local engine answered both with a `not_covered` gap. Demo v1 asks
"why did the arm-cell incident happen, what changed": the answer must be built from what Memory holds, cite
the bytes behind it, and read as a tree and a change list rather than a flat ranked list.

Four facts constrain the design:

- A packet presents claims as Memory knew them at one snapshot (ADR 0003 §3, ADR 0006 §4). A transaction diff
  compares two snapshots, so the older claims are no longer current at the packet's snapshot and cannot be
  claim items.
- `MemoryReader` has no lookup by claim id, and a claim id is a content hash, so nothing in it says where to
  look.
- Memory states relations between claims only as equal assertions, `*_candidate` readings, resolver findings
  (`clock_mismatch`, `overridden_on_arrival`) and `supersedes` links. It states no cause.
- The demo's calibration `drift` claims are newer than graph-schema 1.6.0, the pinned version. A packet
  cannot describe them (ADR 0007 §6), yet "why do we believe the camera drifted" must still reach the two
  calibration files.

## Decision

1. **Trails in the packet: `query-packet` 1.2.0, additive.** `ContextPacket.trails` holds one trail per
   answered explain clause, ordered by clause. `WhyTrail(at, claim, steps)` and
   `DiffTrail(at, subject, nodes, before, after, changes)` live in `packets/trails.py`. They name claims by
   id. A claim current at the packet's snapshot is also a `ClaimItem`, and a step or change that names a
   carried claim must agree with it (assertion kind and evidence; predicate and subject). The JSON member is
   written only when a packet has a trail, so every existing packet keeps its bytes and id. An empty
   `trails` array is refused. That one member is the exception to ADR 0003 §6's "lists always present". The
   decoder refuses a change whose predicate is beyond the pin. `answer_problems` checks that each trail
   answers the clause at its pointer: the claim for a why, the subject and both points for a diff. Bounds:
   16 trails, 256 steps and depth 8 per why tree, 1024 claims and 64 nodes per diff.
2. **Claim history by id.** `explain.history.ClaimHistory` is `version(claim_id)` (the stored version, with
   its real `superseded_at`) plus `superseded_by(claim_id)`. `IndexedReader` is Memory's `ReferenceReader`
   with that index, built from the decoded `GraphDocument` (public schema types only). `read_graph` now
   returns it, and the MCP CLI's `local_client` uses it, so `neptune_why` answers. With a reader
   that lacks the seam, `why` is a `not_covered`
   gap that says so. `LocalEngine(..., history=...)` can pass the index explicitly.
3. **`why(c)` at Memory's snapshot.**
   - **Root.** The claim as Memory knows it then.
   - **Refusals.** Each is a gap at `/explain/i` with no trail: an unknown id, a claim recorded after the
     snapshot, a superseded claim (the gap names its successors and the `as_of` that shows it), and an
     inferred root when inference is excluded.
   - **Relations.** Below the root, in pre-order:
     - `corroborates`: same subject, predicate and object, overlapping on one clock;
     - `conflicts`: a resolver finding names both, and the step names that finding;
     - `alternative`: one of the two is a `*_candidate` claim of the predicate family, with another object,
       overlapping.
   - **Cycles.** Relations are symmetric, so the edge back to the parent is not repeated. Any other claim
     reached again is a `repeat` step and is not expanded.
   - **Evidence.** Every step cites its claim's evidence refs. A claim that cannot be carried still cites
     its evidence and is named in a gap: one beyond the pin, one on a clock the query neither asked for nor
     bridged, or one never current, such as an overridden inference.
   - **Caps.** Depth 3, 16 claims per step and 128 steps. Every cut is a gap naming the claims not
     followed. Inferred relatives are withheld in an `inferred_withheld` gap when inference is excluded.
4. **`diff(s, t1, t2)`.** It compares the claims whose subject or object is `s`, or a node `s` is declared
   `same_as` up to its depth (candidates are never followed). Subject identity is resolved once, at
   Memory's snapshot, for both axes. A transaction diff does not re-resolve `same_as` at `t1` or `t2`.
   - **Transactions.** The claims current at `t1` are compared with those current at `t2`.
     - `superseded`: a version current at `t2` with another object lists the old claim, through Memory's
       `supersedes` chain.
     - `closed`: the replacing versions keep its object over a narrower interval. That is a split closure,
       or a new lineage's restatement with the same start and an earlier end. A claim that nothing replaced
       (a retired lineage) is also `closed`.
     - `opened`: everything else that is new at `t2`.
     - `between`: a version met on a `supersedes` chain that was recorded and replaced inside
       `(t1, t2]`. Versions recorded and retired inside the window with no chain to a `t1` claim are not
       listed: Memory's reader has no by-node history.
   - **Instants on one clock.** The claims valid at `t1` are compared with those valid at `t2`, as known at
     the snapshot.
     - `closed` or `opened` as validity starts or ends.
     - `superseded` when a claim with the same subject and predicate took over at the very tick the old one
       ended.
     - The comparison is by fact (subject, predicate, object), so a fact held at both instants is no
       change, whichever claims carry it.
     - `between`: a claim valid at neither instant that starts after `t1` and before `t2`. It opened and
       closed inside the window and is listed, never dropped. On the demo, the cell's
       `configuration_unknown` period between configurations 1.4 and 1.5 is listed this way.
     - Claims on other clocks are an `other_clock` gap.
   - **Refused as gaps.** Instants on two clocks are not compared, even when bridged: no conversion is
     assumed. An `after` beyond Memory's snapshot is a gap at `/explain/i/after`.
   - **Carrying.** Changed claims current at the snapshot are carried. Older versions are named, and the
     human rendering links each to `why` at the transaction before the change.
   - **Ordering and caps.** Changes are ordered by predicate, then change kind, so they read grouped by
     predicate. The cap is 512 claims, and a cut is a gap.
5. **Engine.** `explain.Explainer` runs the clauses with the graph channel's admission rules (pin, inference,
   clock) and returns two answers.
   - A graph answer of the carried claims, scored `weight × 0.5^depth` for why and `weight` for diff.
   - A catalog answer of one `EvidenceItem` per cited source ref, resolved through `CatalogApi.resolve` at
     `as_of` and scored at half its best citing claim. An unresolvable source is also an `unresolvable`
     gap. Without a catalog, one `not_covered` gap at `/explain` says the evidence is cited, not resolved.

   The engine folds each answer into the channel answer with the same channel (best score per item, as
   within one channel), then fuses as before. Fusion still sees peers, not a privileged explain channel.
   The explainer's settings enter the engine's config hash. One failing clause is a gap, not a failed query.
6. **Human rendering.** `explain.markdown.render_markdown(packet)` writes the header, each why trail as a
   nested list, each diff by predicate (opened, closed, superseded with "narrowed to" or "replaced by"),
   the remaining items, supersessions since the snapshot, findings, gaps, and an evidence list. Inferred
   steps and items are marked **INFERRED** with model and confidence.
   - **Links.** `neptune://claim/<id>?as_of=N` opens `why` at `N`. `neptune://evidence/<token>?as_of=N`
     hydrates through the Ledger and is the MCP server's resource URI, so one link works in both.
   - **Quoting.** Every value from evidence is a JSON literal in a code span longer than any backtick run
     in it, with line breaks escaped. Prose is Markdown-escaped, and any bare URI or identifier in it
     (such as a gap detail quoting upstream text) goes in a code span. Source text cannot start a
     heading, forge a link or pose as a claim. The console follows only real
     `[...](neptune://...)` links, never a URI that merely appears in text.
   - The renderer adds no fact.

## Alternatives considered

- **Derive trails from the query and packet on the consumer's side.** No contract change, but a transaction
  diff's old versions and supersession chains are not in a single-snapshot packet. Every consumer would also
  re-implement the rules. Lost.
- **Carry old versions as claim items with their real `superseded_at`.** That changes what a claim item means.
  A 1.1 renderer that ignores the change would present a superseded claim as current. Lost: old versions are
  named, never carried.
- **An `explain` retrieval channel.** `Channel` is part of the packet contract. A fifth peer would compete
  with the graph channel for the same claims and double their fused score. Lost: explain hits join the graph
  and catalog answers.
- **Scan Memory for a claim id.** The reader has no enumeration, and walking every node per `why` reads the
  whole graph. Lost: `ClaimHistory`.
- **Treat "same record" or "co-occurring event" claims as support.** Memory states those as context, not as
  support, and a trail must add no reading. Lost.
- **Carry instants across clocks through a bridged mapping, as the graph channel carries windows.** Doable
  later. Comparing an instant converted through an estimated map with a stated one needs its own rules for
  the estimate's residual. Deferred to a gap.

## Consequences

- MCP `neptune_why` and `neptune_diff` and `Client.why` and `Client.diff` answer real trails with their
  signatures unchanged. The agent renderer (MVL-147) may render `trails`. Packets it reads without trails are
  unchanged.
- Deploy and Learn may raise their `query-packet` lock to 1.2.0. A 1.1 reader refuses a packet with trails as
  `shape` and never misreads it.
- `read_graph` returns `IndexedReader`, a `ReferenceReader` subclass. A store-backed Memory reader needs a
  by-id lookup to answer `why` (a request to Memory).
- Deploy's console must resolve the two link schemes: `why` for claim links and `hydrate` for evidence
  links.
- **Revisit when:**
  - Memory publishes a by-id read or support relations;
  - graph-schema moves past 1.6.0 (drift claims become carried items);
  - a consumer needs cross-clock instant diffs or pages of long trails.
