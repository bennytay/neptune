# 0048 — The flight-log adapter: PX4 ULog and ArduPilot DataFlash as runs, decoded streams and cited tables

- Status: Accepted
- Date: 2026-10-02
- Issue: MVL-20

## Context

PX4 and ArduPilot run multicopters, fixed wings, rovers, boats and submarines, and both write a
self-describing binary log: PX4's ULog (definitions section, then timestamped messages) and ArduPilot's
DataFlash `.bin` (a `FMT` record per message type, then records). MVL-20 asks for both, with parameters,
firmware identifiers, timestamps, events, sensor, state and control streams and native schema provenance,
so that a team can ingest raw flight logs and keep what later physical analysis needs.

The frame is fixed: the model is frozen (ADR 0023), so no new record kinds; adapters never import each
other (ADR 0024); the runtime runs every call in a confined child (ADR 0030); output must not depend on how
a plan cuts the source (ADR 0034 §6); a damaged source is findings, never a raise. ADR 0040 already reads
PX4 and ArduPilot *firmware and parameter files* (identity declared per file); this ADR reads the *logs*.
MCAP (ADR 0034) is the reference for `Run`, `Stream`, series rows and exact citations, but its payloads are
opaque until MVL-21. These two formats carry their own schema, so their payloads are decoded here.

## Decision

1. **One adapter, `flightlog`, two formats.** Both formats become the same shape (a run, a boot clock, a
   stream per declared message type, tables for what the log states about itself), and the adapters cannot
   share code except through a common module of one adapter. Two adapters would duplicate the cursor,
   findings tally, table and series machinery, or need a shared non-adapter package that ADR 0034 says needs
   its own ADR. The cost is one lineage: a version bump re-ingests both formats. The format parsers stay
   separate modules (`ulog*.py`, `dataflash*.py`) and only `common.py` is shared. No new runtime dependency:
   only `struct`. `pyulog` and `pymavlink` are test oracles (`oracle.json`), fetched by `uv` for that run only.
2. **Probe.** ULog: the 7-byte magic is `SIGNATURE`; a complete header with version ≤ 1 and a plausible
   message type after it is `VERIFIED`. DataFlash has no magic: a source that opens with `A3 95 80` (a `FMT`
   record) is `SIGNATURE`, and one whose first record is the standard `FMT`-of-`FMT` payload is `VERIFIED`.
   Anything else is 0 whatever its name (`.bin` is a hint only).
3. **What a log becomes.**
   - One `Run` citing the ULog header (the first record header of a DataFlash log). A ULog's header
     timestamp is `first`, `stated`, on the boot clock; its info `sys_uuid`, when present, is `machine`
     (`px4.sys_uuid`, `stated`, citing the value bytes). `last` and everything a log does not state is
     `Unknown`. No `Machine` record: a `sys_uuid` alone is not a description of a machine (MVL-41 binds
     firmware and software identity to runs).
   - One `TimestampDomain` for the log's boot clock: ULog `timestamp`, DataFlash `TimeUS` (or `TimeMS` in old
     logs, a second domain). Scope `()`, role `sample`, resolution 1 µs (1 ms), epoch `boot`, timescale
     `monotonic`, each `Known` citing the declaring header or `FMT` record. Ticks are stored as declared.
     **GPS time is never merged into it and never converted to UTC**: GPS messages are streams like any
     other and their `time_utc_usec` / `GWk` / `GMS` fields are plain value columns.
   - One `Stream` per ULog subscription (`A` message; topic the message name, metadata `msg_id`, `multi_id`
     and the text of every nested format used) and per DataFlash `FMT` type that is not a definition or a
     parameter record. `schema_definition` is the exact bytes of the format message (the whole `FMT` record).
     `schema_encoding` is `ulog_format` / `dataflash_fmt`. ULog logged messages (`L`, `C`) and dropouts (`O`)
     are streams too (`metadata message_type`), the dropout rows carrying their duration and a time that is
     `not_covered`: the message has no timestamp, and borrowing the previous message's would be an inference.
   - One series row per message: `seq`, the clock, the fields as `value/<name>` columns **as stored**, and a
     `byte_range` locator to the whole message or record. ULog nested types flatten to `a.b` (`a[0].b` for
     arrays of them), primitive arrays are repeated columns, `char` arrays strings up to the first NUL with a
     `state` column (invalid UTF-8 is `unknown`, never replaced), `_padding*` fields are dropped, and the
     top-level `uint64 timestamp` is the time column. DataFlash `c C e E` and `L` columns keep their raw
     integers (the format character says ×0.01 and ×10⁻⁷); no value is scaled or converted to SI.
   - Tables, every cell `stated` and citing its bytes: ULog `flag_bits`, `info`, `info_multi`, `parameters`,
     `parameters_default` (definitions section) and `info_data`, `info_multi_data`, `parameter_changes`,
     `parameters_default_data` (data section); DataFlash `parameters` (one row per `PARM` record, header the
     `FMT` labels) and `field_units`. Info and parameter values keep the type the key declares.
   - **Declared units** (DataFlash): `field_units` has a row per declared column with message, field, format
     character, unit id (`FMTU`), unit text (`UNIT`), multiplier id (`FMTU`) and multiplier (`MULT`), each cell
     citing the bytes that state it and `stated`; a unit id with no `UNIT` record is `Unknown` with a
     `unit_undeclared` finding; a log with no `FMTU` says `units_not_declared`. The same facts ride on the
     stream as verbatim text (`unit.<field>`, `unit_id.<field>`, `multiplier.<field>`, `multiplier_id.<field>`).
     A ULog declares no units, only types, which are the columns' types; nothing is invented.
4. **Planning walks the log once and is where every finding is made.** `plan` runs the same `Walk` that
   `ingest` runs, without building rows: it follows the format's rules byte for byte, counts each stream's
   rows, collects the declarations (formats, subscriptions, `FMT` table, unit records, the first place of each
   table and pseudo-stream) and cuts the log into pieces at message boundaries (`chunk_bytes` 8 MiB,
   `max_rows` 100,000, table rows weighing 16, constructor arguments). Because it sees the whole log, its
   findings are whole-log tallies, so output, findings included, is identical whatever the plan cuts; chunks
   emit none. Each chunk carries the declarations it needs, its byte range, each stream's `seq` start and each
   table's row start. Chunk 0 emits the run, the clock and the streams.
5. **ULog rules.** Header (version > 1 is read as 1 with a warning); flag bits at offset 16; unknown
   incompatible flag bits mean the messages are not read (the format requires refusing; the run and a finding
   remain); `appended_offsets` start further regions, each walked on its own, a bad offset or leftover bytes a
   finding. Data messages leave out trailing `_padding` (as PX4 writes them): the minimum size is the bytes
   up to the last real field, a shorter message is `size_mismatch` (error, no row), a longer one a warning.
   Data before its subscription, for an unsubscribed id or for a format that does not lay out has no rows
   (`unknown_message_id`). A subscription's first declaration wins. Unknown but plausible message types
   (upper-case ASCII) are skipped by size and counted; a damaged header skips to the next sync message
   (`corrupt_bytes`, with the bytes lost). Limits: 4,096 formats and subscriptions, 2,048 columns, 65,535
   bytes, 8 levels of nesting, 1,024 elements of an array value.
6. **DataFlash rules.** The table starts with the standard `FMT`; the first declaration of a type wins, a
   different redeclaration is `conflicting_format`. A format whose characters, labels or declared length do
   not agree is unusable (`bad_format`): its records are skipped by their declared length (`unreadable_records`).
   A header that is not `A3 95` + a declared type (or comes before its type's `FMT`) skips to the next declared
   record (`corrupt_bytes`). Names `FMT`, `FMTU`, `UNIT`, `MULT` are definitions, `PARM` a table, the rest
   streams; `TimeUS` / `TimeMS` as the first integer column is the time column, otherwise the rows have no
   time (`not_covered`, `no_time_field`).
7. **Hostile input.** A log cut anywhere yields a strict prefix of the rows of the whole (`truncated`, one
   finding); a time past 2^63 − 1 is `unknown` in its row (`time_out_of_range`); counts and lengths are
   checked before anything is read; a problem met many times is one finding with `count` and the bytes lost;
   memory is a 1 MiB window plus one chunk's rows (peak measured under 120 MiB for 150,000 rows).

## Alternatives considered

- **Two adapters (`ulog`, `ardupilot`).** Cleaner descriptors and independent lineage, but the shared
  machinery would be copied or require a shared package by ADR. Lineage coupling is cheap: logs of both
  formats are re-ingested together on a parser change.
- **`pyulog` / `pymavlink` as the readers.** They raise on damage, read through file objects, do not give a
  message's byte offset, scale and convert values (pymavlink applies ×0.01 and ×10⁻⁷, and pyulog gives a
  dropout the last timestamp it saw), and add dependencies. They are oracles instead.
- **Findings from each chunk.** Natural, but a tally per chunk changes with the cuts (ADR 0034 §6); plan-time
  findings need the plan to decode times and strings for validation, which is cheap.
- **A `Machine` and a `SoftwareConfiguration` from info and `MSG` records.** The firmware version strings stay
  in the `info` table and the `MSG` stream, cited; binding them to a run is MVL-41's and ADR 0040's.
- **Dropout and parameter-change times from the last message.** pyulog does; it is an inference the log does
  not state, so the rows say `not_covered` and the bytes keep their order.
- **A ULog `value/timestamp` column.** It would repeat `time/0`; a `uint64` past 2^63 − 1 is unknown in the
  time column and the bytes are cited.

## Consequences

- A PX4 or ArduPilot log lands in the Run/Stream model MCAP uses, with the fields decoded as stored, the
  message definitions kept as cited bytes and the declared units stated and cited, for a multicopter, a rover
  or a boat alike.
- Planning is one pass over the log in Python (about 1 s per million messages); a 500 MB log costs tens of
  seconds of CPU in one call, against the sandbox's `cpu_seconds`. Raise the limit per adapter in M9 or split
  planning if logs of that size matter.
- Not done, and left to follow-ups: the ArduPilot text `.log`, MAVLink `.tlog` (a different framing), topic and
  time selection config, GPS week/millisecond clocks as their own `TimestampDomain`, and decoding firmware
  strings into software identity. Streams that stay empty in a log still get a typed, empty series.
- Revisit if a log format version changes the structure (ULog version 2, DataFlash 2), if units become a
  model field, or if planning in one call proves too slow for real logs.
