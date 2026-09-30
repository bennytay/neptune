# M1 gate: stress test of the canonical contract

- Date: 2026-10-01 · Issue: MVL-56 · Reviewed: ADRs 0002–0022, `neptune.model`, `neptune.store`, the four
  worked examples (`tests/fixtures/model/`) and their packages (`tests/golden/packages/`)
- Method: each MVL-56 scenario was walked on paper against the model. Where a worked example covers it, the
  scenario was also checked against the example's golden records and the bytes they cite.
- Outcome: the contract holds for every scenario. Four gaps and one example bug were found and fixed here
  (ADR 0023). The model is frozen at schema version 1, and M2 may start once this is merged and `main` is
  tagged `m1-gate`.

## Scenarios

| Scenario | How the model holds it | Verdict |
|---|---|---|
| MCAP `log_time` vs `publish_time` | Two `TimestampDomain`s, two `time/<i>` columns; clock 0 only orders rows (ADR 0018). Examples: quadruped, manipulator. | holds |
| PX4 boot time plus GPS time | `timestamp`, `timestamp_sample` and `time_utc_usec` are three domains; nothing relates them until a `ClockAlignment` (MVL-36). Example: drone. | holds |
| Same topic recorded in ROS 1 and ROS 2 | Two runs and two streams: different evidence, schema encodings (`ros1msg`, `ros2msg`) and clocks. Equal topic names merge nothing. | holds |
| Humanoid with 40+ frames | One `FrameGraph` per URDF, a `Frame` and a `HardwareComponent` per link, a `FrameTransform` and a component per joint: about 160 small records. Live `/tf` is a stream of samples, not records. | holds |
| Manipulator with tool changes | Each tool set is its own `HardwareConfiguration`; which applied when is a binding (MVL-38). Tested in `test_machine.py`. | holds |
| Multiple robots in one folder | A run's machine is a declared id or `NotCovered`. Byte-identical files are one artifact at two locations; one URDF record names no machine, so nothing is falsely merged. Grouping by folder is derived (MVL-13). | holds |
| Software version changes mid-folder | Each declaration is its own `SoftwareConfiguration`; a version published during a run is a stream. Binding to runs is MVL-38. | holds |
| Calibration for one hardware revision | `Calibration.hardware_revision` is declared text that matches `HardwareConfiguration.revision`. Tested. | holds |
| PDF page and bbox | `[Page(p), Span]` cites text, `[Page(p), PageRegion]` cites the drawn box in the page's stored coordinates; `DocumentPage` keeps size and `/Rotate`. | holds |
| URDF with mesh references | The mesh file is a `SpatialArtifact`. The link's reference to it is URDF detail, a companion kind for MVL-24; its text stays cited through the link's component. | holds, detail deferred |
| CAD asset | `SpatialArtifact` (`cad`) with its declared unit; IFC elements become `Asset`s citing `ObjectLocator(GlobalId)`. | holds |
| Site register with blank cells | Blank is `Unknown`, a defined "N/A" is `KnownAbsent`, the text `none` stays text; every cell cites its `RowCell`. Example: mobile robot. | holds |
| No configuration at all | Runs and streams stand alone. Machine `NotCovered`, no configuration records; the receipt shows it. A missing software identity for a run is flagged by MVL-27/38. | holds |
| Corrupt MCAP chunk | An `error` finding cites the chunk's bytes and names the affected streams; the other chunks' rows are kept. The declared count and the rows then disagree, which validation reports (MVL-41). | holds |
| Mixed coordinate systems | Axes are per `Frame` and never defaulted. Geodetic values carry their CRS, units and height reference, and a local map keeps its own frame; nothing converts (MVL-37). | holds |

## Questions

- **Will this force a rewrite later?** Not by the model, once F1 was fixed: every later change is an addition,
  and older packages always load. Per-part detail, run bindings, configuration snapshots and task context are
  new kinds (O1–O3).
- **Are we losing information?** No value is lost that the bytes hold, and every record cites them. Four
  representations round a declared form, each documented: EXIF degrees-minutes-seconds become signed degrees
  (L2), an offset-stated time becomes an exact instant (ADR 0023 §2), text numbers become floats (ADR 0015
  §1), and a `KnownAbsent` cites its definition rather than its token (L1).
- **Are evidence and interpretation separated?** Yes. The separation is a type boundary in code (`inferred`
  cannot reach `model/`), a rule in the records (roles only where a format declares them, no type inference
  for CSV cells), and now a separate directory in packages (F5).
- **Are identities stable?** Content ids and declared logical ids never depend on parsers. Record ids are
  lineage-scoped by design, and the version bump in this review left the ids of all 93 golden records
  unchanged.
- **Is provenance sufficient?** Every record, field, cell and series row resolves to exact bytes: the
  examples check 137 citations against the committed files. The gaps were values from several fields (F3)
  and `INHERITED` at depth (F4).
- **Are time and frame semantics strong enough?** Yes, and civil times now have a rule (F2). A declared UTC
  offset stays in the bytes (L7).
- **Can another parser integrate cleanly?** Yes. The worked examples play nine adapters over four binary
  formats and five text files. None needed a model change, only documented choices and adapter-specific locator
  steps.

## Findings

### Fixed here (ADR 0023)

- **F1. Adding a field had no lossless migration.** ADR 0017 §7 would have left older packages unreadable
  after the first field addition. Fields are now frozen at schema version 1, and the model grows only by
  new kinds, enum members and locator steps. Readers read every version from 1 up, and records of version 0
  are refused.
- **F2. Civil date-times had no tick rule.** The mobile-robot example read its photo's EXIF time 10 hours
  off. A time with an offset is an exact POSIX instant; a zone-less time counts its own civil seconds with
  timescale `Unknown`; a date counts days. The example is fixed, and a new test checks every example's
  declared times against the bytes they cite.
- **F3. A value from several fields had no citation rule.** It now cites the smallest part holding them
  all. The quadruped run's end (start + duration) cites the metadata object instead of the duration alone.
- **F4. `INHERITED` at depth was ambiguous.** It always means the record-level provenance.
- **F5. Derived records had no place in a package.** `derived/` is reserved, apart from `records/`.
- **F6. Schema version 0 → 1.** Every golden file changed for the version alone; ids are unchanged, and the
  two content changes are F2 and F3.

### Accepted limitations (not fixed; revisit if they bite)

- **L1.** A `KnownAbsent` cites the definition of "none", not where the token sits. A table cell's place
  comes from its row; elsewhere it is the record's citation.
- **L2.** EXIF's degrees, minutes and seconds with a hemisphere are read into signed float degrees, rounded
  once. The declared form stays in the bytes.
- **L3.** The unit catalogue has no `px` or `pt`, so camera intrinsics in pixels have unit `Unknown`. Page
  and image coordinates are in their own systems by design (ADR 0016). Adding a unit is a catalogue version.
- **L4.** glTF's axes (+Y up, +Z forward, +X left) are not a named convention, so they are `Unknown` plus a
  finding until an ADR adds the member.
- **L5.** In Python, `Known(True) == Known(1)` and `Known(1.0) == Known(1)`, although their JSON differs.
  Nothing relies on that equality today.
- **L6.** Registers are JSON Lines, fine for thousands of rows. Timestamped tables are series.
- **L7.** A civil time's stated UTC offset is not kept in its domain. A companion kind can carry it if
  consumers need it.
- **L8.** A receipt lists every finding, so adapters report one finding per affected range, never one per
  sample (`adapter-contract.md`).

### Open for M2 and later (not the canonical contract)

- **O1.** Very large streams may need partitioned series files (`series/<stream>/<part>.parquet`) for chunked
  writes and resume. Decide in MVL-16 / MVL-9 with a package-format ADR.
- **O2.** Incremental ingest: a new package per scan, or a delta. A package's ledger reflects its scan
  history, so a fresh ingest and an incremental one differ. MVL-16 / MVL-45.
- **O3.** ADR 0004 §3's "wrapped coverage field on the container" is met by findings plus validation's
  counts (MVL-39 / MVL-41), not by a canonical field.
- **O4.** Receipts gain bindings with MVL-38 as a document field (ADR 0023 §1).
