# Neptune

**A live, cited model of a whole robot site that Claude can ask.** Neptune folds a site's exports, documents
and live robot feeds into one fact log: robots, machines, people by role and shift, tasks, rules, and what
feeds what. Every answer cites the file, row, page or feed line it rests on, and says `stale`, `unknown` or
`pending` instead of guessing.

## Run it

Needs `uv` and GNU make 3.82+. `make setup` once, then:

```
make site-demo                  # Plant 7 (assembly: conveyor, arm cell, AMR fleet); prints the MCP line
make site-demo SITE=harbor      # Harbor DC (warehouse: sorter, legged inspector, humanoids, goods-to-person)
claude mcp add neptune-site -- <the "MCP:" line it printed>
```

**Live:** `make site-live` starts a local MQTT broker, subscribes Neptune to it and replays Plant 7's feeds at
60x from 09:55 (the conveyor jams about 10 s in). The MCP server picks up each new log entry before every
call; `neptune_site_changes` lists what arrived since your last answer. Drop a file into
`site-demo/static/` while it runs and it is ingested. Ctrl-C stops all three.

**Your own folder:** lay it out as `<dir>/static/` (a `layout.yaml` naming your areas, robots, machines and
feed sources, plus your exports: CSV, XLSX, JSON, JSONL, PDF, photos) and `<dir>/feed/*.jsonl` (recorded feed
messages, one `{t, source, topic, payload}` envelope per line). Then:

```
uv run --all-packages --all-groups python -m neptune_memory.site build <dir>
uv run --all-packages --all-groups python -m neptune_memory.site draft-mapping <dir> static/cmms/work_orders.csv
```

`build` logs every file it cannot map as `not mapped` (still searchable, never a fact). `draft-mapping` drafts
the entry for one file; check it, save it into `<dir>/static/neptune.mappings.yaml`
([spec §12](docs/site-model/spec.md)) and `build` again. The Claude Code skill does the same loop and shows
you every draft before it saves. No mapper code names a site: Plant 7 and Harbor differ only in that file.

## Ask it

What Claude gets back from the `neptune-site` MCP tools (trimmed; real output of `make site-demo`):

**"What did the CV-2 jam hold up?"** (Plant 7, `neptune_site_insights` at 10:10)

```text
- machine:cv-2 finding = "starvation chain: machine:cv-2 fault:JAM_PE3 at 10:05:02 → robot:arm-c3
  idle_starved at 10:05:30 → task:T-8812 (robot:amr-01, robot:amr-02) waiting at 10:06:00 (downstream in
  the topology, within 120 s; the fault holds still)" since 10:05:02 · inferred [S7][S8][S9][S10]...
! findings are inferred by deterministic rules: correlations, not causes
[S7] feed/plc_cv2.jsonl line 3423 "JAM_PE3"
[S9] feed/arm_c3.jsonl line 3428 "no part at pick position for 28 s; waiting for infeed"
[S10] static/layout.yaml line 26 "- {subject: machine:cv-2, predicate: feeds, object: cell:c3}"
```

**"Who can reset the tripped sorter right now?"** (Harbor, `neptune_site_insights` at 10:20)

```text
- machine:sort-1 finding = "qualification gap: machine:sort-1 fault:OVERTEMP_M2 needs
  qualification:electrical-isolation (rule:hsr-7-3.4) and no on-shift holder is free from 10:12:10 to now;
  its only holder, person:reyes, is on break 10:00:00 to 10:30:00" since 10:12:10 · inferred [S11][S1][S5][S12][S13]
[S1] static/docs/HSR-7_excerpt.pdf page 1 block 2 "3.4 After a thermal-overload trip, a powered sorter drive
     is inspected and reset only by a person qualified for electrical isolation."
[S5] static/roster/roster.csv row 2 col "Certifications" "electrical-isolation|sorter-reset"
[S11] feed/sorter_plc.jsonl line 1035 "OVERTEMP_M2"
[S13] static/roster/roster.csv row 6 col "Break from" "10:00:00"
```

**"Is fleet robot gtp-03 the CMMS asset AMR-17?"** (Harbor, `neptune_site_resolve`, before anyone confirms)

```text
- robot:AMR-17 serial = "GTP-4471" · stated [S1]
- robot:AMR-17 same_as = robot:gtp-03 · inferred, pending [S1][S3]
[S1] static/cmms/assets.json json /assets/2/serial "GTP-4471"
[S3] feed/rmf_fleet.jsonl line 3 "GTP-4471"
```

Two names stay two entities until an operator says yes (`python -m neptune_memory.site confirm <dir>`;
`make site-demo` answers yes for the demo). Gold questions pin all of this in CI: Plant 7 8/8 and 3 insight
questions, Harbor 5/5 and 2 insight questions, each checked by its cited facts, never by wording.

## What's real, what's synthetic

- **Synthetic:** both sites. Plant 7 and Harbor DC are deterministic generated corpora (`generate --site`):
  invented people, regulations, work orders and two days of feed messages. No real robot or plant is
  connected. Feed payloads are JSON in the shape of each dialect (Sparkplug metrics, OPC UA node values,
  VDA 5050 state, Open-RMF fleet states, ROS 2 `DiagnosticArray`), not the binary wire formats.
  The whiteboard photo's text is a recorded model extraction; `extract-photos` re-records it if you set
  `ANTHROPIC_API_KEY` (`uv sync --extra anthropic`).
- **Real:** everything that reads them. The Neptune compiler ingests the files (CSV, XLSX, JSON, PDF, PNG)
  into cited packages; one mapping file per site turns rows, blocks and feed fields into facts; the fact log,
  views, identity proposals, insights and MCP tools are the same code for any site. `site-live` is a real
  MQTT broker (amqtt) and client (paho-mqtt): it records each message, then logs it, and a rebuild from the
  recording gives the same bytes.
- **Not there:** no MQTT auth, TLS or reconnects; no hosted service. Insights are five deterministic rules
  (starvation chains, qualification gaps, stale sources, changes before faults, regulation checks):
  correlations, never causes. A PDF's rules need a hand-written `documents:` entry; `draft-mapping` covers
  CSV and JSONL tables and feeds only. Times are the site's clock, never converted.

## Development

```bash
make setup   # uv-managed venv with dev tools
make check   # lint + mypy --strict + tests (what CI runs); PKG=neptune-memory for one package
```

Working here? Read `AGENTS.md`, then [`docs/site-model/README.md`](docs/site-model/README.md) and
[`docs/site-model/spec.md`](docs/site-model/spec.md). Neptune is a demo project. Raw sources are never
rewritten, every value carries provenance, blanks stay blank, and the same inputs give the same bytes.

## Documentation

| Doc | Contents |
|---|---|
| `docs/site-model/` | The site model: spec, mapping format, live, insights, gold questions |
| `docs/architecture.md` | The compiler: package boundaries, evidence vs. derived |
| `docs/canonical-data-model.md` | The compiler's entities, epistemic states, time/frame/unit primitives |
| `docs/adapter-contract.md` | The four-method adapter ABI |
| `docs/developer-workflow.md` | Git, issue and PR workflow |
| `docs/adr/` | Architecture Decision Records |

Demo v1 (one robot incident through Ledger, Memory v2 and Context v2) was removed in #213; it lives in the
history before that PR.

## License

[Apache License 2.0](LICENSE).
