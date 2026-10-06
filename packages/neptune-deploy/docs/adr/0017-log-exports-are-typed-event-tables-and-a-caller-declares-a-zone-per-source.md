# 0017 — Log exports are typed event tables, and a caller declares a zone per source

- Status: Accepted
- Date: 2026-10-06
- Issue: MVL-191
- Builds on: ADR 0002 §5 and §7, ADR 0010 §7 (the diagnostics table), ADR 0012 §2 (civil zones),
  ADR 0016 §1 and §9 (time-only values and stated precision); root ADR 0061 §3 (a zone the
  transform config states)

## Context

Memory's event index (Memory ADR 0013) reads a table as events only when its times are integers
(`{ticks}` or `{seconds, nanoseconds}`) on a clock named by a `timestamp_domain` id. A syslog,
PLC or safety-controller export arrives as CSV, and the compiler keeps every CSV cell as text
(root ADR 0042 §1). So INC-C3-0011's protective stop in syslog (row 4182, `2026-09-14 14:32:38`)
cannot be placed on any timeline, and the 32 s conflict with the CMMS downtime log cannot be
shown. These exports state no zone in their bytes. The site declares the zone, and the harness
passes it per source (corpus 2.1.0: `America/New_York` for PLANT-2's syslog and downtime log).

## Decision

1. **An event-log mapping writes a typed table.** The schema is `neptune-deploy.event-log-mapping/1`,
   in `neptune_deploy.eventlogs`, with the preset `syslog_csv`. A headed table that has every
   `requires` column becomes one table named by the mapping (`syslog events`), with one row per
   source row, in order. The header is:
   - each listed column, verbatim, citing its cell. A column the log lacks is `NotCovered` with
     `column_absent`;
   - the time column verbatim, then `<time>.sec` and `<time>.nanosec`, integers read by the
     declared format (`lifecycle.times`, at the precision the text states). They count from
     1970-01-01T00:00:00 on the log's own civil wall clock (root ADR 0061 §2). They equal the wall
     time read as if it were UTC, but they are not POSIX instants: the clock's timescale is
     `Unknown`, and an instant comes only from a derived transform that applies a tz database to
     the declared zone, never from this table. Then `@clock:<time>`, the `TimestampDomain` id they
     count on, as the diagnostics table writes `@clock:stamp`;
   - `@id:<namespace>`, the row's identifier text. For syslog this is `Seq` under `syslog`, which
     the corpus same-event assertion names (`{syslog, "4182"}`).

   Every cell is `stated` under the mapping's transform and cites the source cell; the computed
   cells cite the time or identifier cell they are read from. A blank time is `Unknown` with
   `value_blank`. One that does not read is `Unknown` with `value_unreadable`, and a time of day
   without a date never reads (ADR 0016 §1). Either way the row is kept. Rows that cross midnight
   read the dates they state. Two rows stating one identifier are both kept, with
   `identifier_repeated`. A row stating no identifier is kept, its `@id` is `Unknown`, and it gets
   `identifier_blank`: an assertion cannot name it. `MsgID` is a typed field and repeats freely. A
   short row's missing cells are blank, and the compiler already reports the row
   (`tabular.csv_ragged_rows`). A log a mapping writes is
   that mapping's, and no lifecycle rule reads it again (no `table_unmapped`). It runs inside
   `python -m neptune_deploy map`: `-p` takes event-log presets as well as lifecycle ones.
2. **A caller may declare a civil zone per source, for any mapping** (root ADR 0061 §3).
   - The command-line form is `--source-zone PRESET SOURCE ZONE` (repeatable). The library form
     is `presets(names, {(preset, source): zone})`, or `mapping.with_source_zones({source: zone})`
     on a lifecycle or event-log mapping.
   - `SOURCE` is the source revision's path in the package. `ZONE` is an IANA name or exactly
     `unstated`, checked as a mapping's zone is (ADR 0012 §2).
   - The zones enter that mapping's transform config (`civil_time_zones: {path: zone}`), so they
     change its config hash and transform id: new lineage. Without them the config, and every
     existing id, is unchanged.
   - For the tables of that source, every civil clock the mapping reads has a `civil_time_zone`
     companion with that zone, `stated`, citing the source's table (with a step naming the clock),
     under that transform. An instant (a reading with an offset) still has no zone.
   - Absent the option, the zone is the mapping's own, which every shipped preset leaves
     `unstated`, so it is `Unknown`.
   - Refused before anything is written (`MappingError`, exit 2): a zone for a preset the run does
     not map, for a path the package does not hold, or for a source none of whose tables that
     preset maps (`-p syslog_csv --source-zone syslog_csv neptune.yaml UTC`, or a `cmms_downtime`
     zone on the syslog path). Also refused: a malformed zone, or two zones for one pair. An
     unused zone would move the transform id and look applied while no clock received it.
   - The caller's zone replaces the mapping's own for that source's tables. Document templates do
     not take it yet: no template source needs it.
3. **Versions.** The event-log mapper is `deploy_event_log_map` `0.1.0`. The lifecycle mappers keep
   `0.3.0`: with no source zone their output is byte-identical.

## Alternatives considered

- **A compiler change to type CSV columns.** Root ADR 0042 keeps cells as text. A typed reading is
  a declared interpretation, which is a mapping file's job. Lost.
- **Lifecycle records for log lines.** A syslog line is not a lifecycle declaration (root ADR
  0051), and an `intervention` per log line would stretch the kind further than ADR 0016 §7 does.
  Lost.
- **`MsgID` as the identity.** It names the kind of message (`PSTOP`) and repeats. `Seq` is the
  collector's own number. Lost.
- **A zone column, or a zone in a preset copy.** The bytes state none. A preset copy per site
  multiplies files, and a guessed zone breaks ADR 0016 §1. The site's declaration belongs to the
  caller, recorded in the transform. Lost.
- **Ignoring a zone that names nothing in the run.** That is a silent no-op. Lost: it is a usage
  error.

## Consequences

- Platform adds `syslog_csv` (and `cmms_downtime`) to `deploy.json` with `--source-zone` per source.
  Memory declares `syslog events` in its `memory.events` config: `at` = `{seconds:
  "Timestamp.sec", nanoseconds: "Timestamp.nanosec"}`, `clock` = `{column: "@clock:Timestamp"}`.
- The fixture is corpus 2.1.0's syslog CSV (Platform's corpus-event-tables branch), committed and
  ingested into `tests/fixtures/demo_corpus/syslog_package/`. It is pinned by content id until
  2.1.0's lock is on main, which then binds it.
- Revisit if a log states a zone or an offset per row, or if a template source needs a caller's
  zone.
