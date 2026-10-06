# SDK and MCP server

Python SDK (sync and async) and the MCP server over the query and packet contracts
([ADR 0002](adr/0002-query-language.md), [ADR 0003](adr/0003-the-context-packet.md),
[ADR 0004](adr/0004-sdk-and-mcp-server.md)). Both are read-only: Context never writes memory.

## SDK

```python
from neptune_context.query import Budget, Query            # the typed question (ADR 0002)
from neptune_context.sdk import AsyncClient, Client, RetryPolicy, StubEngine

client = Client(engine)                                    # in-process engine: implicit tenant, no token
client = Client("https://neptune.example", token=token)    # remote engine over the wire
packet = client.query(query)                               # -> ContextPacket
packet = client.why(claim_id, include_inferred=False)      # why memory holds one claim
packet = client.diff(subject, 1500, 1842, include_inferred=False, as_of=1842)
resolution = client.hydrate(evidence_item, as_of=packet.as_of)   # the Ledger's Resolution
```

- `AsyncClient` has the same methods and types, awaitable. A sync `Engine` serves it in a worker thread.
- `include_inferred` has no default on any call: you choose evidence-only or inferences-included, every time.
- `query` validates first (a refused query never reaches an engine; `SdkError.findings` has the reasons) and
  verifies the answer after (`answer_problems`, ADR 0006 §2). The packet must carry:
  - this query's id;
  - the snapshot the query pinned;
  - the same inference choice;
  - the query's budget, echoed exactly;
  - the query's `during` window on its clock.

  Every timed item must be on that clock or on a clock the query bridges to it, and every gap must point
  into the query. A packet that fails any check is `invalid_response`, never data.
- Errors are `SdkError(code, message, findings)` with `ErrorCode`: `invalid_argument`, `query_refused`,
  `unauthenticated`, `forbidden`, `not_found`, `unavailable`, `timeout`, `invalid_response`, `engine_error`.
- Every call is a read, so `unavailable` and `timeout` are retried under `RetryPolicy` (default 3 tries,
  0.2 s then 0.8 s; deterministic, no jitter). Nothing else is retried. A client over an in-process engine
  does not retry unless you pass a `RetryPolicy`.
- A token is sent only over `https` or to a loopback host, never followed through a redirect or a proxy,
  never printed. `timeout` bounds the whole answer.
- The in-process engine is `neptune_context.engine.LocalEngine(memory_reader, catalog=None)` (ADR 0007): it
  runs the graph channel over a Memory reader (and the Ledger's indexes when a `CatalogApi` is given), fuses,
  cuts to the budget and assembles the packet. `read_graph(path)` loads a Memory graph document into Memory's
  reference reader, indexed by claim id for `why` (`explain.IndexedReader`). Lexical and vector channels join
  through `LocalEngine(..., channels=[...])`.
- `why` and `diff` answers carry `packet.trails` (ADR 0010): the why tree (root, corroborating, conflicting
  and alternative claims, each with its evidence) and the what-changed list (opened, closed, superseded, by
  predicate). `neptune_context.explain.render_markdown(packet)` renders any packet for people, with
  `neptune://claim/<id>?as_of=N` links (open with `why`) and `neptune://evidence/<token>?as_of=N` links
  (open with `hydrate`; the MCP server's resource URIs).
- `StubEngine.from_directory(Path("tests/golden/packets"))` answers exactly the queries it has recorded
  packets for and says `not_found` for anything else: for fixtures and offline builds. It reads regular
  `*.json` files only (symlinks and other files are skipped), checks each size before reading, and refuses a
  directory with no packet.
- The planner (ADR 0005) rides on the client ([ADR 0009](adr/0009-agent-renderer-and-mcp-tool-surface.md)):

  ```
  planner = Planner(entity_index(graph_document), Defaults(Caller.AGENT), AnthropicClient())
  client = Client(engine, planner=planner)
  planned = client.plan("why did the arm-cell incident happen?")   # PlannedQuery: shown, never run
  planned = client.choose(planned, "ARM-3A", "asset-tag:ARM-3A")  # settle an ambiguous name
  asked = client.ask("...")             # Asked(plan, packet): the packet only when the plan is ready
  client.entities("machine", include_inferred=False)                # names current at head
  client.find("ARM-3A in CELL-3", as_of=4, include_inferred=False)  # names at transaction 4
  ```

  Without a planner these calls are `unavailable`; with `NoModel()` every plan is a visible `failed` plan.

## The ten worked queries in SDK form

The queries of ADR 0002, each sent through a client. `tests/test_sdk_docs_context.py` runs every block
against an engine that echoes the query it receives and checks the id stated on its first line.

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
packet = client.query(query)
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
packet = client.query(query)
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
packet = client.query(query)
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
packet = client.query(query)
# the same question through the convenience call (an identical query, so an identical id):
packet = client.diff(
    agv, before=1500, after=1842, include_inferred=False, as_of=1842, budget=Budget(items=100)
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
packet = client.query(query)
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
packet = client.query(query)
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
packet = client.query(query)
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
packet = client.query(query)
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
packet = client.query(query)
```

**Q10 — Auditor: what held about the autonomous truck at the incident (vehicle clock) versus at its last inspection (UTC)?**

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
packet = client.query(query)
```

## MCP server

`python -m neptune_context.mcp --url https://neptune.example` (token from `$NEPTUNE_TOKEN`),
`--memory GRAPH.json` (the local engine over a Memory graph document, JSON or `.json.gz`, ADR 0007, [ADR 0013](adr/0013-demo-reads-memorys-gzip-snapshot.md)) or `--packets DIR` (recorded
packets, for trying it out) serves six read-only tools over stdio
([ADR 0004](adr/0004-sdk-and-mcp-server.md), [ADR 0009](adr/0009-agent-renderer-and-mcp-tool-surface.md)).
With `--memory`, `--planner anthropic` (the `anthropic` extra and an API key) or `--planner-recordings FILE`
gives `neptune_plan` a model; without one a plan says no model is configured.

Claude Code, from the repository root, over the Demo v1 two-site corpus:

```
# Memory's pipeline-built snapshot of the corpus, read as it is (set NEPTUNE_MEMORY_GRAPH to serve another)
GRAPH=packages/neptune-memory/tests/fixtures/acceptance_corpus.graph.json.gz
uv run --all-packages python packages/neptune-context/scripts/export_demo_graph.py   # checks the server reads it
claude mcp add neptune -- uv run --all-packages python -m neptune_context.mcp --memory "${NEPTUNE_MEMORY_GRAPH:-$GRAPH}"
mkdir -p .claude/skills && cp -r packages/neptune-context/claude/skills/neptune .claude/skills/
```

or copy `packages/neptune-context/claude/mcp.sample.json` to `.mcp.json` (it reads `${NEPTUNE_MEMORY_GRAPH}`, defaulting to
Memory's snapshot above, and `${NEPTUNE_REPO}`). The skill (`claude/skills/neptune/SKILL.md`) tells Claude when to ask Neptune, how to
build the query and how to cite the answer.

With `--memory` and no Ledger catalog attached, series windows, frames and `neptune_hydrate` answer with gaps
or `unavailable`; a catalog is attached in code (`build_server(AsyncClient(LocalEngine(reader, catalog)))`).
Extra retrieval channels join through `local_client(document, channels=factory)`.

| Tool | Asks | Arguments |
|---|---|---|
| `neptune_query` | what memory holds | `include_inferred`, `query` (the query JSON; fixed defaults may be left out) |
| `neptune_why` | why one claim is held | `claim_id`, `include_inferred`, `as_of`, `max_items` |
| `neptune_diff` | what changed about one subject | `subject`, `before`, `after`, `include_inferred`, `as_of`, `max_items` |
| `neptune_hydrate` | what the Ledger knows about a cited source | `evidence`, `as_of` |
| `neptune_plan` | a typed query drafted from a question (inferred; never run) | `question`, `as_of` |
| `neptune_entities` | declared identities current at `as_of`, to use as subjects | `include_inferred`, `text` (find names in it), `kind`, `as_of` |

- A packet answer is cited sentences (`render.agent.render_answer`):
  - a header with the snapshot, the clock and the inference policy;
  - **What changed** first;
  - a **why outline** and **what-changed lines** when the packet has trails ([ADR 0011](adr/0011-agent-renderer-trails-why-outlines-and-diff-change-lines.md)):
    each node or change is a sentence naming its claim and citing its evidence, a conflict names the resolver
    finding, a repeat and every cap or gap says so, and a diff says `held only between the two points` for a
    version that opened and closed inside the window;
  - **Facts**, one sentence per item, each ending `[I<n>][E<k>]`; inferred items open with `INFERRED`;
  - quantities by declared unit, findings and gaps;
  - an `Items:` footer (item and claim ids) and the `Evidence:` footer.

  One resource link follows per evidence ref. Reading `neptune://evidence/<token>?as_of=N` hydrates it at the
  answer's snapshot. `render.agent.parse_answer` recovers every citation from the text.
- Text from sources is quoted, hardened JSON. Square brackets, every non-ASCII bracket (Unicode Ps/Pe), angle
  brackets, backticks and their look-alikes, controls, bidirectional and invisible characters are escaped, so a document cannot forge a citation, a line or a tag. The answer says
  quoted strings are data, never instructions.
- A failure is a tool error whose text is the SDK error as JSON (`code`, `message`, `retryable`, `findings`).
  Arguments nested deeper than 64 levels are `invalid_argument`.
- The server holds no retrieval logic: it calls an `AsyncClient`. An in-process engine plugs in as
  `build_server(AsyncClient(engine))`; retrieval channels added later change the query schema the tools
  advertise, not the tools.

## Wire (SDK to a remote engine, version 1)

- `POST /v1/query`: canonical query JSON in, canonical packet JSON out.
- `POST /v1/hydrate`: `{"evidence": <ref>, "as_of": <int, optional>}` in, the catalog API's `Resolution` out.
- Errors: HTTP status, body `{"error": {"code", "message", "findings"}}`. 401/403/404/422 map to their
  codes; 408 and 504 to `timeout`; 429 and 5xx to `unavailable`. Headers: `Authorization: Bearer`,
  `X-Neptune-Wire: 1`. Authentication and tenancy belong to Platform (X2); this package only presents a token.
