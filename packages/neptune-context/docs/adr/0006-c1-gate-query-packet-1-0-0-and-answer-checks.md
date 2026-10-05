# 0006 — C1 gate: query-packet 1.0.0, answer checks, and the amendments the personas forced

- Status: Accepted
- Date: 2026-10-05
- Issue: MVL-111

## Context

The C1 gate ([review](../reviews/c1-stress-test.md)) ran six consumer personas and the issue's attacks
against the query language ([ADR 0002](0002-query-language.md)), the context packet
([ADR 0003](0003-the-context-packet.md)) and the SDK and MCP server ([ADR 0004](0004-sdk-and-mcp-server.md)).
Five cases broke. Each break let a wrong or ambiguous answer through as data:

- an engine that answers on a clock the query never asked for or bridged;
- an engine that widens the budget it echoes, so its packet looks "within budget";
- a stated scene or configuration that rests on inferred claims, rendered without `INFERRED`;
- an `inferred_withheld` gap in a packet whose header says inference is included;
- a Memory snapshot that trails the Ledger: supersessions between the two snapshots could not be listed,
  and nothing rendered said the claims were older than `as_of`.

Two consistency defects came from earlier reviews. Plain strings such as `"out"` equal their enum members
and share a `query_id`, yet `validate` refused them. `shape` findings indexed set members in a different
order from the canonical JSON. The registry contract also had to be published, which ADR 0003 §9 defers to
this gate. A contract changed after 1.0.0 needs a version bump, so every fix lands before publication.

## Decision

1. **`query-packet` 1.0.0, stable.** The owner module is `neptune_context.contract`.
   - `contract_schema()` is the one registry export. It embeds the query schema and the packet schema
     verbatim, each as its own schema resource with its own `$id`: `#/$defs/Query`
     (`urn:neptune:schema:query:1`) and `#/$defs/ContextPacket` (`urn:neptune:schema:context-packet:1`).
     The two halves keep their own definition names, so the query's `FrameRef` and the compiler's never
     collide.
   - `QUERY_PACKET_VERSION = 1` is the registry major. It rises whenever `QUERY_VERSION` or `PACKET_VERSION`
     does.
   - Goldens come from `contracts/query-packet/goldens.py`: `packet.<qNN-...>.json` and
     `query.<qNN-...>.json` (the ten persona pairs) and `query.worked-qNN.json` (ADR 0002's ten).
   - Owner contract tests: `test_contract_context.py`, `test_packet_goldens_context.py`,
     `test_answer_context.py`.
   - The first stable version adds `query-packet = "1.0.0"` to `neptune-deploy`'s lock. `neptune-learn` has
     no package yet.
   - The module also exports the readers (`loads`, `decode_packet`), the canonical bytes and ids,
     `check_packet` (ADR 0003 §9's conformance) and `answer_problems` (§2).
2. **A packet is checked against the query it answers.** `neptune_context.answer.answer_problems(query,
   packet)` lists every way the packet fails to answer the query. The packet model cannot run these checks,
   because a packet names its query only by id. The checks:
   - `query_id`, a pinned integer `as_of` and `include_inferred` match the query;
   - the budget limits equal the query's `Budget` exactly (items, tokens, bytes, latency_ms);
   - `during` is the query's window on the query's clock (a civil time resolves to its
     `CivilClock.domain_id`), or absent when the query set none;
   - with a `during`, every timed item must be on that clock or on a clock that the query's
     `clock_bridges` join to it. The timed items are a claim's valid interval, a series window's clock and
     a sensor sample's instant. A diff's instants add their own clocks. Anything else on another clock
     belongs in an `other_clock` gap (ADR 0002 §3);
   - every gap's `at` resolves in the query's canonical JSON.

   The SDK runs `answer_problems` on every answer, and any problem is `invalid_response`. This amends
   ADR 0004 §3, which checked only the first three.
3. **An item is no stronger than the claims it rests on.** A `SceneItem` or `ConfigurationItem` that names an
   inferred claim (carried as a `ClaimItem` in the same packet) must itself be `inferred`. That means it
   names its model, has `Known(p)` or `Unknown` confidence, and renders `INFERRED`. Otherwise the packet is
   refused with `assertion_mismatch`, a check that sits beside the dangling-reference check. A deterministic
   item may rest on observed or stated claims. This amends ADR 0003 §3.
4. **Snapshots are explicit.**
   - `superseded_since` covers `(memory_snapshot.as_of, head]`, not `(as_of, head]`. Claim items are as Memory
     knew them at its own snapshot, which may trail the packet's `as_of`. Every supersession since then must
     be listable. The change only relaxes the old rule, so every packet valid before stays valid.
   - An `inferred_withheld` gap is refused (`inference_excluded`) when `inference_included` is true.
   - Text renderers state Memory's snapshot when it trails `as_of`, and the world-time window on its clock
     when `during` is set. "Changed since" counts from Memory's snapshot.

   This amends ADR 0003 §4 and §7.
5. **A plain string that equals an enum member is that member.** Two equal queries share a `query_id`, so
   they get the same verdict. `"out"` for `Direction.OUT` and `"claim_text"` for `TextField.CLAIM_TEXT`
   validate as the members. Any other string is still `shape`. `accept` returns the query with the members
   substituted. The result is an equal query with the same bytes and id, so an engine only sees enums.
   Pointer indexes of set members follow the codec's canonical order: `sorted(str(m))` for enum sets,
   string order for string sets. This amends ADR 0002 §7.
6. **`neptune_context.contract` does not re-export the SDK.** The contract is the data that crosses
   packages: schemas, versions, readers and checks. `Client`, the engines and the wire are a library over
   that contract. They are versioned with the package, and the registry does not version them. A consumer
   imports `neptune_context.sdk`. Re-exporting them would make every SDK change a contract change.
7. **Recorded and kept.**
   - The query's `FrameRef` is written `{frame_id, graph_id}`. The compiler's (in packets) is
     `{frame_id, frame_graph_id}`, so a consumer that copies a frame from a packet into a region renames one
     key. Renaming would change the bytes, and so the id, of every region query, including ADR 0002's
     worked ids, for no gain in meaning.
   - ADR 0002's Consequences say a packet "embeds the query's canonical JSON". ADR 0003 §1 decided
     otherwise: a packet names its query by `query_id` only. This ADR records ADR 0003 as the rule.
   - `neptune_diff` and `Client.diff` take no clock bridges. A cross-clock diff is a `neptune_query` with
     `explain` and `clock_bridges`, and the tool description says so.
   - A query at `head` and its replay pinned to the answer's `as_of` are two queries with two ids. A
     dataset or audit cites item ids (content-addressed, independent of relevance) plus `as_of`, not the
     packet id.
   - `latency_ms` is echoed and checked equal (§2). Enforcing it on the call belongs to the engine and to
     `eval/` (C2), not to the packet.

8. **SDK and MCP hardening from the gate's code review.** These amend ADR 0004 §2, §4 and §6.
   - The `mcp` floor is `>=1.19,<2`, the first release whose low-level server accepts a returned
     `CallToolResult`.
   - `hydrate` checks that the resolution's locator equals the ref's, not only its source.
   - A sync `Client` refuses an engine whose `hydrate` is a coroutine function (`inspect`, not the
     deprecated `asyncio` test).
   - Each timeout raises a fresh error.
   - A wire status the table does not map takes the error code its body names. A server's deterministic
     `invalid_response` is therefore not retried as an outage.
   - An evidence URI's `as_of` has at most 19 digits and is at most 2^63 − 1.
   - The CLI exits 2 on an unreadable packet directory.

9. **Upstream vocabularies come from the pins, never from live owner code.** Subject kinds (graph-schema
   node types and catalog-api thread kinds), predicate names, and the Memory definitions the packet schema
   embeds are read from `neptune_context/pinned.json`. That file is a snapshot of
   `contracts/graph-schema/v<pin>/` and `contracts/catalog-api/v<pin>/` at the versions in `pins.py`, and a
   test fails when it differs from the registry. `validate`, the query schema, the packet schema and the
   planner's prompt all read it. An upstream release (a new Memory predicate or node type, a new Ledger
   thread kind) therefore changes nothing Context publishes: not the export, not a query id, not a prompt
   hash. A test adds all three to the live upstream code and proves it. Adopting them is a pin bump: a
   reviewed Context change that regenerates the snapshot, the query and packet schemas, the planner
   recordings and a `query-packet` minor version.
   - The query schema keeps the subject-kind and predicate enums (ADR 0002's Consequences), now sourced
     from the pins. A pin bump already changes the packet half, because it embeds graph-schema's
     definitions, so moving the enums out would not avoid the minor version. Widening an input enum is
     additive: every 1.0 query stays valid. The MCP tool schema and the planner keep the vocabulary as
     machine-readable constraints.

## Alternatives considered

- **Two registry contracts, `query` and `packet`.** Each half could version alone. But consumers always need
  both, since a packet is meaningless without the query it answers. ADR 0002 §9 and ADR 0003 §9 already
  name one contract. Lost.
- **Merge both schemas' definitions into one flat `$defs`.** The two `FrameRef` definitions differ, so one
  half would have to rename a definition, and the export would no longer equal either half's schema.
  Embedded resources keep both verbatim. Lost.
- **Put the answer checks in the packet model.** The model would have to import the query model, which
  ADR 0003 §1 forbids so that the halves evolve independently. A function that takes both is enough. Lost.
- **Refuse plain strings in the codec too.** `query_id` would then raise on a value Python treats as equal to
  a valid query, and the codec would need a type walk of its own. Coercion follows equality. Lost.
- **Keep `superseded_since` at `(as_of, head]`, and forbid a trailing Memory instead.** ADR 0003 §2 allows
  Memory to trail the Ledger by design: consolidation is asynchronous. Forbidding the lag would make every
  packet wait for Memory. Lost.

- **Declare predicates and kinds as pattern-constrained strings, with membership checked only by
  `validate`.** The query half would stay fixed across pin bumps. But the packet half changes on every bump
  anyway, and agents and the planner would lose the vocabulary as schema constraints. Lost (§9).
- **Read the registry under `contracts/` at run time.** This avoids the snapshot, but an installed package
  has no registry. The snapshot ships in the wheel, and a freshness test ties it to the registry. Lost (§9).

## Consequences

- Deploy and Learn build against `contracts/query-packet/v1.0.0` and `neptune_context.contract`. They run
  `check_packet` on packets and `answer_problems` when they hold the query.
- C2's engine must keep every timed item on the asked or bridged clock, echo the budget exactly, point gaps
  into the query, and mark scenes and configurations over inferred claims as inferred. The SDK refuses
  anything else.
- A later change that an older reader would misread raises `QUERY_VERSION` or `PACKET_VERSION` together
  with `QUERY_PACKET_VERSION`, and becomes `query-packet` 2.0.0. New optional members are minor versions
  (ADR 0002 §9).
- Memory may hold values newer than Context's graph-schema pin, such as a node type or literal type added in
  a minor. The pinned packet schema does not describe them. Serving them requires a pin bump first, and
  C2's engine must report such a value as a gap rather than pass it through. A Memory or Ledger PR never
  regenerates a Context artefact (§9).
- Revisit when Memory's spatial or episode views land (scenes gain placed nodes), when the Ledger exposes
  series reads (the Arrow handle may gain a call), or if a consumer needs packet pages.
