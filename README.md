# Neptune

**A robotics-native ingestion fabric.** Neptune turns the messy evidence robotics teams already produce —
MCAP and ROS recordings, flight logs, URDF/SDF, calibration, configs, PDFs, images, geometry, site records,
task briefs — into a canonical, provenance-preserving, multimodal representation that downstream memory,
retrieval, simulation and training systems can consume without ever re-parsing the raw files.

Think of it as a **data compiler for physical AI**: raw sources in, deterministic ingest package + receipt out.

## Status

Pre-alpha. The canonical contract (M1) is being built; nothing is usable yet. Execution plan lives in Linear
(`Neptune — Robotics Ingestion Fabric`, milestones M1–M10). Decisions live in `docs/adr/`.

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
| `docs/developer-workflow.md` | Git, Linear, PR, and parallel-agent workflow |
| `docs/testing-strategy.md` | Test categories, fixtures, determinism and golden tests |
| `docs/security.md` | Threat model and per-milestone hardening |
| `docs/adr/` | Architecture Decision Records |
| `docs/reviews/` | Milestone review gates |
| `docs/audit-2026-09-30.md` | Engineering handoff audit that shaped the plan |

## License

Not yet chosen. Until a `LICENSE` file exists, all rights reserved.
