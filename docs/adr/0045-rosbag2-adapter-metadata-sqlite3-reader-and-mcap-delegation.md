# 0045 — rosbag2: one adapter for `metadata.yaml` and sqlite3 storage, MCAP left to the MCAP adapter, a SQLite reader over bytes

- Status: Accepted
- Date: 2026-10-02
- Issue: MVL-19

## Context

A ROS 2 bag is a directory: `metadata.yaml` and one or more storage files, `.db3` (sqlite3) or `.mcap`
(`storage_identifier` says which), split by size or duration into `<name>_0`, `<name>_1`, and so on. MVL-19
asks for the metadata, the sqlite3 reader, MCAP-backed bags, QoS and topic metadata, canonical streams and
split-bag assembly, and accepts when the storage backends of one recording give equivalent canonical run and
stream objects.

The frame is fixed. An adapter reads one source through a `SourceReader` (bytes, no path) and cites only that
source (law 9). Every file of a bag is a source; grouping them is session grouping's job (ADR 0036, rule
`rosbag2_directory`), and an inferred grouping is never a `Run` (ADR 0017 §5). Record kinds are frozen
(ADR 0023). ADR 0034's consequences expected MVL-19 to need the MCAP reader "lifted into a shared non-adapter
package, by ADR, first".

## Decision

1. **MCAP-backed bags are delegated, not lifted.** rosbag2's MCAP plugin writes ordinary MCAP: profile `ros2`,
   `ros2msg` schemas, `cdr` messages, and the offered QoS profiles in the channel's metadata. A member `.mcap`
   file is claimed by the MCAP adapter on its magic and gives, unchanged, the run, clocks, one stream per topic
   and one row per message (ADR 0034); the bag's `metadata.yaml` is claimed by this adapter. No MCAP code is
   shared, moved or copied, the MCAP golden is untouched, and ADR 0034's expectation of a shared reader is not
   needed: nothing rosbag2 adds lives inside the MCAP file.
2. **sqlite3 storage is read by our own SQLite reader (`rosbag2/_sqlite.py`), not by `sqlite3`.** The standard
   library opens a path. A `SourceReader` is bytes, so `sqlite3` would need the database copied to scratch for
   every `plan` and every `ingest` call (against chunk purity, a 1 GiB scratch cap, and hosts without Landlock,
   where scratch is absent and the run would fail), or `Connection.deserialize` of the whole file into memory.
   The reader walks the table b-trees the file format defines, straight from the source:
   - read-only by construction: no engine, no SQL, no journal, no `-wal` or `-shm` file is opened or written.
     This is what `file:...?mode=ro&immutable=1` gives (and what `rosbags` does), without a path;
   - hostile input: page size, reserved bytes, text encoding, cell counts, cell pointers, varints, payload
     sizes and interior keys are checked against the page and the file before use; a damaged page is a
     `damaged_page` problem and the walk continues with the rest; a walk visits at most the file's page count
     and is at most 40 deep (a cycle costs its budget); rowids must strictly increase and stay within the range
     their parents' keys give, so any rowid range of the table is the same rows however it is cut;
   - scope: rowid tables only, UTF-8 only, no index use, and payloads that spill into overflow pages are
     measured (their size is in the record header) and not followed. A row cites its cell, so a consumer that
     wants the blob follows the overflow chain from the cell's last four bytes.
   Tests use the standard library to write databases and to read them back as the oracle, at four page sizes
   and every integer width.
3. **`metadata.yaml` is read by a YAML subset parser (`rosbag2/_yaml.py`).** PyYAML is not a dependency and
   gives no byte spans. The subset is what yaml-cpp writes: block mappings and sequences, plain and quoted
   scalars (double-quoted escapes resolved), `|` and `>` blocks, single-line flow collections of scalars,
   comments, one document. Anchors, aliases, tags, complex keys, nested or multi-line flow collections,
   multi-line scalars and tab indentation are refused with the line, costing the entry they belong to
   (`unsupported_yaml`). Size (4 MiB), depth (32) and node count (200,000) are bounded.
4. **What a file becomes.**
   - *Metadata* (one chunk): one `Run` (`stated`) citing the `rosbag2_bagfile_information` mapping, `first` the
     declared start and `last` start plus duration (the bytes of both cited as one value), on a
     `TimestampDomain` named by the key the ticks come from (`starting_time.nanoseconds_since_epoch`), with
     role, epoch and timescale `Unknown` (the key does not say which clock); and `StructuredTable`s: the scalar
     entries as (key path, value), `topics_with_message_count` (name, type, serialization_format,
     offered_qos_profiles, type_description_hash, message_count), `files` and `relative_file_paths`. Every row
     cites its entry and every cell its own bytes, all `stated`; a plain decimal integer is a number, anything
     else its text, a blank is `Unknown`. The metadata makes no `Stream`: the data files do, and a second
     stream per topic from the bag's own statement would be a duplicate under another run.
   - *sqlite3* (a declarations chunk and rowid-range data chunks): a `Run` citing the database header whose
     `first`/`last` are the smallest and largest message timestamp (`observed`, each citing its timestamp's
     bytes, or the cell where a value has none); a `TimestampDomain` for the `timestamp` column (scope `()`,
     role `receive`, 1 ns, as the MCAP adapter's `log_time`); a `Stream` per `topics` row citing it, with type,
     serialisation format, the QoS and type hash as stream metadata when not blank, the definition cited from
     `message_definitions` when the table is there and the text is in its page, and `message_count` as
     counted. One series row per message: `seq` (rank among its topic's messages by rowid), `time/0`,
     `value/message_id`, `value/data_bytes` and the cell's `locator/0/offset` and `length`. Payloads are not
     decoded (`payload_not_decoded`, MVL-21).
5. **Equivalence of the storage backends** is tested on one recording written both ways (a mobile base:
   velocity, battery voltage, status text; both validated with `rosbags` and `mcap`). The data files give
   equal: run `first`/`last`; clock 0's scope, role, resolution, epoch, timescale and monotonicity; and per
   topic the schema name, schema encoding, definition bytes, message encoding, metadata (QoS), message count,
   and every message's `seq`, time and payload. Provenance, record ids, assertion kinds and the MCAP file's
   second clock (`publish_time`) are not part of the equivalence. The two metadata files give equal runs, topic
   and file tables, and differ only in `storage_identifier` and the part's name.
6. **Split bags.** Each part is a source with its own run and one stream per topic (ADR 0018); the parts of a
   bag are one recording through grouping (`rosbag2_directory`: `metadata.yaml` and every part). The `files`
   table keeps the declared order, which is the assembly order; sorted by start time it is the recording's
   timeline, and a list out of order is `part_order`. An adapter sees one file, so it reports what a file says
   of itself: `parts_disagree` (`relative_file_paths` and `files` list different parts, each missing from or
   extra to the other), `part_gap` (numbering from 0 with gaps, naming the missing indices), `duplicate_part`,
   `count_mismatch`, `storage_mismatch`, `unsafe_part_path` (absolute, parent, backslash or empty; kept as
   declared, never opened) and `compressed_storage` (file or message compression; no adapter reads `.zstd`
   parts). Reconciling what is listed with what is present is a check across sources (law 9) and belongs to
   `validate/`, which does not exist yet: it is a follow-up, not an adapter's guess.
7. **Time.** Ticks are stored as declared. The metadata's clock and the data files' clocks are separate
   domains; relating them is a `ClockAlignment` (MVL-36). No UTC, no unit conversion; the epoch stays
   `Unknown` because neither the metadata key nor the database states which epoch.
8. **Planning reads every leaf page of `messages` once** (never the overflow pages), because `seq` is a rank
   within the topic and the extent is a minimum and maximum. Chunks are rowid ranges of at most `max_rows`
   (constructor argument, 100,000) valid rows, each carrying the `seq` its topics start from, so output does
   not depend on where ranges fall. Page-level problems and refused rows (`bad_row`: record does not parse or
   topic/timestamp not an integer; `unknown_topic`) are findings made once by the plan; chunks apply the same
   rules silently, so no finding is emitted twice.
9. **Probe** decides from bytes: SQLite magic plus a readable schema holding rosbag2's `topics` and `messages`
   tables is `VERIFIED`; any other database, and a header whose schema does not read, is 0. A top-level
   `rosbag2_bagfile_information:` key in the head is `SIGNATURE`, with `version` and `storage_identifier`
   `VERIFIED`. The name is never used.

## Alternatives considered

- **Lift the MCAP reader into a shared module** (ADR 0034's expectation). It would move 3,000 lines for no
  gain: the MCAP adapter already produces the canonical objects for a rosbag2 MCAP file.
- **Have this adapter claim `.mcap` files in bags.** It cannot see the directory, ties with the MCAP adapter
  at the same confidence, and would duplicate the parser.
- **`sqlite3` on a scratch copy or `deserialize`.** Copy per call, scratch absent on some hosts, memory
  bound by file size, and the C engine parsing hostile pages anyway; kept as the test oracle only.
- **PyYAML.** A new runtime dependency, no byte spans, and anchors/aliases (YAML bombs) to defend against.
- **A `Stream` per topic from `metadata.yaml`.** Duplicates the data files' streams under a different run.
- **Per-stream `first`/`last` for sqlite3.** The planning pass has them, but MCAP declares none and the fields
  mean "what the source declares"; both stay `Unknown` so the backends agree.
- **Reconciling listed and present parts in the adapter.** Impossible without citing two sources.

## Consequences

- Both rosbag2 backends reach the canonical model with the same run and streams; QoS is preserved verbatim.
- A sqlite3 file is planned in one pass over its leaf pages (read cost proportional to the table's leaf pages,
  not its payload bytes); a database of hundreds of millions of rows pays that once, then resumes by chunk.
- A WAL-mode database's `-wal` file is another source and is not read (`wal_not_read`, info); messages only it
  holds are missing.
- The SQLite reader is ours to maintain; it reads one format version family and refuses the rest with a finding.
- Follow-ups: cross-source part reconciliation in `validate/`; compressed parts (`.db3.zstd`, `.mcap.zstd`,
  per-message compression); payload decoding and `header.stamp` clocks (MVL-21); aligning the bag's clocks
  (MVL-36); a golden package for a bag once the corpus (MVL-49) exists.
- Revisit if a bag layout puts one topic's messages in several files with shared numbering, if `validate/`
  lands (the part findings may move there), or if a rosbag2 storage other than sqlite3 and MCAP appears.
