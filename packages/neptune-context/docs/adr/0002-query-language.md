# 0002 — The query language: subjects, time, space, graph, text, budget and explain, with as_of and during semantics

- Status: Accepted
- Date: 2026-10-05
- Issue: MVL-108

## Context

Every consumer of Context (an agent over MCP, a VLA policy at inference time, a simulator, Deploy's console,
Learn's dataset selection, an auditor) asks the same kind of question: *what does memory hold about these
things, here, then, as known at that point?* Memory answers bi-temporally (graph-schema 1: a claim is valid on
one clock and current between two Ledger transactions) and refuses to compare instants on different clocks;
the compiler stores frames, units and clocks as declared and relates them only through named records
(`ClockMapping`, `FrameTransform`). A query language that let a caller say "between 10:00 and 10:05" without
saying on which clock, or "within 2 of the gripper" without a frame and unit, would reintroduce exactly the
silent assumptions the lower layers refuse. The context packet (MVL-109) records which query it answers, so
the query also needs one canonical encoding and a stable id.

## Decision

1. **One typed query, plain data.** `neptune_context.query.Query` is a frozen dataclass: `subjects`, `as_of`,
   `during`, `clock_bridges`, `regions`, `frame_bridges`, `site`, `graph`, `text`, `budget`, `explain` and
   `include_inferred`. It says *what* to retrieve and on which clock, frame and snapshot, never *how*: the
   planner picks channels and none is privileged. Clauses combine by conjunction. Set-like members are
   `frozenset`s, so equal meaning is equal value. The JSON form (§6) is the one wire form; a textual form is
   deferred until the natural-language planner (C3) needs a human-editable rendering, and will be a pure
   rendering of the same JSON.
2. **Subjects.** `Subject(kind, declared_id=None, same_as_depth=0)`. `kind` is a graph-schema node type or a
   catalog-api thread kind (the union of the pinned vocabularies). With no `declared_id` it selects every
   entity of that kind; with one (`<namespace>:<value>`, a compiler token then a value that is neither blank
   nor padded) it selects that entity, widened along declared `same_as` edges up to `same_as_depth` (0–3).
   `same_as_candidate` is never followed: a candidate is not an identity. A kind-wide selector has depth 0.
3. **Time: `as_of` and `during`.**
   - `as_of` is the **transaction clock**: a Ledger transaction (`int`, 0 to 2^63−1) or `"head"`. It fixes the
     snapshot: claims current at `as_of`, supersessions after it masked (Memory ADR 0006 §6). `"head"` is the
     default; the planner resolves it to one transaction and the packet records the number, so every answer
     is replayable from its packet.
   - `during` is **world (valid) time**: `[start, end)` in integer ticks (`end` may be `"open"`) on one named
     clock, either a source clock (`DomainClock`: a compiler `TimestampDomain` record id) or a civil time
     (`CivilTime`: absolute timescale `utc|tai|gps|posix`, absolute epoch `unix|gps`, exact resolution as a
     fraction in lowest terms; Memory's `CivilClock` rule). Never a float, never a bare number, never local
     time. Two civil times with different resolutions are different clocks.
   - **Cross-clock is named or not at all.** A claim valid on another clock is not refused and not coerced:
     Memory returns it apart (`other_clocks`, graph-schema guarantee 8). It is placed on `during`'s clock
     only through a `ClockBridge(mapping_id, source, target)` naming one `ClockMapping` record; the planner
     checks the record joins those clocks and labels anything placed through an estimated mapping
     `inferred`. A `diff` across two clocks must be joined by a chain of bridges, and every bridge must reach
     a clock the query uses (no bridge is silently ignored).
4. **Space.** Two independent scopes, combinable:
   - **Frame plus region**: `FrameRegion(frame, unit, shape)` with the compiler's `FrameRef`
     (frame id verbatim within one declared frame-graph record), a declared length unit (a catalogued
     canonical symbol: `m`, `mm`, `ft`, ...; nothing converts) and a shape (axis-aligned `Box` with
     `min < max`, or `Sphere` with positive radius), coordinates finite. Several regions must share one unit
     and either one frame or a chain of `FrameBridge(transform_id, parent, child)` naming `FrameTransform`
     records; a bridge must reach a region's frame.
   - **Site plus zones**: `SiteScope(site, zones)`, declared ids. Topological, so it needs no frame.
5. **Graph, inference, text, budget, explain.**
   - `GraphClause(predicates | "any", hops 1–4, direction out|in|both)` follows claims from the anchors: the
     declared subjects and the site. A graph clause with no anchor is refused (it would walk the whole
     graph). Predicates come from the pinned graph-schema vocabulary.
   - `include_inferred: bool` has **no default** in the type or the wire form. `default_include_inferred`
     gives the conventional choice (`False` for a control policy, `True` for an agent), but the query always
     records the value; inferred items are labelled in the packet either way.
   - `TextClause(text, fields, channels)`: free text (non-blank, at most 2000 code points, no control
     characters) routed to the `lexical` (BM25) and/or `vector` channels, scoped to fields (`declared_id`,
     `claim_text`, `record`, `document`, `finding`). Channels are peers; a vector score is a score, never a
     claim.
   - `Budget(items, tokens=None, bytes=None, latency_ms=None)`: `items` is mandatory (1–10 000); any other
     limit left unset is unbounded. A latency budget is the caller's; `eval/` asserts the engine's own.
   - `explain`: an ordered tuple (at most 16, no repeats) of `Why(claim_id)` (evidence, transform,
     supersession and findings of one claim) and `Diff(subject, before, after)` (what changed about one
     declared subject between two Ledger transactions, or between two world-time instants; never one of
     each; `before` earlier than `after`; a transaction diff may not look past an integer `as_of`).
6. **Canonical form, id and schema.** `codec.to_json` writes one shape: list members always present, absent
   single clauses omitted (no `null`), set members ordered by their own canonical JSON bytes, coordinates as
   floats with `-0.0` written `0.0`, `query_version` included. `codec.canonical_bytes` is the compiler's
   canonical JSON of that value; **`codec.query_id` is `query:sha256:` + the SHA-256 hex of those bytes**
   (`QUERY_ID_PATTERN`). Equal queries have equal bytes and ids on every machine; the context packet, caches
   and logs name a query by this id. `decode.loads` reads JSON text strictly (at most 64 KiB, UTF-8, no
   duplicate keys, no NaN or Infinity, no lone surrogates, no unknown members) and never raises on hostile
   input. The draft 2020-12 JSON Schema (`query.schema.query_schema`, exported to
   [`docs/schema/query.schema.json`](../schema/query.schema.json), `$id urn:neptune:schema:query:1`)
   describes the shape and per-member bounds; a document the schema rejects is always refused, one it accepts
   may still be refused by §7.
7. **Refusal is structured.** `decode.loads`/`from_json` return a `Query` or `Refused(findings)`; `validate`
   returns every finding in a fixed order. A `QueryFinding` has a stable `code` (`FindingCode`), a JSON
   pointer `at` into the canonical JSON and a deterministic message. Any finding refuses; there are no
   warnings. Codes cover shape, version, duplicates, bounds, identifiers, unknown kinds and predicates, bad
   clocks, intervals, units, regions and text, and the meaning rules: an empty query, `same_as` without an
   id, an unanchored graph clause, a cross-clock diff without a mapping, regions in different frames
   without a transform or in different units, and dangling bridges.
8. **Bounds.** At most 64 subjects, 16 regions, 64 zones, 16 bridges of each kind, 16 explain items,
   `same_as_depth` 3, 4 hops, 2000 text characters, 10 000 items, 10^6 tokens, 64 MiB, 600 000 ms; ticks
   and transactions are int64. Each bound has a boundary test.
9. **Versioning.** `QUERY_VERSION = 1` (an integer, carried as `query_version` in every document). A reader
   refuses any other value. Raise it for any change an older reader would misread or that changes the
   canonical bytes of an existing query (and so its id): a new required member, a changed meaning or
   encoding, a removed or renamed finding code. A new optional member is additive (an old reader refuses a
   document that uses it as `shape`, never misreads it, and no existing id changes); so is a new finding
   code. The registry contract `query-packet` stays `planned` until the
   context packet lands (MVL-109 and its implementation); its first published version then registers
   `neptune_context.contract` as owner module with the query and packet schemas together.

## Worked queries

Ten queries from the five consumer personas, across embodiments. Each block is executed by
`tests/test_query_worked_context.py`: it must validate, encode byte for byte to the golden file
`tests/golden/worked-queries/qNN.json`, and hash to the `query_id` on its first line. Ids, clocks and transactions
are illustrative.

**Q01 — Fleet engineer: what were the AMRs of the north fleet running overnight?**

```python
# query_id: query:sha256:cd874ad9ac193efd2935030d74a086558ff235ee20aa74e7a34cbdf1c0bc1647
utc_ns = CivilTime("utc", "unix", Fraction(1, 1_000_000_000))
query = Query(
    include_inferred=True,
    budget=Budget(items=200, tokens=8000),
    subjects=frozenset({Subject("fleet", "fleet_registry:amr-north")}),
    during=During(utc_ns, 1_789_423_200_000_000_000, 1_789_452_000_000_000_000),
    graph=GraphClause(
        frozenset({"member_of_fleet", "runs_software", "has_configuration", "runs_model"}),
        hops=2,
        direction=Direction.BOTH,
    ),
)
```

**Q02 — Fleet engineer: which ROV runs or findings mention a thruster stall?**

```python
# query_id: query:sha256:551aac6dd2c598aeeaf1d25fa36f431714275764c98663b1c65bd7017e8511f0
query = Query(
    include_inferred=True,
    budget=Budget(items=50),
    subjects=frozenset({Subject("run")}),
    text=TextClause(
        "thruster stall during descent",
        fields=frozenset({TextField.FINDING, TextField.DOCUMENT, TextField.CLAIM_TEXT}),
        channels=frozenset({TextChannel.LEXICAL, TextChannel.VECTOR}),
    ),
)
```

**Q03 — Safety lead: where was the humanoid authorised, as known when the risk assessment was signed?**

```python
# query_id: query:sha256:de8b9cff96a3ebfc3540bcf22816c9ec9ee372ccfc5dc7283ea9b563fd0fd261
query = Query(
    include_inferred=False,
    budget=Budget(items=100),
    as_of=1842,
    subjects=frozenset({Subject("machine", "asset_tag:hx-02", same_as_depth=1)}),
    site=SiteScope("site_registry:plant-7", frozenset({"zone_map:cell-a", "zone_map:aisle-3"})),
    graph=GraphClause(
        frozenset({"located_at", "zone_of", "governed_by"}), hops=2, direction=Direction.OUT
    ),
)
```

**Q04 — Safety lead: what changed about AGV 114 across its requalification?**

```python
# query_id: query:sha256:80f501c2ab83dd673ea5e080836229eb444de1b33cb87f9d60d099ec68da63ce
agv = Subject("machine", "asset_tag:agv-114")
query = Query(
    include_inferred=False,
    budget=Budget(items=100),
    as_of=1842,
    subjects=frozenset({agv}),
    explain=(Diff(agv, before=1500, after=1842),),
)
```

**Q05 — VLA policy at inference: what is in the arm's workspace now, and its calibration?**

```python
# query_id: query:sha256:9bc7598e52baea9698b01c3d9fa24d8607cbd65f3290f2aa3870b7baf1992ec6
arm_clock = DomainClock("rec:sha256:af57ce58b44eba15d6343fbb4d33a1108fd14580d9f0179615781bc04b9e2005")
base = FrameRef(
    "base_link", "rec:sha256:618dc15d777252fe47d00effc1e229c38c20e7f73daf9ee710cf1e4f84576894"
)
query = Query(
    include_inferred=False,
    budget=Budget(items=32, tokens=2048, latency_ms=50),
    subjects=frozenset({Subject("machine", "cell:ur10e-04")}),
    during=During(arm_clock, 7_200_000_000, None),
    regions=frozenset({FrameRegion(base, "m", Box((-0.2, -0.6, 0.0), (0.9, 0.6, 1.1)))}),
    graph=GraphClause(
        frozenset({"has_calibration", "mounted_on"}), hops=1, direction=Direction.BOTH
    ),
)
```

**Q06 — VLA policy on an inspection drone: obstacles near the hover point, across map and odom frames.**

```python
# query_id: query:sha256:db9b4d6296b528edea763d924303fb1f668874f10541914e96f131e38e0f3ca5
uav_graph = "rec:sha256:2ca9456555865ee580ef2d11ac030ffed6ba09f19a192b8bda067a71e2ed669a"
map_frame, odom_frame = FrameRef("map", uav_graph), FrameRef("odom", uav_graph)
query = Query(
    include_inferred=False,
    budget=Budget(items=64, tokens=1024, latency_ms=100),
    subjects=frozenset({Subject("machine", "airframe:uav-21")}),
    regions=frozenset(
        {
            FrameRegion(map_frame, "m", Sphere((120.0, 48.5, 30.0), 15.0)),
            FrameRegion(odom_frame, "m", Box((-5.0, -5.0, -2.0), (5.0, 5.0, 2.0))),
        }
    ),
    frame_bridges=frozenset(
        {
            FrameBridge(
                "rec:sha256:e7ca009bf209fb0a9d6178d008df42126a28ddb3395c49961b3ba52b7d704fc6",
                parent=map_frame,
                child=odom_frame,
            )
        }
    ),
)
```

**Q07 — Simulator setup: every asset placed in two zones of the dock, for scene building.**

```python
# query_id: query:sha256:a23f2b04de83316242f0a759a3169d11ba71b11b642845764df400c4b2451e23
query = Query(
    include_inferred=False,
    budget=Budget(items=500, bytes=8_000_000),
    subjects=frozenset({Subject("asset")}),
    site=SiteScope("site_registry:dock-3", frozenset({"zone_map:bay-a", "zone_map:bay-b"})),
    graph=GraphClause(frozenset({"located_at", "zone_of"}), hops=2, direction=Direction.IN),
)
```

**Q08 — Simulator setup: replay a quadruped's inspection run with what held during it, on its own clock.**

```python
# query_id: query:sha256:32ada8c09fabbe82c77b1c59fc983e673ba23ccdedcf9f2053512edf3a73df15
quad_clock = DomainClock("rec:sha256:9eed593e7f9a98935f9c278870ce8bdc580c3533683178aca01741502c181bef")
utc_ns = CivilTime("utc", "unix", Fraction(1, 1_000_000_000))
query = Query(
    include_inferred=False,
    budget=Budget(items=300),
    subjects=frozenset({Subject("run", "run_log:quad-12-2026-09-14-0812")}),
    during=During(quad_clock, 0, 2_700_000_000_000),
    clock_bridges=frozenset(
        {
            ClockBridge(
                "rec:sha256:ad648d2828d14d74915b586fc818fd3f66c67044fc88f5e927eb10e40df9cfd8",
                source=quad_clock,
                target=utc_ns,
            )
        }
    ),
    graph=GraphClause(
        frozenset({"recorded_by", "has_configuration", "runs_model", "has_calibration"}),
        hops=2,
        direction=Direction.OUT,
    ),
)
```

**Q09 — Auditor: why does memory hold these two claims, as known on the audit date?**

```python
# query_id: query:sha256:c0bd196a709831fecf725849fa172baea27bc20f8050cbf7ad222c358b0037cb
query = Query(
    include_inferred=True,
    budget=Budget(items=20),
    as_of=2051,
    explain=(
        Why("claim:sha256:f518ff4505c681dc943a6caffbec442a9d81619f6aea7ada18707213293b82c3"),
        Why("claim:sha256:78a4f821a2d1e119d4ed770a9e0c6473e8d140db7e27d938137a9c08e8264edc"),
    ),
)
```

**Q10 — Auditor: what held about the autonomous truck at the incident (vehicle clock) versus at its last
inspection (UTC)?**

```python
# query_id: query:sha256:f17c558c0fe1d9b440938edb9f609eece3a7c28793632aa6bea6e78c3efbc125
truck_clock = DomainClock(
    "rec:sha256:70d1ca5d0c9e2ecc6bff6b79154d9fbab35c7eb36422bddec9e621c46ab94f5f"
)
utc_ns = CivilTime("utc", "unix", Fraction(1, 1_000_000_000))
truck = Subject("machine", "vin:5yj3e1ea7kf317000")
query = Query(
    include_inferred=True,
    budget=Budget(items=100),
    subjects=frozenset({truck}),
    clock_bridges=frozenset(
        {
            ClockBridge(
                "rec:sha256:5ac126d390251f21a1ca3f9245929c00e97a1e3a4d15ddf8a8e5a883a201526f",
                source=truck_clock,
                target=utc_ns,
            )
        }
    ),
    explain=(
        Diff(
            truck,
            before=Instant(utc_ns, 1_788_080_400_000_000_000),
            after=Instant(truck_clock, 912_345_678_000),
        ),
    ),
)
```

Refused on purpose (each is a test): Q10 without its bridge (`cross_clock_without_mapping`); Q06 without its
bridge (`cross_frame_without_transform`), or with its sphere in `mm` (`mixed_region_units`); Q01's graph clause
over a kind-wide fleet selector (`graph_without_anchor`); a `during` in naive local time
(`bad_clock`).

## Alternatives considered

- **A string query language first (SQL- or Cypher-like).** Readable, but a parser is a new hostile surface
  and its ambiguity is where clocks and frames go missing; the issue orders dataclasses first. Deferred to a
  rendering of the JSON (§1). Lost for now.
- **Default `during` to civil UTC, or let the planner pick a clock.** Converts timestamps implicitly, which
  the compiler forbids (no UTC at parse time) and Memory refuses. Lost.
- **Refuse any query whose evidence might sit on another clock.** Most deployments have several device
  clocks; refusing would make `during` unusable. Memory's `other_clocks` keeps such evidence explicit
  instead. Lost.
- **Default `include_inferred` by caller inside the query.** Hides a choice that changes what a policy acts
  on; the query records it explicitly and a helper gives the convention. Lost.
- **Hash the dataclass `repr` or pickled bytes for the id.** Not stable across Python versions or field
  order. Canonical JSON is already the compiler's identity encoding. Lost.
- **Publish the registry contract now with the query only.** The registry contract is the query *and*
  packet; publishing half would force an immediate bump. The schema is exported and tested here now. Lost.

## Consequences

- The packet (MVL-109) keys on `query_id` and embeds the query's canonical JSON; replay is query + resolved
  `as_of` + pinned versions.
- The planner (C2) receives only validated queries: every clock, frame and unit relation it may use is named
  in the query. It must still check named records exist and join what the query says they join.
- The subject-kind and predicate enums follow the pinned graph-schema and catalog-api versions; a pin bump
  that adds a kind or predicate changes the exported schema and needs a test update in the same PR.
- Revisit when: the NL planner needs a textual form; Memory publishes spatial or episode readers (regions
  and sites may gain richer shapes); a consumer needs disjunction between clauses (today conjunction only).
