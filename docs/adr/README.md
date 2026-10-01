# Architecture Decision Records

One decision per file, `NNNN-short-title.md`, using `0000-template.md`. Decisions are superseded by a new ADR
that links back; they are never edited in place. Status values: Proposed, Accepted, Superseded by NNNN.

| ADR | Title | Status |
|---|---|---|
| [0001](0001-language-and-runtime.md) | Language and runtime | Accepted |
| [0002](0002-serialization-and-ingest-package.md) | Canonical serialization and ingest-package layout | Accepted |
| [0003](0003-identity-tiers.md) | Identity tiers and parser-upgrade survival | Accepted; golden-diff rule amended by 0017 |
| [0004](0004-epistemic-states.md) | Epistemic states and the field-scope rule | Accepted |
| [0005](0005-timestamp-domains.md) | Timestamp domains | Accepted |
| [0006](0006-provenance-and-locators.md) | Provenance and locator model | Accepted |
| [0007](0007-coordinate-frames.md) | Coordinate-frame semantics | Accepted |
| [0008](0008-adapter-abi.md) | Adapter ABI surface | Accepted; exact types in 0024 |
| [0009](0009-source-revisions-and-id-rendering.md) | Source revisions, dedup policy and id rendering | Accepted; §3–§5 amended by 0010 |
| [0010](0010-lossless-local-locations.md) | Lossless local locations: raw names, symlinks, absences | Accepted |
| [0011](0011-knowledge-json-and-types.md) | `Knowledge[T]`: JSON shape, Python types, provenance slot | Accepted; §2 clarified by 0023 |
| [0012](0012-time-types-and-clock-roles.md) | Time types: `Timestamp`, `Duration`, `TimestampDomain`, clock roles | Accepted; civil times added by 0023 |
| [0013](0013-unit-catalogue-and-si-normalisation.md) | Units: declared-unit catalogue and exact SI normalisation | Accepted |
| [0014](0014-version-primitives.md) | Version primitives: one type per kind, stored verbatim | Accepted |
| [0015](0015-frame-rotation-and-transform-types.md) | Frames, rotations and frame transforms: types and named conventions | Accepted |
| [0016](0016-evidence-refs-locator-paths-and-transform-lineage.md) | Evidence refs, locator paths and transform lineage | Accepted; amended by 0023 §3 |
| [0017](0017-canonical-records-envelope-and-schema-version.md) | Canonical records: envelope, families, schema version and findings | Accepted; §7 amended by 0023 |
| [0018](0018-runs-streams-and-series-layout.md) | Runs, streams and the series layout | Accepted |
| [0019](0019-machine-context-records.md) | Machine context: machines, hardware, software and calibration | Accepted |
| [0020](0020-world-and-record-context-records.md) | World and record context: sites, assets, geometry, media, documents and tables | Accepted |
| [0021](0021-schema-export-and-worked-examples.md) | The canonical JSON Schema and the worked examples | Accepted |
| [0022](0022-ingest-package-and-receipt.md) | The ingest package and its receipt | Accepted; §1 amended by 0023 |
| [0023](0023-m1-gate-freeze-and-growth.md) | The M1 gate: the model freezes, grows only by addition, and fixes four gaps | Accepted |
| [0024](0024-adapter-abi-types-selection-and-reference-adapter.md) | The adapter ABI's exact types, adapter selection, and the reference adapter | Accepted |
| [0027](0027-hostile-file-handling.md) | Hostile file handling: walk findings, archive limits, source verification, scratch space | Accepted |
