# 0023 — The M1 gate: the model freezes, grows only by addition, and fixes four gaps

- Status: Accepted
- Date: 2026-10-01
- Issue: MVL-56
- Amends: ADR 0017 §7 (schema evolution), ADR 0012 (civil date-times), ADR 0016 (several-field
  values), ADR 0011 (`INHERITED` at depth), ADR 0022 §1 (package layout)

## Context

The M1 gate (`docs/reviews/m1-stress-test.md`) walked the scenarios of MVL-56 against the model
and its four worked examples. The contract held for every scenario, but the review found four
gaps. Each was cheap to close now and would be expensive after M2 builds on the model:

1. **Adding a field had no lossless migration.** ADR 0017 §7 asks every migration to be lossless,
   and a field added later has no value in older records. A reader of the new version would then
   refuse every older package, which breaks the promise that old packages still load.
2. **Civil date-times had no rule for becoming ticks.** EXIF `DateTimeOriginal`, register dates and
   manifest times are civil fields, some with a UTC offset and many without. The worked mobile
   robot example read its photo's time ten hours off, because nothing said what the ticks count.
3. **A value read from several fields had no citation rule.** A run's end computed from a start and
   a duration, or a position from a latitude and a longitude cell, can hold one citation.
4. **`INHERITED` nested inside a value**, as in a position's units inside a site's location, could
   be read as the enclosing state's provenance or as the record's.

It also found that derived records, which non-negotiable 8 keeps apart from evidence, had no place
in a package.

## Decision

1. **Schema version 1, frozen fields, growth by addition** (amends ADR 0017 §7).
   - `SCHEMA_VERSION` becomes 1. Version 0 was a draft and was never persisted; readers refuse it.
   - From version 1 on, a record kind's fields, their types and their meanings never change. The
     model grows only by addition, each step an ADR and a version bump:
     - a new record kind, including a companion kind that names the record it adds facts to (the
       extension rule of ADR 0019 §4, now for every kind);
     - a new enum member;
     - a new core locator step.
   - Every addition leaves older records valid as they are. A reader therefore reads every version
     from 1 to its own, and the migration is the identity. Older packages always load.
   - The package's documents (manifest, receipt, envelope) are not records. One may gain a field in
     a version bump, for example the receipt's bindings with MVL-38. Its reader then reads each
     version by that version's shape, and the package reader recomputes a receipt at the version
     it was written with.
   - Anything that is not an addition is a new kind, and sources are re-ingested by new adapter
     versions beside the old lineage. A package format that breaks this is a new major format,
     decided by ADR, not a migration.
2. **Civil date-times become ticks by one rule** (extends ADR 0012).
   - A date-time with a stated UTC offset or `Z` names an exact instant. Its ticks count POSIX
     seconds (86,400 per day, no leap seconds) from 1970-01-01T00:00:00Z. The domain's epoch is
     `unix` and its timescale `posix`. The stated offset stays in the cited bytes.
   - A date-time with no zone counts the same way from 1970-01-01T00:00:00 of its own civil
     clock. The epoch is `unix` on that clock, and the timescale is `Unknown` because the zone is
     unstated. Nothing assumes UTC or the site's zone.
   - A date alone counts days from 1970-01-01 (resolution 86,400 s), under the same two cases.
   - The resolution is the finest field the text states: a second, or a fraction of one.
3. **A value read from several fields cites the smallest part that holds them all** (amends ADR
   0016): the row for a latitude and a longitude cell, and the metadata object for a start plus a
   duration. The fields themselves stay citable through that part.
4. **`INHERITED` always means the record-level provenance**, however deeply the state is nested
   (clarifies ADR 0011 §2). An adapter that wants a nested state to cite its enclosing value's
   evidence cites it explicitly.
5. **Derived records get their own place in a package** (amends ADR 0022 §1). `derived/` is
   reserved for them, apart from `records/`. The reader refuses it until the derived layer's
   schema lands (M7), so a package can never mix interpretation into its evidence tables.

## Alternatives considered

- **Keep ADR 0017 §7 as written and fill added fields with a state.** No state means "this lineage
  did not look": `Unknown` says the source was looked at, and `NotCovered` says it cannot say.
  Inventing a seventh state for migrations would put a tooling concept into evidence.
- **Allow optional fields.** Every reader would have to handle both shapes of every kind forever,
  and a missing key would come back as a meaning.
- **Store civil date-times as text.** Nothing could order or align them, and every consumer would
  parse them again.
- **Convert every civil time to UTC with the site's zone.** This is the silent assumption ADR 0005
  forbids. A zone is evidence only when the source states it.
- **Keep a stated UTC offset in the domain.** It needs a field that `TimestampDomain` does not have
  and cannot gain (§1). The offset stays in the bytes, and a companion kind can carry it if
  consumers need it.
- **Leave derived storage to M7.** Cheap now, but readers would accept any directory until then,
  and interpretation could leak into a package unnoticed.

## Consequences

- Old packages stay readable by every later reader. A companion kind costs a join, and kinds can
  accumulate over the years. A major format revision, with re-ingest from the immutable sources,
  is the way out.
- Every golden file changes once, for the version bump alone. Ids do not change (ADR 0017 §7), and
  the review checked that every id is identical.
- Adapters reading civil times and several-field values follow one rule each
  (`adapter-contract.md`).
- Revisit if a companion kind is needed so often for one kind that a major revision is cheaper, or
  if consumers need declared UTC offsets.
