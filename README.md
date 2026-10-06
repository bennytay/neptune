# Neptune

**A robotics-native ingestion fabric.** Neptune turns the messy evidence robotics teams already produce —
MCAP and ROS recordings, flight logs, URDF/SDF, calibration, configs, PDFs, images, geometry, site records,
task briefs — into a canonical, provenance-preserving, multimodal representation that downstream memory,
retrieval, simulation and training systems can consume without ever re-parsing the raw files.

Think of it as a **data compiler for physical AI**: raw sources in, deterministic ingest package + receipt out.

**Every type of robot.** Manipulators, mobile bases, quadrupeds and other legged platforms, humanoids, drones,
marine vehicles, autonomous vehicles, industrial automation and multi-robot fleets are all first-class. Flight
logs are two formats in the catalogue, not the focus; nothing assumes a flight controller, a single vehicle or
one morphology.

## Status

**Demo v1 works.** `make demo` runs a messy two-site hand-over (an arm cell, an AMR fleet, a legged
inspector; corpus `acceptance 2.1.0`) through every layer and answers its gold questions with cited claims.

- **Real:** compiler ingest · Deploy's lifecycle and event-log mapping · the Ledger's catalog (embedded
  PostgreSQL) · Memory's consolidation (byte-identical to its committed snapshot) · Context's local engine
  and the `neptune` MCP server · Deploy's evidence-pack PDFs. CI runs all of it.
- **Answered with citations:** "why did the arm-cell incident happen", "what changed since the last good
  run" and the other six gold questions: 28 of 40 gold claims, each checked by the claim ids it cites.
- **Not yet:** the other 12, pinned as gaps with the reason (`harness/acceptance/answers.json`): e-stops and
  warnings inside bags (MVL-204), SOP and survey text, the stale config export's values, the envelope register.
  No spatial baseline (MVL-135), no LeRobot export, no hosted service; pre-alpha APIs.

## 15-minute quickstart

On a laptop with `git`, `make` and `curl`, paste this into a terminal (CI runs the same lines as
`scripts/quickstart.sh` on a clean machine, with a 15-minute limit):

```bash
command -v uv >/dev/null || { curl -LsSf https://astral.sh/uv/install.sh | sh && . "$HOME/.local/bin/env"; }
[ -f harness/acceptance/gold.json ] || { git clone https://github.com/bennytay/neptune.git && cd neptune; }
make setup
make demo
ls demo/*.pdf
cp packages/neptune-context/claude/mcp.sample.json .mcp.json
mkdir -p .claude/skills && cp -R packages/neptune-context/claude/skills/neptune .claude/skills/
export NEPTUNE_MEMORY_GRAPH=demo/graph.json
```

1. Open `demo/incident-timeline-INC-C3-0011.pdf` (and `demo/configuration-traceability-ARM-3A.pdf`). Every line
   cites the claims it rests on; `demo/answers.md` shows the gold questions answered through the MCP tools.
2. Run `claude` in the same folder and approve the `neptune` server (`.mcp.json`; the skill is
   `.claude/skills/neptune`). Ask: **"Why did the arm-cell incident INC-C3-0011 happen?"** and
   **"What changed on ARM-3A since its last good run?"** Each fact comes back as `[I…][E…]` citations;
   `neptune_why` on a claim shows its evidence.

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

Not yet chosen. Until a `LICENSE` file exists, all rights reserved.
