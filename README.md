# Neptune

**Ask Claude what happened to your robots, and get answers cited to the exact row, page or byte of your own
files.**

Neptune compiles the evidence robotics teams already have (MCAP and ROS 2 bags, CMMS work orders, change
tickets, syslogs, incident PDFs, URDF, calibration and configs) into a cited, provenance-preserving memory.
Claude Code asks it through an MCP server. Nothing is guessed: a blank stays a blank, two names for one robot
stay two until a source says they are one, and clock times are shown as declared, never converted.

Every type of robot: arms, mobile bases, legged platforms, humanoids, drones, marine vehicles, autonomous
vehicles and fleets. Nothing assumes one morphology.

```text
$ make up SRC=~/site-S-007            # 15 files: work orders, change tickets, runs, an incident PDF, URDFs
up: green in 4 s; memory holds 199 claims (30 clock, 23 event, 7 machine, 4 run, 8 stream)

> what was work order WO-26-0414?    (what Claude gets back from the neptune MCP server, trimmed)

event named "WO-26-0414" (valid at 2026-04-14 19:30, local wall time, zone not stated):
1. Stated: involves machine "cmms.asset:AMR-07". [I2][E1][E2][E3]
2. Stated: stated_cause "Localisation drift after map update". [I3][E2][E5][E3]
3. Stated: at_site site "cmms.site:S-007". [I4][E6][E2][E3]
Sources: F1 "cmms/work_orders.csv" ... [E1] F1 row 7, column 3 "Asset ID" ... [E5] F1 row 7, column 6 "Problem"
```

**Try it:** the [15-minute quickstart](#15-minute-quickstart) runs the demo plant in one paste.
**Your data:** [`make up SRC=<folder>`](#run-it-on-your-own-folder).

> **What's real, what's not yet (v0.1)**
>
> - **Real:** ingest of MCAP, ROS 2 bags, CSV/XLSX, PDF, URDF, YAML configs and more into a deterministic,
>   cited package; mapping of common CSV exports (CMMS, ServiceNow, Jira, Linear, syslog, registers) by their
>   columns; the memory graph; Claude Code answers with citations. Linux and macOS, tested in CI.
> - **Not yet:** a CSV whose columns match no shipped preset is ingested but not turned into events; a robot
>   named differently in two systems stays two ids until you declare aliases in a `neptune.yaml`; no time
>   zones unless you state them; on macOS adapters run without the sandbox (it needs Linux); no hosted
>   service; pre-alpha APIs.
> - **Want it on your fleet's data?** We are onboarding design partners: open an issue.

## Run it on your own folder

Point it at a folder of your files (MCAP or ROS 2 bags, CSV exports, PDFs, URDF, configs). The folder is
only read; nothing to write first:

```
make up SRC=~/my-robot-logs
cd up/my-robot-logs/claude && claude
```

**On macOS** (make 3.81, and the adapter sandbox needs Linux), run it with `uv` and say the adapters may
run unconfined over a folder you trust:
`uv run --all-packages --all-groups python -m harness.up ~/my-robot-logs --isolation in_process`.
The quickstart's demo does this by itself on macOS, and says so.

It ingests the folder, maps every CSV a shipped Deploy preset fits by its columns (CMMS work orders and
downtime logs, ServiceNow changes, Jira and Linear tickets, syslogs, asset, risk and zone registers,
requalification tests) and incident-report PDFs, builds the memory graph and writes a Claude Code folder
over it. `up/my-robot-logs/up.md` says what each stage did and what memory now holds. Over the demo's S-007
site folder (15 files, no setup) that is 4 runs, 7 machine ids, 23 events (work orders, changes,
requalification, the incident) in about 5 s.

- **No `neptune.yaml`:** each CSV's first row is read as its header (`--csv-header none` says not). A
  machine named in two systems stays two ids until you state they are one: `neptune init-manifest SRC`
  drafts a commented manifest; fill in its `machines:` aliases to say so.
- **A CSV no preset fits** is ingested and searchable, but not mapped to events. Copy a preset from
  `packages/neptune-deploy/src/neptune_deploy/lifecycle/presets/`, rename its columns, and pass a
  declaration with `make up SRC=... ARGS="--deploy my-deploy.json"` (`harness/acceptance/deploy.json` is one).

## Status

**Demo v1 runs end to end.** One command takes a messy two-site hand-over (an arm cell, an AMR fleet, a legged
inspector; corpus `acceptance 2.3.0`) through every layer and asks its nine gold questions through the `neptune`
MCP tools. Every answer is cited, and CI checks the citations by claim id, never by wording.

- **Real:** compiler ingest · Deploy's lifecycle and event-log mapping · the Ledger's catalog (embedded
  PostgreSQL) · Memory's consolidation (byte-identical to its committed snapshot) · Context's local engine
  and MCP server · Deploy's evidence-pack PDFs. CI runs all of it.
- **Answered today (22 of 46 gold claims supported)**, asked exactly as the Claude Code skill asks
  (`neptune_entities`, a one-hop read of the incident, `neptune_compare_runs` of the last good run and the
  incident run). *What happened:* the fingers struck PF-3, collision detection stopped the arm, the E-stop at
  OP-2. *What changed since the last good run:* WO-26-0911 replaced the finger set (FS-0340) and set TCP z
  145.5 -> 151.5 mm, removed and refitted the wrist camera bracket and deferred the hand-eye recalibration;
  WO-26-0912 re-ran it with a substitute board; the calibration went from CAL-ARM3A-0818 to CAL-ARM3A-0911
  (reprojection error 1.86 px, camera z 0.0745 -> 0.0702), which passed because SOP-CELL-021 revision C
  (2026-09-11) raised the acceptance limit from 0.8 to 2.0 px and dropped the 10-pick verification, naming no
  change record and no approver. Also: no wrist camera images exist, the cell PC's
  log runs about a minute and a half late, LEG-01 went to firmware 3.2.0, and INC-0007's stated cause (the
  rack face 40 mm out). *Did the maintenance follow the vendor's procedure:* service bulletin SB-2026-117
  applies to the finger set WO-26-0911 installed (FS-0340, one part by a reviewer's stated assertion) and asks
  for torque 2.5 N.m, TCP z +6.0 mm, a hand-eye recalibration before production after a bracket removal and a
  change record; the work order raised TCP z by 6.0 mm but deferred the recalibration to the next day.
  *When did ARM-3A stop:* the controller syslog's protective stop at 14:32:38 and the CMMS downtime log's
  DT-26-0914-01 at 14:33:10, both local wall time in America/New_York as Deploy's declaration states, shown on
  their own clocks and never converted; a reviewer's stated assertion makes them one stop.
- **Not answered yet:** 11 gold claims are cited only through statements about something else (`co_cited`),
  among them "the calibration caused it" (Neptune states differences, never causes) and the hand-entered CMMS
  time as the 32 s outlier (both times are shown; the difference is the agent's arithmetic). 13 are gaps: that
  no change record sets the new TCP, the bags' statuses (events
  that cite the bag by byte range, not by row), survey text, the stale config export's values, the
  envelope register. Each is pinned with its reason in `harness/acceptance/answers.json`.
  No spatial baseline (MVL-135), no LeRobot export, no hosted service; pre-alpha APIs.

## 15-minute quickstart

On a laptop with `git` and `curl`, paste this into a terminal (CI runs the same lines as
`scripts/quickstart.sh` on a clean machine, with a 15-minute limit). With GNU make 3.82 or later, `make setup`
and `make demo` are the same two `uv` commands.

```bash
command -v uv >/dev/null || { curl -LsSf https://astral.sh/uv/install.sh | sh && export PATH="$HOME/.local/bin:$PATH"; }
[ -f harness/acceptance/gold.json ] || { [ -d neptune/.git ] || git clone https://github.com/bennytay/neptune.git; cd neptune; }
uv sync --all-packages --all-groups
uv run --all-packages --all-groups python -m harness.demo --out demo
ls demo/*.pdf
cp packages/neptune-context/claude/mcp.sample.json .mcp.json
mkdir -p .claude/skills/neptune && cp -R packages/neptune-context/claude/skills/neptune/. .claude/skills/neptune/
export NEPTUNE_MEMORY_GRAPH=demo/graph.json NEPTUNE_SOURCES=demo/sources.json
```

1. Open `demo/incident-timeline-INC-C3-0011.pdf` (and `demo/configuration-traceability-ARM-3A.pdf`). Every line
   cites the claims it rests on; `demo/answers.md` shows the gold questions answered through the MCP tools,
   with each gold claim's class and reason.
2. Run `claude` in the same folder and approve the `neptune` server (`.mcp.json`; the skill is
   `.claude/skills/neptune`). Ask: **"Why did the arm-cell incident INC-C3-0011 happen?"** and
   **"What changed on ARM-3A since its last good run?"** Each fact comes back as
   `[I…][E…]` citations, and the `Sources` footer names each `[E…]` file and its row, page or field;
   `neptune_why` on a claim shows its evidence. In this path no Ledger catalog is attached, so
   `neptune_hydrate` (opening an `[E…]` source) answers `unavailable` with the file's corpus path;
   `make demo` hydrates through the Ledger and checks it.

Re-running the block is safe (`demo/` is rebuilt from scratch; an interrupted run leaves nothing it needs).

**Record or use it from a clean folder**: only the server (`demo/` if `make demo` ran, else the committed
snapshot), the skill and `MAX_MCP_OUTPUT_TOKENS`, none of the repository's agent instructions:

```
make demo-folder DIR=~/neptune-demo
cd ~/neptune-demo && claude
```

**Use it from Claude Desktop.**

Claude Desktop does not expand `${…}`, so give absolute paths. Add this to `claude_desktop_config.json`
(Settings, Developer, Edit Config), put the real path of this clone in both places and the output of
`command -v uv` as `command`, then restart Claude Desktop. The `--memory` and `--sources` paths are `NEPTUNE_MEMORY_GRAPH` and
`NEPTUNE_SOURCES` of `mcp.sample.json`: the graph and source index `make demo` wrote.

```json
{
  "mcpServers": {
    "neptune": {
      "command": "uv",
      "args": [
        "run", "--directory", "/ABSOLUTE/PATH/TO/neptune", "--all-packages",
        "python", "-m", "neptune_context.mcp",
        "--memory", "/ABSOLUTE/PATH/TO/neptune/demo/graph.json",
        "--sources", "/ABSOLUTE/PATH/TO/neptune/demo/sources.json"
      ]
    }
  }
}
```

## Run it on your own folder

Point it at a folder of your files (MCAP or ROS 2 bags, CSV exports, PDFs, URDF, configs). The folder is
only read; nothing to write first:

```
make up SRC=~/my-robot-logs
cd up/my-robot-logs/claude && claude
```

It ingests the folder, maps every CSV a shipped Deploy preset fits by its columns (CMMS work orders and
downtime logs, ServiceNow changes, Jira and Linear tickets, syslogs, asset, risk and zone registers,
requalification tests) and incident-report PDFs, builds the memory graph and writes a Claude Code folder
over it. `up/my-robot-logs/up.md` says what each stage did and what memory now holds. Over the demo's S-007
site folder (15 files, no setup) that is 4 runs, 7 machine ids, 23 events (work orders, changes,
requalification, the incident) in about 5 s.

- **No `neptune.yaml`:** each CSV's first row is read as its header (`--csv-header none` says not). A
  machine named in two systems stays two ids until you state they are one: `neptune init-manifest SRC`
  drafts a manifest whose `machines:` aliases do that.
- **A CSV no preset fits** is ingested and searchable, but not mapped to events. Copy a preset from
  `packages/neptune-deploy/src/neptune_deploy/lifecycle/presets/`, rename its columns, and pass a
  declaration with `make up SRC=... ARGS="--deploy my-deploy.json"` (`harness/acceptance/deploy.json` is one).

## Principles in one breath

Raw evidence is immutable · every value has provenance · missingness is explicit · no silent assumptions about
units, clocks, frames or identities · deterministic and idempotent · parser upgrades create new lineage ·
partial success over total failure · evidence stays separate from interpretation · all input is untrusted.

## Intended developer experience

```bash
neptune ingest ./run_2026_09_30/
neptune ingest s3://bucket/site-a/
```

```python
from neptune import ingest
result = ingest("./run")
```

No per-file type declarations. Ambiguity is surfaced in the receipt, never guessed; an optional manifest
overrides it.

## Development

```bash
make setup   # uv-managed venv with dev tools
make check   # lint + mypy --strict + tests (what CI runs)
```

Working here? Read `AGENTS.md` first, then `docs/architecture.md`.

## Documentation

| Doc | Contents |
|---|---|
| `docs/architecture.md` | System shape, package boundaries, four input domains, evidence vs. derived |
| `docs/ingestion-pipeline.md` | Stages from discovery to receipt; runtime vs. adapter responsibilities |
| `docs/canonical-data-model.md` | Entities, epistemic states, time/frame/unit primitives |
| `docs/provenance-and-identity.md` | Three-tier identity, provenance records, locators |
| `docs/adapter-contract.md` | The four-method adapter ABI |
| `docs/developer-workflow.md` | Git, Linear, PR and software-factory workflow |
| `docs/testing-strategy.md` | Test categories, fixtures, determinism and golden tests |
| `docs/security.md` | Threat model and per-milestone hardening |
| `docs/adr/` | Architecture Decision Records |
| `docs/reviews/` | Milestone review gates |
| `docs/audit-2026-09-30.md` | Engineering handoff audit that shaped the plan |

## License

[Apache License 2.0](LICENSE).
