# 0018 — Runs, streams and the series layout

- Status: Accepted
- Date: 2026-10-01
- Issue: MVL-67 (sub-issue of MVL-1)

## Context

ADR 0017 fixed the record envelope. MVL-67 defines the `run` family: the sessions evidence
declares, the timestamped channels recorded in them, and the Parquet layout of their samples.
Earlier ADRs constrain it:

- ADR 0002 §3: anything per sample goes to Parquet, rows are sorted by `(domain id, ticks, source
  order)`, and states and per-row provenance are columns.
- ADR 0004 §7: a wrapped column is a value column plus a dictionary-encoded state column.
- ADR 0005: a time is ticks in a named clock domain, and nothing converts or picks a clock at
  parse time.
- ADR 0006 §6: the transform and assertion kind are hoisted onto the `Stream`, each row carries its
  own locator columns, and full per-row provenance must be rebuildable from the two. `explain`
  (MVL-39) depends on it.
- ADR 0017 §5: a session that a procedure assembles from several sources is inferred and lives in
  `derived/`.

Two failure modes shape the design. A log's samples usually carry several clocks: an MCAP message
has a log time and a publish time, and a ROS message often has a `header.stamp` too. Choosing one
as "the" time throws away the others and silently asserts that they agree. And a series can hold
billions of rows, so provenance has to be cheap per row and still exact.

## Decision

1. **`Run`: a session that one piece of evidence declares.** Kind `run`, family `run`.
   - The declaration is a recording (MCAP, bag, ULog), a rosbag2 `metadata.yaml` or a manifest
     entry. The record-level provenance cites it: `observed` for a recording, `stated` for a
     manifest.
   - `logical_id: Knowledge[LogicalId]` is the id the evidence gives the session.
     `machine: Knowledge[LogicalId]` is the declared identifier of the machine that recorded it
     (a ULog `sys_uuid`, a manifest's robot), never inferred from a topic prefix or a folder name.
   - `first` and `last: Knowledge[Timestamp]` are the session's first and last instants, both
     inclusive, on whatever clock the evidence states them on. They are separate fields because a
     source may state one and not the other: a ULog header states the start only. They are not
     checked against each other; implausible declared values are validation's (ADR 0004 §5).
   - A `Run` lists no members. A stream names its run (§2). Binding configs, URDFs or software to a
     run is MVL-38, manifests are MVL-14, and heuristic groupings are derived records from MVL-13
     and MVL-34.
2. **`Stream`: one channel exactly as its source declares it.** Kind `stream`, family `run`.
   - The record-level provenance cites the declaration: an MCAP channel record, a bag connection, a
     ULog subscription. One stream per declaration: a topic split across several files (split
     bags) is several streams of one run.
   - `run: RecordId` is the run the same transform says the stream was recorded in (structural).
   - `topic: Knowledge[str]` is the name, verbatim; `NotApplicable` where a format has none.
   - `schema_name` and `schema_encoding: Knowledge[str]` are the declared type (`sensor_msgs/msg/Imu`)
     and the language its definition is written in (`ros2msg`). `schema_definition:
     Knowledge[EvidenceRef]` cites the definition's bytes. All three are `KnownAbsent`, citing the
     format, where the format says a channel has no schema (MCAP `schema_id` 0).
   - `message_encoding: Knowledge[str]` is how each sample's bytes are encoded (`cdr`).
   - `metadata` holds the other properties the declaration states as text, verbatim and sorted by
     key: MCAP channel metadata, ROS 1 connection fields, a ULog `multi_id`. It is structural.
   - `clocks: tuple[RecordId, ...]` is §3.
   - `message_count: Knowledge[int]` and `first` / `last: Knowledge[Timestamp]` are what the source
     declares in an index or summary, the times on one of the stream's clocks. What the series
     holds is counted from the series; a disagreement is a validation finding.
   - `Knowledge[str]` values are non-empty. A blank is `Unknown` (ADR 0004 §5).
   - `series: SeriesProvenance` is §5.
3. **Every clock is kept, and none is chosen.**
   - Each clock the samples carry is its own `TimestampDomain` (ADR 0012) and its own column
     `time/<i>`, where `i` is its position in `clocks`. One MCAP message's log time, publish time
     and `header.stamp` are three columns in three domains.
   - Clock 0 is the clock the source itself orders or indexes its samples by. It orders the stored
     rows (§7) and means nothing more: it is not the stream's time.
   - Relating clocks is a `ClockAlignment` (MVL-36). Until then, comparing them raises.
4. **The series column contract.** One Parquet file per stream, one row per sample.

   | Column | Parquet type | Holds |
   |---|---|---|
   | `seq` | int64, never null | the sample's 0-based position among the stream's samples in source order |
   | `time/<i>` | int64 | ticks on clock `i`, exactly as encoded. Never the TIMESTAMP logical type, which would assert a unit and UTC |
   | `locator/<i>/<field>` | int64, UTF-8 or double, never null | the fields of locator step `i` that vary per row (§5) |
   | `value/<name>` | as decoded | a decoded field, exactly as the source encodes it |
   | `state/<column>` | dictionary UTF-8, never null | the `KnowledgeState` of wrapped column `<column>` (§6) |

   - Names are namespaced, so nothing a source names can collide with Neptune's columns: a field
     called `seq` becomes `value/seq`.
   - Every sample the source holds for the stream is a row, or a finding says why it is not. A
     stream whose payload Neptune does not decode still has its series: time and locator columns,
     no value columns, and a finding.
   - Adapters name the value columns and document how nested fields map onto them. MVL-21
     standardises that mapping for message schemas, together with declared field types and units.
5. **Row provenance: hoisted, plus per row.**
   - `SeriesProvenance(source, locator, assertion_kind)` sits on the `Stream`. The transform is the
     stream's own.
   - `locator` is a tuple of step templates. Each is a core step kind or an adapter's
     `<adapter id>:<name>`, its fixed fields, and the names of the fields each row supplies in its
     `locator/<i>/<field>` columns. A template filled with one row's values, then parsed strictly
     (ADR 0016), is that row's locator.
   - At least one field varies per row, so every row cites its own sample. A coordinate frame
     (`frame`) is not a place a sample is read from.
   - `Stream.row_provenance(row)` is `Provenance(EvidenceRef(source, filled template), stream
     transform, assertion_kind)`. `Stream.row_time(row, i)` reads a time in its own domain, and
     `Stream.check_row(row)` checks the whole contract.
6. **States in a series.**
   - A wrapped column has `state/<column>`, holding `known`, `unknown`, `not_covered` or
     `not_applicable`. The column is null exactly where the state is not `known`. A sentinel the
     specification defines is not kept beside its state; the row's locator still reaches its bytes.
   - `KnownAbsent` needs a citation of what defines the absence, and `Ambiguous` needs its
     candidates. Neither fits one cell, so an adapter writes `unknown` and a finding that says what
     the evidence said.
   - Value columns keep IEEE values. A non-finite value no specification defines as a sentinel
     stays as decoded (ADR 0017 §8).
7. **Order.** Rows are sorted by their ticks on clock 0, rows without them last, then by `seq`.
   This is how ADR 0002 §3 and ADR 0005 §7 apply: a series has one clock 0, so the domain in their
   `(domain id, ticks, source order)` key is constant. `seq` breaks ties and recovers source order.
   The contract fixes what `seq` means; whether the adapter or the store numbers it is MVL-7's.
8. **Self-describing files.** The series file's key-value metadata holds the `Stream` record's
   canonical JSON under the key `neptune.stream`, so a series file alone rebuilds any row's
   provenance. File names, row-group size and writer settings are MVL-5's and MVL-16's. The
   contract lives in `neptune.model.series` and `neptune.model.run`, which stay standard-library
   only; the Parquet writer belongs to the store.

## Alternatives considered

- **One canonical time per sample**, such as `header.stamp` when present and the log time
  otherwise. Consumers get one column, but the other clocks' evidence is gone and the choice
  asserts that the clocks agree, which is exactly what ADR 0005 forbids.
- **One row per (sample, clock)**, a long format. Every clock is kept, but rows multiply by the
  number of clocks and one sample's values are split across rows.
- **Time columns named after the source's field** (`log_time`, `header.stamp`). They read well, but
  they can collide with value names, and one field name can mean different clocks on different
  topics. Positions map to domains through `clocks`.
- **Rows kept in source order, with no sort.** The store could append chunk outputs without a merge,
  and no clock would be privileged even in storage. It contradicts ADR 0002 §3 and ADR 0005 §7 and
  gives up exact range scans on the clock the source indexes by; row-group statistics would still
  prune. The ADRs' order is kept, and `seq` preserves source order.
- **A source column per row**, for a topic spread over several files. Every format in scope declares
  a channel in the file that holds its messages, so a split recording is several streams of one run.
- **A separate schema record kind**, as MCAP shares one schema between channels. A name, an
  encoding and a citation per stream cost little. MVL-21 can add a schema record with field types
  if its registry needs one.
- **The run's extent as one interval value.** A source often states a start without an end.
- **Keeping a sentinel in the value column beside its state.** It is lossless, but a consumer
  reading the value column alone would take a covariance of -1 as a value.
- **`machine` as a list**, for fleet sessions. No source in scope declares several machines for one
  session. Going from one id to a list later is a lossless migration (ADR 0017 §7).

## Consequences

- A log keeps every clock its samples carry, and alignment (MVL-36) can use all of them. A consumer
  names the clock it wants; there is no default.
- Row provenance costs a few integer columns and stays exact: the `Stream` line plus the row is
  everything `explain` needs.
- The store sorts each series by clock 0 across chunks, which is a merge of sorted chunk outputs.
- Per-row `KnownAbsent` and `Ambiguous` are not representable in v0.
- Revisit if sources need per-row `KnownAbsent` or `Ambiguous` often, if a format declares one
  channel whose samples live in several files, if range queries on clocks other than clock 0 are
  too slow, or if a source declares several machines for one session.
