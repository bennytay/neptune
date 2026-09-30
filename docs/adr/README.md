# Architecture Decision Records

One decision per file, `NNNN-short-title.md`, using `0000-template.md`. Decisions are superseded by a new ADR
that links back; they are never edited in place. Status values: Proposed, Accepted, Superseded by NNNN.

| ADR | Title | Status |
|---|---|---|
| [0001](0001-language-and-runtime.md) | Language and runtime | Accepted |
| [0002](0002-serialization-and-ingest-package.md) | Canonical serialization and ingest-package layout | Accepted |
| [0003](0003-identity-tiers.md) | Identity tiers and parser-upgrade survival | Accepted |
| [0004](0004-epistemic-states.md) | Epistemic states and the field-scope rule | Accepted |
| [0005](0005-timestamp-domains.md) | Timestamp domains | Accepted |
| [0006](0006-provenance-and-locators.md) | Provenance and locator model | Accepted |
| [0007](0007-coordinate-frames.md) | Coordinate-frame semantics | Accepted |
| [0008](0008-adapter-abi.md) | Adapter ABI surface | Accepted |
| [0009](0009-source-revisions-and-id-rendering.md) | Source revisions, dedup policy and id rendering | Accepted; §3–§5 amended by 0010 |
| [0010](0010-lossless-local-locations.md) | Lossless local locations: raw names, symlinks, absences | Accepted |
| [0011](0011-knowledge-json-and-types.md) | `Knowledge[T]`: JSON shape, Python types, provenance slot | Accepted |
| [0012](0012-time-types-and-clock-roles.md) | Time types: `Timestamp`, `Duration`, `TimestampDomain`, clock roles | Accepted |
| [0013](0013-unit-catalogue-and-si-normalisation.md) | Units: declared-unit catalogue and exact SI normalisation | Accepted |
