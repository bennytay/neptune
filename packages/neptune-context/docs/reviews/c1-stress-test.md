# C1 gate: stress test of the query and packet contracts with consumer personas

- Issue: MVL-111 (gate of milestone C1: Query Contract, Packets, SDK & MCP). Date: 2026-10-05.
- Under review: Context ADRs [0002](../adr/0002-query-language.md) (query),
  [0003](../adr/0003-the-context-packet.md) (packet) and [0004](../adr/0004-sdk-and-mcp-server.md)
  (SDK, MCP); `neptune_context.query`, `packets`, `render`, `sdk`, `mcp`; the ten golden query/packet pairs.
- Method: every scenario was run, not walked on paper. Each one is a test in
  `tests/test_c1_stress_context.py` that drives the real SDK `Client` or the MCP server (in process, over
  the official `mcp` client session). Answers come from the golden stub, or from an engine that answers any
  valid query coherently, as C2's will. Each attack is a hostile engine (an SDK test) or a hostile packet
  document (a reader test). Hostile documents are forged with correct ids, so only the rule under test can
  catch them.
- Outcome: **GO.** Five of the twenty cases broke. [ADR 0006](../adr/0006-c1-gate-query-packet-1-0-0-and-answer-checks.md)
  and code in this PR fix every break. **`query-packet` 1.0.0 is published stable** in
  `contracts/query-packet/v1.0.0/`, with the query and packet halves, thirty goldens and the owner module
  `neptune_context.contract`. All five Context ADRs on this branch (0001–0004, 0006) are Accepted. C2 may start once this PR merges and `main`
  is tagged `c1-gate`. C2 also needs Ledger's `l4-gate`.

## Verdict

| # | Persona or attack | Scenario (test) | Outcome | Resolution |
|---|---|---|---|---|
| 1 | Fleet engineer | Night-time fault on a UTC window (`..._night_window_is_answered_on_the_civil_clock_it_names`) | **breaks**: the packet said which clock, the rendered text did not | ADR 0006 §4: the world-time line |
| 2 | Fleet engineer | Same question at a stale `as_of` (`..._at_a_stale_snapshot_sees_what_changed_since`) | holds | — |
| 3 | Attack: a query that mixes clocks | A diff across UTC and a device clock, unbridged (`test_attack_a_query_that_mixes_clocks_never_reaches_the_engine`) | holds: `cross_clock_without_mapping`, the engine is never called; bridged it passes | — |
| 4 | Attack: mixed clocks in the answer | The engine moves the window to UTC but keeps claims on the log's clock (`..._answers_on_another_clock_is_refused_at_the_sdk`) | **breaks**: accepted as data | ADR 0006 §2: `answer_problems`, run by the SDK |
| 5 | Safety lead | Inferences included and marked; inferences excluded and named (`..._reads_inferences_marked_and_withheld_ones_named`) | holds | — |
| 6 | Attack: inferred posing as stated | A stated configuration or scene over an inferred claim (`..._resting_on_an_inferred_claim_is_refused`) | **breaks**: rendered without `INFERRED` | ADR 0006 §3 |
| 7 | Attack: a self-contradicting header | `inference_included = true` with an `inferred_withheld` gap (`..._says_some_was_withheld_is_refused`) | **breaks**: accepted | ADR 0006 §4 |
| 8 | VLA policy at 10 Hz | Two-item budget, truncation explicit, window on the asked clock (`test_vla_policy_gets_a_truncated_packet_that_says_so`) | holds | — |
| 9 | Attack: a packet over budget | Use above a limit, ids correct (`..._over_its_budget_is_refused_by_the_reader`) | holds: `budget` | — |
| 10 | Attack: a widened budget | The engine echoes 1000 items for a 2-item query (`..._widens_the_budget_is_refused_at_the_sdk`) | **breaks**: "within budget" | ADR 0006 §2 |
| 11 | Simulator setup | Scene around the arm's tool frame (`test_simulator_gets_a_scene_with_nothing_invented`) | holds: no node invented, `not_covered` spatial gap | — |
| 12 | Simulator setup | A packet frame reused in a query region (`..._maps_a_packet_frame_into_a_query_frame_by_one_key`) | holds, with a recorded naming difference | ADR 0006 §7 |
| 13 | Training-data curator | Runs and windows, then a pinned replay (`test_curator_cites_items_and_snapshot_...`) | holds: the replay is another query id, so curators cite item ids and `as_of` | ADR 0006 §7 |
| 14 | Auditor | Unresolvable evidence; claims on another clock (`..._sees_unresolvable_evidence_and_claims_on_another_clock_named`) | holds | — |
| 15 | Attack: stale `as_of` | Memory trails the Ledger, and a supersession falls between the two (`test_attack_a_stale_memory_snapshot_...`) | **breaks**: the packet could not list it, and nothing showed the lag | ADR 0006 §4 |
| 16 | Attack: a gap that points nowhere | A gap at `/site` for a query with no site (`..._gap_pointing_outside_the_query_is_refused_at_the_sdk`) | breaks, folded into case 4's fix | ADR 0006 §2 |
| 17 | Incident-reconstruction agent (Demo v1, MCP) | `neptune_query`, then `neptune_why`, a resource link read, a cross-clock `neptune_diff` refused, the same diff bridged through `neptune_query` (`test_incident_agent_reconstructs_...`) | holds | — |
| 18 | Incident agent | Omits `include_inferred` (`test_incident_agent_cannot_skip_the_inference_choice`) | holds: `invalid_argument` | — |
| 19 | Attack: a renderer that would fabricate | Every golden, rendered: identifiers only from the packet, citations round-trip, scope stated (`test_every_golden_answer_is_unambiguous_about_its_scope`, `test_render_citations_context.py`) | holds after case 1's fix | ADR 0006 §4 |
| 20 | Carried consistency defects (#121) | A plain `"out"`, and a mixed enum/str set's pointer (`test_query_validate_context.py`) | **break**: same id, different verdict; wrong index | ADR 0006 §5 |

Five persona-facing breaks (cases 4, 6, 7, 10 and 15) were defects of the contract or the SDK. Cases 1 and
16 were defects of what an answer states. Case 20 was carried from MVL-108's review.

## The personas' walk

Each persona's questions are golden queries (ADR 0003 §10) or ADR 0002's worked queries, sent through the
SDK. Every embodiment appears: an AMR fleet, a drone, a quadruped, an arm cell, a site's camera, a warehouse
AMR and an autonomous truck.

- **Fleet engineer, night-time fault.** A UTC night window over the north AMR fleet (ADR 0002 Q01) comes back
  with `during` resolved to the civil clock's `TimestampDomain` id. The answer's text now says
  `World time: ticks [..) on clock rec:sha256:...`. Before the gate, an agent reading the text could not tell
  the answer was cut to that window. At transaction 3 the drone question (q02) lists the claim superseded at
  4 and what superseded it. At head (q01) nothing has changed. Holds.
- **Safety lead, audit.** q03 (inference included) marks its one inferred claim
  `INFERRED by "<model> <version>", confidence <state>`. q06 (excluded) lists the withheld inferred claim ids
  under "Not answered" and carries none. The attack is a configuration that says `stated` while it rests on
  that inferred claim. Before the gate it was a valid packet, and it rendered as plain `stated`. Now the
  packet reader refuses it (`assertion_mismatch`), so an engine cannot produce it either.
- **VLA policy at 10 Hz.** q06 asks for two items within 100 ms, evidence only. The answer has two items,
  says one was cut by the items budget, and puts its series window on the asked clock. Measured on the
  golden stub (i5-13600K, 500 runs): `Client.query` (validate, engine, `answer_problems`) takes 0.10 ms p50
  and 0.10 ms p95. Decoding the packet off the wire takes 0.43 / 0.46 ms (3.2 kB; q01's 11 kB: 1.9 / 2.0 ms).
  Rendering takes 0.02 ms. The contract costs well under 1% of a 100 ms frame. What is not built yet is the
  engine's latency (MVL-152) and reading the window's rows (no Ledger series read yet; the Arrow handle
  names package and path).
- **Simulator setup.** The tool-frame scene (q07) has no placed nodes and `site` `NotCovered`. The renderer
  writes `no nodes`, and the spatial gap points at `/regions/0` of the query. A simulator that reuses a
  packet's frame in a query renames one key (`frame_graph_id` to `graph_id`), recorded in ADR 0006 §7.
- **Training-data curator.** QUAD-03's runs and sensor windows (q09) come with episodes `not_covered`.
  Replaying a `head` query pinned to its `as_of` is a different query with a different id. A dataset
  therefore cites item ids (content-addressed, no relevance) and `as_of`, never a packet id.
- **Auditor.** "Why UAV-0043" (q04) shows the unresolvable citation as a gap, and its citation keys
  round-trip. The drone on its own log clock (q05) names the claims that hold on another clock. None of them
  is an item.
- **Incident-reconstruction agent (Demo v1, over MCP).** In one session:
  1. `neptune_query` (the AMR-07 risk text) returns cited text with `[E1]` keys and links;
  2. `neptune_why` on the UAV claim returns q04's packet, and reading its evidence link hydrates through the
     Ledger (`unresolvable`);
  3. `neptune_diff` between a UTC inspection and the truck's vehicle clock is refused with
     `cross_clock_without_mapping`;
  4. the same diff sent as `neptune_query` with a `clock_bridges` entry reaches the engine.

  Leaving out `include_inferred` is `invalid_argument`.

## The fixes

| Fix | Where | Tests |
|---|---|---|
| `answer_problems(query, packet)`: query id, pinned snapshot, inference flag, exact budget echo, the `during` echo, timed items on asked or bridged clocks, gap pointers into the query; the SDK refuses any problem as `invalid_response` | `answer.py`, `sdk/client.py` | `test_answer_context.py`, cases 4, 10, 16 |
| An item over an inferred claim is inferred | `packets/model.py` (beside the dangling-reference check) | `test_packet_model_context.py`, case 6 |
| `inferred_withheld` only when inference is excluded | `packets/model.py` | case 7 |
| `superseded_since` covers `(memory_snapshot.as_of, head]` | `packets/model.py` | case 15 |
| Text states a trailing Memory snapshot and the world-time window | `render/citations.py` | `test_render_citations_context.py`, cases 1, 15, 19 |
| A plain string equal to an enum member is that member; `accept` substitutes the member; set pointer indexes follow the codec | `query/shape.py`, `query/decode.py` | `test_query_validate_context.py`, case 20 |
| `query-packet` 1.0.0: `contract_schema` (both halves embedded verbatim), `QUERY_PACKET_VERSION`, goldens, owner tests; the cross-test that decodes every golden query with the query reader and matches the packet's `query_id` | `contract.py`, `contracts/query-packet/`, `test_contract_context.py`, `test_packet_goldens_context.py` | owner rule (`make contracts-check`) |
| From the gate's code review of the SDK and MCP server: the `mcp>=1.19` floor; `hydrate` checks the locator; a sync client refuses an async `hydrate`; a fresh timeout error per raise; an unmapped wire status takes the body's code; the URI `as_of` bound; CLI exit 2 on unreadable packets | `pyproject.toml`, `sdk/client.py`, `sdk/http.py`, `sdk/wire.py`, `mcp/server.py`, `mcp/__main__.py` | `test_sdk_client_context.py`, `test_sdk_http_context.py`, `test_mcp_server_context.py`, `test_mcp_cli_context.py` |

No golden packet changed. Every fix refuses something the goldens never did, and the one relaxation
(case 15) admits more.

## Carried items

1. **Publish `query-packet` 1.0.0** (MVL-109): done.
   - It was published with `scripts/contracts.py bump query-packet 1.0.0`. The bump added
     `query-packet = "1.0.0"` to `neptune-deploy`'s lock and regenerated `contracts/compatibility.md`.
   - Root `tests/unit/contracts/test_contracts_registry.py` used `query-packet` as its example of a planned
     contract. It now uses `dataset-manifest`.
   - The platform harness smoke test now expects the first published packet golden instead of the canned
     packet.
2. **Cross-test of golden queries** (MVL-109): done. All ten decode with `query.loads`, re-encode byte for
   byte and hash to their packet's `query_id`. Regeneration was not needed.
3. **Packet invariants** (#119 review): done, ADR 0006 §3 and §4.
4. **Shape order and plain strings** (#121 review): done, ADR 0006 §5. The rule is to coerce, because
   equal values must get equal verdicts.
5. **SDK re-export** (MVL-110): `neptune_context.contract` does not re-export the SDK (ADR 0006 §6).
6. **Harness** (MVL-123): **harness green at 82eb40c8** (CI run 37311818177, merged with `main`
   e3933188, which carries the X1 gate's real Ledger stage). Report: `harness green | contracts ok |
   compiler: real ok | ledger: real ok | memory: stub ok | context: stub ok`. Context now serves
   `query-packet 1.0.0`, and the smoke query reads the published golden
   `packet.q01-fleet-engineer-who-recorded-the-drone-run.json` instead of the canned packet.

## ADR status after the gate

| ADR | Status | Changed by the gate |
|---|---|---|
| 0001 | Accepted | — |
| 0002 | Accepted | amended by 0006 (§7 plain strings and pointer order; the Consequences sentence on embedding the query is superseded by 0003 §1) |
| 0003 | Accepted | amended by 0006 (§3 items over inferred claims, §4 the `superseded_since` window and `inferred_withheld`, §7 snapshot lines in text) |
| 0004 | Accepted | amended by 0006 (§3 the answer checks; §2, §4 and §6 hardening, 0006 §8) |
| 0006 | Accepted | new: query-packet 1.0.0, answer checks, amendments |

ADR 0005 is reserved for MVL-185 (the natural-language planner).

## What C2 and the consumers inherit

- **MVL-144 (graph channel):**
  - keep every timed item on `during`'s clock or a bridged one, and name the rest in an `other_clock` gap;
  - compute `superseded_since` from Memory's snapshot;
  - mark scenes and configurations over inferred claims `inferred`.
- **MVL-145 (fusion and cut-off):** echo the query's budget exactly, and set `dropped` and `exhausted`
  together.
- **MVL-152 (latency):** `latency_ms` is echoed and checked, but nothing enforces it on the call yet. The
  contract's own cost is measured above. `HttpEngine`'s deadline covers only the body. Connecting and
  reading the status line and headers are bounded only by the socket timeout per operation, so a server
  that trickles headers is never cut off.
- **MVL-147 / MVL-148 (renderers):** keep the citation property and the scope lines (snapshot, world time).
  Policy windows need a Ledger series read before rows can be hydrated.
- **Deploy (MVL-116) and Learn (MVL-120):** build against `contracts/query-packet/v1.0.0` and
  `neptune_context.contract`. Run `check_packet` on the packets you read and `answer_problems` when you
  hold the query. Cite item ids and `as_of`.
- **Coordinator, after merge:**
  1. post the two announcements `scripts/contracts.py bump` printed, for MVL-116 and MVL-120;
  2. tag `c1-gate`;
  3. move `contracts/packages.toml`'s `neptune-context.gate_issue` to the C2 gate when C2's issues start.
