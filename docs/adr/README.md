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
| [0022](0022-ingest-package-and-receipt.md) | The ingest package and its receipt | Accepted; §1 amended by 0023 and 0031 |
| [0023](0023-m1-gate-freeze-and-growth.md) | The M1 gate: the model freezes, grows only by addition, and fixes four gaps | Accepted; §5 amended by 0036 |
| [0024](0024-adapter-abi-types-selection-and-reference-adapter.md) | The adapter ABI's exact types, adapter selection, and the reference adapter | Accepted |
| [0025](0025-series-files-sorted-merged-and-pinned.md) | Series files: one sorted Parquet file per stream, merged from runs, pinned settings | Accepted |
| [0026](0026-local-workspace-in-place-reads-and-assembly.md) | The local workspace, reading sources in place, and assembling packages | Accepted; §1 amended by 0031 (format 2) |
| [0027](0027-probe-engine-sniffing-containers-and-selection-findings.md) | The probe engine: sniffing, bounded container inspection, and selection as findings | Accepted; §4 amended by 0033 |
| [0028](0028-ingest-job-phases-resume-quarantine-and-events.md) | The ingest job: nine phases, the workspace as the only checkpoint, quarantine by source, cancellation and events | Accepted; inspect phase and walk findings amended by 0033 |
| [0029](0029-hostile-file-handling.md) | Hostile file handling: walk findings, archive limits, source verification, scratch space | Accepted; §3–§4 wired by 0033 |
| [0030](0030-parser-sandbox-fork-per-call-confinement-and-limits.md) | The parser sandbox: a confined child process per adapter call, limits, and crash findings | Accepted; §1, §3, §4 amended by 0033 |
| [0031](0031-cache-keys-invalidation-lazy-derivatives-and-collection.md) | The cache: chunk ids as keys, named invalidation rules, lazy derivatives, a report and collection | Accepted; §2 amended by 0033 |
| [0032](0032-probe-listing-and-archive-inspection-stay-two-passes.md) | The probe's container listing and the archive inspector stay two passes | Accepted |
| [0033](0033-m2-gate-probing-scratch-short-reads-and-reuse.md) | The M2 gate: the job probes in the sandbox, calls get scratch, short reads are the source's | Accepted |
| [0034](0034-mcap-adapter-container-reading-planning-and-citations.md) | The MCAP adapter: our own container reader, planning from the summary, exact citations | Accepted |
| [0035](0035-python-sdk-one-surface-dry-runs-results-and-errors.md) | The Python SDK: one sync and async surface over the job, dry runs, results and a stable error taxonomy | Accepted |
| [0036](0036-session-grouping-v0-observed-layout-and-derived-proposals.md) | Session grouping v0: an observed layout, inferred proposals, derived tables in the package | Accepted |
| [0037](0037-configuration-snapshots-and-additive-schema-versions.md) | Configuration snapshots, the config adapter, and schema versions that add without rewriting | Accepted |
| [0038](0038-layout-preserving-pdf-and-markdown-adapters.md) | Layout-preserving PDF and Markdown adapters: pypdf, markdown-it-py, declared order and exact spans | Accepted |
| [0041](0041-standalone-image-ingestion-as-declared-with-region-citations.md) | Standalone image ingestion: containers read as declared, no pixel decoded, regions cited | Accepted |
| [0040](0040-software-identity-declared-per-file-bound-later.md) | Software identity: read as each file declares it, bound to runs later | Accepted |
| [0042](0042-tabular-adapter-csv-json-parquet-as-cited-cells.md) | The tabular adapter: CSV, JSON and Parquet as tables of cited cells | Accepted |
| [0043](0043-neptune-ingest-cli-exit-codes-ignore-rules-and-file-sources.md) | `neptune ingest`: a thin CLI over the SDK, fixed exit codes, declared ignore rules and single-file sources | Accepted |
| [0044](0044-explain-dry-runs-inspect-and-return-a-bounded-typed-explanation.md) | Explain: the dry run inspects and returns a bounded, typed explanation | Accepted |
| [0045](0045-rosbag2-adapter-metadata-sqlite3-reader-and-mcap-delegation.md) | rosbag2: one adapter for `metadata.yaml` and sqlite3 storage, MCAP left to the MCAP adapter, a SQLite reader over bytes | Accepted |
| [0046](0046-ros1-bag-adapter-connections-as-streams-planning-from-the-index.md) | The ROS 1 bag adapter: connections as streams, planning from the index, the same Run and Stream as MCAP | Accepted |
| [0047](0047-optional-manifest-stated-declarations-set-against-evidence.md) | The optional manifest: a source in the folder, stated declarations set against the evidence, generated as commented choices | Accepted |
| [0052](0052-geometry-adapter-meshes-and-scenes-as-referenced-objects.md) | The geometry adapter: meshes and scenes as referenced objects, nothing copied, nothing guessed | Accepted |
