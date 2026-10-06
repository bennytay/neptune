# 0012 — Context pins graph-schema 2.0.0; query-packet 1.3.0 stays additive

- Status: Accepted
- Date: 2026-10-06
- Issue: MVL-191

## Context

Memory published graph-schema 2.0.0 (Memory ADR 0019, PR #151). It is a major because a claim changed
meaning, not shape (graph-schema.md, "Migrating from 1.x to 2.0.0"):

- `succeeds` no longer marks a change on a machine's configuration chain. Configuration nodes are shared,
  so it read fleet-wide. A machine's change is now read from its own adjacent `has_configuration` spans
  (graph-schema.md guarantee 12); `succeeds` holds only where a source states it.
- Identity (consolidator version 3) may join `event` nodes an assertion names, so `same_as` claims are
  re-identified and can now join events as well as thread nodes.
- A 2.x graph document carries its full release (`"graph_schema": "2.0.0"`) beside the major
  (`graph_schema_version: 2`). Memory's codec still reads a 1.x document as written, labelled major 1.

Context was pinned at 1.6.0. The registry's lock rule (platform ADR 0002) makes a major lag fatal, and a
stable major `bump` raises every in-repo consumer's lock in the owner's PR. Memory raised Context's lock,
`pins.py` and `pinned.json` mechanically. Context had to say whether its own published contract
(`query-packet`) changes compatibly, and adapt its code and goldens in the same PR.

The vocabulary between 1.6.0 and 2.0.0 adds twelve predicates (`calibrated_by`, `calibrated_with`,
`calibration_candidate`, `drift`, `gap`, `integrity_finding`, `rate_declared`, `rate_observed`, `recorded`,
`sensor_not_recorded`, `sensor_presence_unknown`, `sensor_recorded`) and the `delta` literal (Memory
ADR 0014). Context previously reported these as `not_covered` gaps ("newer than Context's pinned
graph-schema"), and the Demo v1 snapshot's `drift` claims were dropped before reading because the codec of
the day could not decode `delta`.

## Decision

1. **Pin graph-schema 2.0.0.** `pins.py` holds `CATALOG_API_VERSION = "1.7.0"` and
   `GRAPH_SCHEMA_VERSION = "2.0.0"`; `contracts/lock.toml`, `docs/contracts.md` and `pinned.json` agree.
   ADR 0007 keeps the 1.6.0 pin as written; this ADR is the latest bump `tests/test_pins_context.py`
   checks.
2. **`query-packet` 1.3.0, a minor.** The export differs from 1.2.0 only by widening:
   `ContextPacket/$defs/Delta` is added, `TypedLiteral` gains the `delta` variant, and the predicate
   enums of `DiffTrail` changes and `Query/$defs/Graph` predicates gain the twelve names above (the
   predicate array's `maxItems` rises from 53 to 65, the enum's size). Nothing is removed or narrowed:
   every 1.x query and packet golden validates against 1.3.0 (`scripts/contracts.py bump` checked it).
   `QUERY_VERSION`, `PACKET_VERSION` and `QUERY_PACKET_VERSION` stay 1. Upstream's major does not make
   ours one, because the meaning change (`succeeds`) is not something Context's contract states.
3. **Context does not depend on `succeeds`.** No channel, explainer or renderer selects on it: `diff`
   follows `supersedes` chains and compares facts per instant (ADR 0010), and a configuration change
   reaches a packet as the machine's own `has_configuration` claims. A `succeeds` claim is carried like
   any other claim, with its provenance; its 2.x meaning is Memory's to state.
4. **Identity v3 needs no Context change.** `same_as` widening (ADR 0007 §3) is node-type agnostic, so
   it follows event identities as it follows thread ones. Packet goldens over Memory's published golden
   graph keep their bytes: that graph is a 1.x document, read as written.
5. **The packet names the major of the graph it read.** `memory_snapshot.graph_schema_version` is the
   document's own major (2 for a 2.x document, 1 for a 1.x one read as written), from the reader, never
   Memory's live constant. The golden generator follows the engine here. The full release string is
   checked by Memory's codec, which refuses a release of another major or a malformed one; Context reads
   documents only through it (`engine.read_graph_document`).
6. **`delta` literals are rendered as declared.** The agent renderer writes
   `delta <canonical JSON of the delta> (<unit>, as declared)`; the human renderer writes the same JSON
   in a code span with the literal's declared unit. Neither states a size, a direction or a verdict, and
   every string inside (a parameter's declared name) is hardened or code-quoted like any source text. The
   Demo v1 snapshot's two `drift` claims are now read and carried.
7. **Beyond-the-pin paths stay tested.** No released predicate is beyond 2.0.0, so the tests narrow the
   pin (`explain_fixtures_context.pin_without`) or use a predicate no release has
   (`retrieve_fixtures_context.BEYOND_PIN`). The gap, refusal and named-only rendering are unchanged.
8. **Who raises a graph-schema major.** The owner's PR raises Context's lock and pin (platform ADR 0002);
   Context signs off by adding, in that same PR, this ADR, the `query-packet` bump and whatever
   adaptation the major needs. Context never raises a graph-schema major on its own.

## Alternatives considered

- **`query-packet` 2.0.0 to mirror Memory's major.** Rejected: a major tells consumers an older reader
  would misread a packet. None would: every 1.x packet and query is valid at 1.3.0, and the packet's
  shape did not change. Deploy and Learn would have had to move their locks for nothing.
- **Keep Context on 1.6.0 until a later PR.** Not possible: a major lag fails `contracts.py check`, and
  the lock rule puts the consumer raise in the owner's PR.
- **Report packets at Memory's live major.** Rejected: a packet over a 1.x document would claim major 2
  while its `succeeds` claims carry 1.x's meaning. The packet states what was read.
- **Keep `drift` beyond the pin in the explain fixtures by editing the frozen graph.** Rejected: the
  graph is frozen so Memory releases cannot move Context's goldens (ADR 0010). Narrowing the pin in the
  test is enough to exercise the path.

## Consequences

- Calibration claims (`calibrated_with`, `calibrated_by`, `drift`) and the coverage and health predicates
  are now carried items with provenance instead of gaps. The trail golden `trail-w1` carries its `drift`
  root; the Demo v1 transcript carries two `drift` deltas and the three calibration evidence refs they cite.
- Deploy and Learn may raise their `query-packet` lock to 1.3.0 when they want the new predicates; until
  then the lock check warns (a minor lag).
- The planner's prompt embeds the query schema, so its template hash and the recordings'
  `request_sha256` change with every vocabulary widening; the recorded responses do not.
- Revisit if Memory narrows or removes a predicate, or changes a literal's shape: that would be a
  `query-packet` major.
