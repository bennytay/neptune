# Canonical data model

Status: **authoritative**; frozen at `SCHEMA_VERSION` 1 by the M1 gate (MVL-56, ADR 0023; review:
`docs/reviews/m1-stress-test.md`) and grown only by addition since: version 2 adds configuration snapshots
(MVL-23, ADR 0037), version 3 alignment records (MVL-82, ADR 0050), version 4 deployment lifecycle records
(MVL-83, ADR 0051). Primitives are specified by
MVL-2 / MVL-40 / MVL-4 / MVL-3, the record envelope by MVL-66 (ADR 0017), runs, streams and series by MVL-67
(ADR 0018), machine context by MVL-68 (ADR 0019) and world context by MVL-69 (ADR 0020). The JSON Schema
(`docs/schema/canonical.schema.json`) and the worked examples are MVL-70's (ADR 0021). Any change here needs
an ADR and a schema-version bump, and must be an addition (ADR 0023 §1).

## Record kinds (schema version 4)

Every record kind belongs to one family (ADR 0017 §4). The last four families are the design contract's source
domains.

| Family | Record kinds | Where |
|---|---|---|
| `source` | `SourceArtifact`, `SourceRevision`, `SourceAbsence` | `model/source.py` (ADRs 0009, 0010) |
| `lineage` | `TransformRecord` | `model/provenance.py` (ADR 0016) |
| `finding` | `IngestFinding` | `model/finding.py` |
| `reference` | `TimestampDomain`, `FrameGraph`, `Frame`, `FrameTransform` | `model/reference.py` |
| `run` | `Run`, `Stream` | `model/run.py`, series contract in `model/series.py` (ADR 0018) |
| `machine` | `Machine`, `HardwareConfiguration`, `HardwareComponent`, `SoftwareConfiguration`, `Calibration`; since version 2 `ConfigurationSnapshot`, `ConfigurationValue` | `model/machine.py` (ADR 0019), `model/configuration.py` (ADR 0037) |
| `world` | `Site`, `Asset`, `SpatialArtifact`, `Image`, `Video`, `DocumentRecord`, `DocumentBlock`, `StructuredTable`, `StructuredRecord`; since version 4 `CommissioningBaseline`, `AuthorisationEnvelope`, `Intervention`, `MaintenanceEvent`, `RequalificationRecord`, `IncidentRecord`, `ChangeRecord`, `RiskAssessment` | `model/world.py` (ADR 0020), `model/lifecycle.py` (ADR 0051) |
| `task` | `TaskBrief`, `SOPSection`, `Requirement`, `WorkOrder` | reserved for MVL-33 |
| `alignment` | `IdentityLink`, `ClockMapping`, `FrameBinding`, `RunAssembly`, `SnapshotBinding` (since 3) | `model/alignment.py` (ADR 0050) |

`IngestReceipt` is the package-level account of an ingest run, not a record table: one document per package,
beside `PackageManifest` and the volatile `ReceiptEnvelope` (`model/package.py`, ADR 0022).

Naming decisions: `IngestFinding` (not `IntegrityFinding`). No `ProvenanceEdge` entity in v0 — provenance is
embedded on each record. `EpisodeCandidate` / `Observation` are **reserved names, not modelled**: they are the
boundary to the memory learner.

## Records and the envelope (ADR 0017; `model/record.py`)

- Every record's JSON carries `kind` (its table) and `schema_version`: the lowest version whose readers read it,
  which is the version that added its kind (ADR 0037 §1). Readers check the version first and refuse a newer
  one, so a newer record fails with a version error, never a key error.
- **Evidence records** hold `id` and one record-level `Provenance`. The id is
  `evidence_record_id(kind, provenance.evidence, transform)`; `check_evidence_record_id` verifies it.
  Several records of one kind from one piece of evidence need finer locators, never counters.
- **Ledger records** (`source_*`, `transform_record`) and **findings** derive their ids from their content and
  carry no record-level provenance.
- A canonical record always has one record-level `EvidenceRef`: the evidence that declares it. A record that
  exists only because a procedure combined several sources is inferred and lives in `derived/`.
- A value defined by a format specification (MCAP `log_time` is ns) cites the bytes that establish the format
  plus the transform that applies the spec. When the source carries the definition itself (a ROS message
  definition in an MCAP schema record), it cites that instead.
- `SCHEMA_VERSION` is 4 (2: configuration, ADR 0037; 3: alignment, ADR 0050; 4: deployment lifecycle, ADR 0051).
  It became 1 at the M1 gate (ADR 0023), and a record kind's fields never change from then on. The model grows
  only by addition (new record kinds, including companion kinds naming the record they
  extend, new enum members, new locator steps), each through an ADR and a version bump. So every record from
  version 1 on stays valid, readers read versions 1 to their own unchanged, and ids never move. Version 0
  drafts are refused. Stored packages are never rewritten. Anything that is not an addition is a new kind and
  a new adapter version.
- An addition rewrites nothing that does not use it (ADR 0037 §1): a kind added later declares `since`, its
  records are written at that version and all others at theirs, and a package is written at the lowest version
  that holds its records (`kinds.package_version`), with a table for each kind of that version (`kinds_at`).
- Records are frozen standard-library dataclasses with strict hand-written JSON; there is no modelling library.
  The JSON Schema is generated from them (MVL-70).

## Findings (ADR 0017 §9; `model/finding.py`, `identity/findings.py`)

- `IngestFinding(code, category, severity, subject, transform, message, details, related, records)`.
- `code` is `<producer>.<name>`. `category` is one of `corrupt`, `unsupported`, `unrepresentable`,
  `missing`, `ambiguous`, `inconsistent`, `skipped`, `limit` or `failed`.
- `severity` is judged by what reached the output: `error` means evidence was lost, `warning` means a value is in
  doubt, and `info` means nothing was lost.
- `subject` is an `EvidenceRef`, or a location when there are no bytes to cite (an unreadable directory).
- Build findings with `identity.findings.ingest_finding`. The id covers the whole content, so identical findings
  are one finding. Messages and details are deterministic: no wall-clock, hosts, absolute paths or addresses.

## Epistemic states — `Knowledge[T]` (ADR 0004, ADR 0011; `model/knowledge.py`)

```
Known(value)              the source asserts this value
KnownAbsent               the source asserts there is no value
Unknown                   the source says nothing
NotCovered                the source could not have said anything (never measured / out of scope)
NotApplicable             the field does not apply to this entity
Ambiguous(candidates)     the source supports more than one reading
```

Scope rule: fields with epistemic weight — units, clocks, frames, versions, calibration, identities, coverage —
use the wrapper. Purely structural fields (the list of streams found) do not. A `None` in a canonical record is
a bug, not a value.

- Provenance: states hold `INHERITED` (the record's provenance, omitted from JSON) or their own. `INHERITED`
  means the record-level provenance however deeply the state is nested (ADR 0023 §4). `KnownAbsent` always
  cites what defines the absence.
- A value read from several fields cites the smallest part holding them all (ADR 0023 §3).
- JSON: `{"knowledge": "<state>", ...}`, e.g. `{"knowledge":"known","value":30}`. Full shape: ADR 0011.
- No confidence scores on evidence; uncertainty is `Ambiguous` / `Unknown` / `NotCovered` (ADR 0004 §6).

## Provenance (ADR 0006, ADR 0016; `model/provenance.py`)

- `Provenance(evidence: EvidenceRef, transform: TransformRecord id, assertion_kind: observed | stated)` fills
  every `Knowledge` provenance slot. `inferred` exists only in `derived/` and is rejected on canonical states.
- `EvidenceRef(source content id | external object, locator path)`: steps outermost first, each inside what the
  transform decoded from the previous one. Details and the step table: `provenance-and-identity.md`.
- `TransformRecord.upstream` hash-links a normaliser to the transform it consumed, so a normalised value cites
  the same evidence as its input and its chain is `adapter → normaliser`.

## Time (ADR 0005, ADR 0012; `model/time.py`)

- `Timestamp = (ticks: int, domain_id)`: signed 64-bit, never floats, never bare. `Duration` carries its
  domain too. Ordering and subtraction across domains raise; `==` is record equality, not simultaneity.
- `TimestampDomain` is a record (`model/reference.py`) that carries provenance. It has a structural `field` and
  `scope` (where the ticks are read, verbatim) and `Knowledge`-wrapped `role` (receive / publish / sample /
  document), `resolution` (exact `Fraction` seconds per tick), `epoch`, `timescale` and `declared_monotonic`.
- MCAP `log_time` and `publish_time`, ROS `header.stamp` and receive time, PX4 boot-time and GPS time are
  separate domains. Mappings between domains are `ClockMapping` records (ADR 0050, below): stated ones are
  canonical, estimated ones derived (ADR 0060: an exact fit over sync anchors, valid only between its first
  and last anchor, its `residual_bound` `Unknown` unless the latency between paired readings is stated).
  `neptune.derived.clocks.ClockGraph.align` gives an instant on another clock with its bound, or says why
  not (`unsynchronised`, `outside_validity`, `rate_unknown`); no stored tick changes.
- Civil date-times (ADR 0023 §2): with a stated offset, ticks are POSIX seconds of the exact instant (epoch
  `unix`, timescale `posix`); with no zone, POSIX-style seconds on the source's own civil clock (epoch `unix`,
  timescale `Unknown`); a date alone counts days.

## Units (ADR 0013; `model/units.py`)

- `Knowledge[Unit]`, stored as declared (`mm` stays `mm`) or `Unknown`; nothing defaults to SI.
- `Unit` = canonical product of catalogued, optionally prefixed atoms: JSON `"km.h^-1"`.
  `Dimension` adds plane and solid angle to the SI bases, so rad/s ≠ Hz.
- Declared text goes through `unit_from_text` only: one reading ⇒ `Known`, several (`g`, `C`) ⇒ `Ambiguous`,
  unreadable ⇒ finding + `Unknown`.
- `to_si` is exact (`rational × π^k`) and used by derived transforms only; the SI value is a separate
  record. `CATALOGUE_VERSION` is an output-affecting library version.

## Frames and spatial references (ADR 0007, ADR 0015; `model/frames.py`, `model/spatial.py`, `model/reference.py`)

- `FrameRef = (frame_id verbatim, frame_graph_id)`. Graphs are source-scoped `FrameGraph(id, provenance, scope)`
  records. The `Frame` record holds `Knowledge`-wrapped `axes` (named conventions
  `enu ned nwu flu frd rdf rub ruf fru`) and `handedness`. There is no default; REP-103 is not evidence.
- Rotations (`Quaternion`, `RotationMatrix`, `EulerAngles`, `RotationVector`), `Translation`, `Pose` and
  `HomogeneousMatrix` keep their float components in source order. Order, layout, Euler sequence/mode,
  quaternion algebra and units are separate `Knowledge` fields, so "undeclared" keeps the numbers.
- `FrameTransform(id, provenance, parent, child, direction, value, validity)`: one graph, `direction` `Knowledge`-wrapped
  (`Ambiguous` when a calibration file does not say), validity `STATIC` or a `Timestamp`. Not to be confused
  with `TransformRecord`, the provenance record. Nothing composes transforms.
- Across graphs (ADR 0068, derived): a run's tf and header frame ids form one `frame_tree`, each tf pair a
  `frame_edge` (static or dynamic, its samples' first and last instants, direction `child_to_parent`, unit and
  quaternion algebra `Unknown`); names a declared graph and a run share, or that differ by a leading `/`, are
  `frame_link` proposals; `frame_group`s are frames transforms, edges and stated bindings join (never links),
  each with its `origin` and an `Unknown` `earth`; `spatial_reference` says which frames, CRS or geodetic type a
  stream's or record's values are in. `neptune.derived.frames.FrameIndex.compare` says whether two references
  are comparable (a path, whether inferred, its caveats, coverage at an instant) or why not (`disconnected`,
  `crs_differs`, `no_georeference`, …). Nothing is composed or reprojected.
- `GeodeticPosition`: latitude/longitude as declared, `Knowledge`-wrapped height, `CrsCode`, units and
  `HeightReference` (`ellipsoid`, `mean_sea_level`, `home`, `ground`).

## Versions and software identity (ADR 0014; `model/versions.py`)

- One type per kind: `GitCommit`, `SemanticVersion`, `DeclaredVersion`, `BuildId`, `FirmwareVersion`,
  `ModelCheckpointHash`, `ContainerImageDigest`. Each is `Knowledge`-wrapped in its own field of a
  `SoftwareConfiguration` item (ADR 0019 §5).
- Kinds never compare equal and never sort together. Only `SemanticVersion` is ordered (SemVer precedence).
- Stored verbatim: no `v` stripping, no case folding. Semver prerelease and build are views of the full text.
- A value takes a kind because the source says so, never because it looks like one. Otherwise it is a
  `DeclaredVersion`. JSON: `{"kind":"semver","value":"1.2.3-rc.1"}`.

## Runs, streams and series (ADR 0018; `model/run.py`, `model/series.py`)

- `Run`: a session one piece of evidence declares (a recording, a rosbag2 `metadata.yaml`, a manifest entry).
  `logical_id` and `machine` are declared ids; `first` / `last` are inclusive and separate, because a source
  may state only one. Heuristic groupings are derived: `session_proposal` records in the package's `derived/`
  tables (MVL-13, ADR 0036; assembled over evidence by MVL-34, ADR 0066), never `Run`s. The files one
  `Run` is made of, where a source states them (rosbag2's `relative_file_paths`), are a canonical
  `run_assembly` (ADR 0050 §7, ADR 0066 §1); each file's own `Run` stays as declared.
- `Stream`: one channel as declared. It holds `run`, `topic`, `schema_name` / `schema_encoding` /
  `schema_definition`, `message_encoding`, `metadata`, `clocks`, and the source's declared `message_count` /
  `first` / `last`, plus `series`. A topic split across files is several streams of one run.
- Every clock a sample carries is its own domain and its own `time/<i>` column. Clock 0, the one the source
  orders or indexes by, only orders the stored rows; it is not the stream's time. A decoded ROS payload whose
  type leads with a `std_msgs/Header` adds its stamp as one more clock, `header.stamp` (ADR 0068).
- Series: one Parquet file per stream, one row per sample.

  | Column | Type | Holds |
  |---|---|---|
  | `seq` | int64 | the sample's position in source order |
  | `time/<i>` | int64 | ticks on `clocks[i]`; never Parquet's TIMESTAMP type |
  | `locator/<i>/<field>` | int64, UTF-8 or double | the fields of locator step `i` that vary per row |
  | `value/<name>` | as decoded | decoded fields, named by the adapter; a ROS payload's by field path (`header.frame_id`, `transforms[].child_frame_id`, a list column under one array level), each with its state column (ADR 0068) |
  | `state/<column>` | dictionary UTF-8 | `known` / `unknown` / `not_covered` / `not_applicable`; the column is null exactly where not `known` |

- Row provenance: the `Stream` hoists the source, the assertion kind and a locator template, and the
  stream's transform applies. The row's `locator/` columns fill the template. `Stream.row_provenance(row)`
  rebuilds it, and the file's metadata holds the `Stream` line under `neptune.stream`.
- Rows are sorted by their clock-0 ticks (unknown last), then `seq`.
- The store writes each stream's file from per-chunk sorted runs with a bounded-memory merge, in
  65,536-row groups with pinned settings, so the bytes depend only on the rows (ADR 0025). A
  records-only package may hold a stream without its file; an ingest writes one for every stream.
- `SeriesBatch` carries rows from an adapter to the store column by column (ADR 0024 §5). Each
  `SeriesColumn` has a `ColumnType` as the source encodes it (bool, int8–64, uint8–64, float32, float64,
  string, binary), `repeated` for arrays; Neptune's own columns keep the types above.

## Machine context (ADR 0019; `model/machine.py`)

- `Machine`: a robot or vehicle one declaration names by at least one id: a manifest's robot entry, a
  fleet register's row, a log's vehicle information. `identifiers` lists every id it gives, each cited;
  `manufacturer` and `model` are declared text. A URDF names a model, never a machine.
- Declared identifiers (shared with `Site` / `Asset`): `tuple[Knowledge[LogicalId], ...]`, each `Known`
  or `Ambiguous`, no repeats, sorted by namespace then value. Ids in one declaration are what MVL-35
  links by; equal ids in two declarations are two records.
- `HardwareConfiguration`: what one declaration says a machine is made of: `machine`, `name`,
  `revision` (declared text). A snapshot, never edited: a tool change is a new record.
- `HardwareComponent`: one declared part, naming its `configuration`. `category` is `link`, `joint`,
  `sensor`, `actuator`, `computer`, `power`, `payload` or `tool`; `name`, `model`, `identifiers` and
  `frame` (a `FrameRef` in a declared graph) are as declared. Kinematics are the source graph's
  `FrameTransform`s. Detail only one category has (joint limits, inertia) arrives as new record kinds.
- `SoftwareConfiguration`: the software one declaration says ran: `machine` plus items. Each item has
  `name`, `device` (firmware per device) and one field per identity: `commit`, `release`, `build`,
  `digest`. A missing identity is `Unknown` or `NotCovered` plus a finding, never blank.
- `Calibration`: what one declaration states about one `subject`, for which `machine` and
  `hardware_revision`, and when (`performed`, `valid_from`, `valid_until`). `parameters` are declared
  names with their numbers in source order (or a setting's text) and units; `extrinsics` lists the
  `FrameTransform` records it declares.
- Which configuration or calibration applied to which run is a binding (MVL-38); nothing here points
  at a run.

## Configuration snapshots (ADR 0037; `model/configuration.py`)

- `ConfigurationSnapshot` (since version 2): one configuration document as its bytes declare it, a JSON or
  TOML file or one document of a YAML stream: `format`, `format_version` (a `%YAML` directive; `Unknown`
  without one, `NotCovered` in JSON and TOML), `encoding`, `byte_order_mark`, `line_endings`, `comments`
  (verbatim, each citing its span, never attached to a value), `values` (how many value records it has) and
  `digest`.
- `ConfigurationValue` (since version 2): one node, naming its `snapshot`: `path` (keys verbatim, positions as
  integers), `occurrence` (per step, which of the entries sharing that key it passes through; `(path,
  occurrence)` is unique in a snapshot), `order` among its parent's entries, `key_tag` (a YAML key's type
  where it is not a string, else `NotApplicable`), YAML `tag` (`NotCovered` in JSON and TOML), `text` (a
  scalar as written) and `value`: a `ConfigCollection`, a `ConfigAlias` (a reference, never expanded, to a
  node, or with `key` to an anchored key's entry) or a `ConfigScalar` in the format's own type, citing its span. A format-defined null is `KnownAbsent` citing
  the document; YAML 1.1 and 1.2 readings that differ in an undeclared document are `Ambiguous`.
- A value's locator is a `JsonPointer` into the document as parsed, after a `config:document` step in YAML;
  each entry of a repeated key is addressed by position (`config:entry`) instead.
- Identity: the bytes by their content id, the values by `digest`, the sha256 of every value's path, occurrence
  and `comparison_key`, in that order. `compare_configurations(left, right)` lists the paths whose declared values differ;
  equal digests exactly when it lists none. Spelling, quoting, comments, key order and format never count.
- Nothing is inferred: a key named `wheel_radius` is a declared number, with no unit unless the document
  states one in a value of its own.

## Alignment (ADR 0050; `model/alignment.py`)

- Relations between records other families declare, only as far as cited evidence states them. An
  exact join of declared values (two declarations giving one id) is evidence; anything estimated is
  `inferred` and lives in `derived/<kind>.jsonl` with the same fields. Nothing merges or re-times.
- `validity`: `Knowledge[ValidityWindow]`, `{clock, start, end}` on one `TimestampDomain`, start
  inclusive and end exclusive; `KnownAbsent` bound = stated open, `Unknown` = not stated.
- `IdentityLink {left, right, basis, identifier, evidence}`: `right` `Known` or `Ambiguous`, never `left`;
  `co_declared` (one declaration, two ids) or `shared_identifier` (two declarations, one `identifier`).
  Never a merge: both ids stay keys.
- `ClockMapping {source, target, method, anchor, rate, residual_bound}`:
  `target(t) = anchor.target + rate * (t - anchor.source)` in ticks, `rate` an exact positive fraction,
  `residual_bound` a `Duration` on the target clock, `validity` on the source clock.
- `FrameBinding {parent, child, transform, basis, calibration}`: the `FrameTransform` that gives one graph
  edge its value (`robot_description`, `calibration`, `transform_message`).
- `RunAssembly {run, rule, members}`: each member a `SourceRevision` id, a role (`recording`,
  `description`, `context`) and the `EvidenceRef` that places it in the run.
- `SnapshotBinding {run, snapshot, snapshot_kind}`: the hardware, software, calibration or configuration
  snapshot a run ran with, over a window on one of its clocks (MVL-38).

## World and record context (ADR 0020; `model/world.py`)

- `Site` / `Asset`: a place or thing one declaration names (a register row, a manifest entry, a GeoJSON
  feature): `identifiers`, `name`, `aliases` (each cited, an alias split from a cell cites its span),
  `parent`, `location`; an asset adds its declared `category` and `site`. At least an id or a stated
  name. The rest of a register row stays in its `StructuredRecord`.
- `SpatialArtifact`: a mesh, CAD model, point cloud, map or scene by reference: `category`, `name`, the
  declared coordinate `unit`, `crs` and `frame`. Geometry stays in the source bytes; objects are cited
  with `ObjectLocator`.
- `Image` (a still) and `Video` (one track of a standalone file): pixel `width` / `height`, `encoding`,
  and `capture` (time, position, device make, model and ids). EXIF orientation is kept, never applied.
  Regions are `ImageRegion`, after a `VideoFrame` for video. A video inside a log is a stream.
- `DocumentRecord`: `format`, `title` and `pages` (label, size in page coordinates, rotation).
  `DocumentBlock`: one unit of extracted text in reading order, with its `text` exactly as its
  `[Page, Span]` citation holds it, its drawn `region` (`[Page, PageRegion]`), and `role` / `level`
  only where the format declares them. Guessed roles are derived.
- `StructuredTable`: `name` and `header` (verbatim cells, citing the header row). `StructuredRecord`:
  one row, cells in their source's types (a CSV's text stays text). Blank is `Unknown`; a defined
  "none" is `KnownAbsent` citing the definition. A row cited as `Row(r)` hoists its cells' citations:
  cell `c` is `RowCell(r, c, header[c])` (`cell_evidence`).

## Deployment lifecycle records (ADR 0051; `model/lifecycle.py`)

- Eight `world` kinds from schema version 4, each `stated` by one form, ticket, work order or register
  row: `CommissioningBaseline`, `AuthorisationEnvelope`, `Intervention`, `MaintenanceEvent`,
  `RequalificationRecord`, `IncidentRecord`, `ChangeRecord`, `RiskAssessment`.
- Shared fields, all declared ids: `identifiers` (the record's own), `site`, `machines`, `configuration`
  (a maintenance event's is the as-maintained one it states) and `related` (records and evidence it
  names). Never record ids: MVL-35 links them.
- Stored as declared: severities, results, decisions, authorities, methods and scores are verbatim text
  (`Score(name, value)` keeps the source's label); a `Quantity` is a declared number and declared unit;
  times are `Timestamp`s on the clock the record names; versions are the kind the source names.
- Parts: `Quantity`, `Decision` (decision, authority, time), `InventoryItem`, `TestResult`, `ZoneLimit`,
  `PartReplacement`, `TimelineEntry`, `ChangeItem`, `Score`, `Hazard`. Statement lists keep source order and
  may repeat; id lists are sorted and unique; an empty list states none.
- No lifecycle logic: nothing orders stages, checks one record against another or ranks a severity.

## The package and its receipt (ADR 0022; `model/package.py`, `store/`)

- A package is a directory: `manifest.json`, `receipt.json`, `receipt.md`, `records/<kind>.jsonl` (every kind of
  the package's schema version; empty file = none), `derived/<kind>.jsonl` (inferred tables, below),
  `series/<stream hex>.parquet`, `blobs/sha256/<2>/<64>`, and `volatile/receipt-envelope.json`. The manifest and
  receipt carry the package's version: the lowest that holds its records (ADR 0037 §1).
- `PackageManifest`: the receipt's id, record counts per kind, a handle per source (content id, size, referenced
  or materialised), every file's size and sha256, and the store's settings. The package id is the manifest's
  sha256.
- `IngestReceipt`, the deterministic core, is computed from the package's own records: sources and which
  transforms read them, transforms (the replay inputs), counts, clocks, runs and streams with their declared
  coverage, entities with their ids, findings most severe first, and every ambiguous field. Its id hashes the
  rest; readers recompute it and refuse a receipt that differs. `receipt.md` renders it without converting any
  time.
- `ReceiptEnvelope` holds the job id, wall clock, host, ingest root and durations, outside the manifest, so it
  never changes the package id. Sources are referenced by default; materialising is opt-in.
- `derived/<kind>.jsonl` holds derived tables apart from `records/` (ADR 0036, amending ADR 0023 §5):
  canonical lines sorted by id, each `assertion_kind` `inferred` (`stated` for a session the user declared,
  `observed` for a stream's parsed definition) and naming a transform in the package, listed in the manifest.
  The store checks their structure; `neptune.derived` reads their meaning and refuses kinds it does not define.
  The kinds are `session_proposal` and `session_unassigned` (run/session grouping), and `definition_layout`,
  `stream_layout` and `stream_semantic` (a distinct definition's declared field paths and types, written once;
  each stream's line naming it; and what the stream carries, inferred; ADR 0049), `media_stream` (a stream
  carrying images, video or point clouds: its media kind, frame count, hydrator and derivative states; its
  frames are its series rows, queried and hydrated lazily by `neptune.sdk.media`; ADR 0056), and
  `timestamp_domain` and `clock_mapping` (a clock found in a stream's values, and a mapping fitted from sync
  anchors; ADR 0060), and `frame_tree`, `frame_edge`, `frame_link`, `frame_group` and `spatial_reference`
  (frames across run trees and declared graphs, and what each subject's values are in; ADR 0068). Present and
  empty means the producer ran and inferred nothing, absent means it did not run.

## Serialization (ADR 0002)

- Entities: JSON Lines, one table per entity kind, canonical JSON (sorted keys, fixed number formatting) so
  bytes are hashable and diffable.
- Time-series: Parquet, one file per stream, row groups sized for range queries, rows sorted by clock-0
  ticks, then source order (ADR 0018).
- Raw evidence: content-addressed blobs, byte-identical to source.
- Every record carries `kind` and `schema_version`, the version that added its kind (ADR 0017 §2, ADR 0037 §1).
- NaN and ±Infinity never appear in JSON. A record field that may hold them is typed `Real`, and a non-finite
  value is written `{"non_finite":"inf"}` (`model/scalars.py`, ADR 0017 §8). Parquet keeps IEEE values.

## JSON Schema (ADR 0021)

- `docs/schema/canonical.schema.json` is generated from these types (`make schema`) and checked for drift by a
  test. One line of any record table validates against it; `#/$defs/<Kind>` holds each kind.
- It checks keys, types, tags, enums and id syntax. The Python readers check the rest (order, uniqueness,
  ranges, non-empty text, cross-field rules, `1` versus `1.0`): what a reader accepts always passes the schema.

## Worked examples (ADR 0021; `tests/fixtures/model/`)

| Example | Sources | Records |
|---|---|---|
| drone | PX4 ULog | run, streams with boot and GPS clocks, machine by `sys_uuid`, hardware, firmware, calibration, findings; the log as its run's member and three snapshot bindings |
| quadruped | ROS 2 bag, URDF, STL mesh | run from bag metadata, joint and trajectory streams with three clocks each, URDF frames, transforms and components, the mesh as geometry; the bag's file list, its stated `starting_time` → `log_time` clock mapping, URDF edge bindings |
| manipulator | MCAP, hand-eye YAML | run, joint and camera streams, hand-eye calibration with an `Ambiguous` direction and a finding for its missing unit; the calibration's edge binding |
| mobile robot | ROS 1 bag, site register CSV, PNG photo | run and streams, register table and rows, sites citing their cells, the photo's pixels and EXIF capture; the bag as its run's one member |
| warehouse AMR | deployment records JSON | commissioning, authorisation envelope, intervention, incident, change and risk assessment (ADR 0051) |
| manipulator cell | deployment records JSON | commissioning, risk assessment, maintenance event and requalification (ADR 0051) |

Every source is a real file, and every record resolves back to it: citations land on real records, pointers,
rows and cells resolve, and every id a record names is in the example.
