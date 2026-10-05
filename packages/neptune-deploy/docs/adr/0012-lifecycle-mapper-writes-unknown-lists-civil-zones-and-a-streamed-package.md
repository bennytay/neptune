# 0012 — The lifecycle mapper writes Unknown lists, civil time zones and a streamed package

- Status: Accepted
- Date: 2026-10-05
- Issue: MVL-113 (follow-up)
- Amends: ADR 0002 §4, §5 and §6 (blank list cells, the declared zone, the package write); ADR 0004
  (the generator ingests with `--no-plugins`); ADR 0005 §5 (an unread list) and §9 (mapper versions).
  Uses root ADR 0061 (declared civil zones, list states) and root ADR 0065 (streaming package write).

## Context

The mapper (ADR 0002) worked around three gaps in the compiler that have since closed:

1. The model had no `Unknown` list, so a blank list cell became `()` plus a `list_cell_blank`
   finding, and a list no rule read was `()` too (ADR 0005 §5 named it in `fields_not_covered`
   because the record could not say it). `()` says "the declaration states none", which a blank
   never does (non-negotiable 3).
2. The model had no place for a clock's declared civil zone, so the mapping's zone lived only in
   the transform config and in the clock's citation.
3. The compiler could only write a package from a list in memory. The D1 gate measured 100,000
   work orders at 2.9 GB, and root ADR 0065 measured 746 MiB with `write_package_stream` because the
   mapper still held its records as lists.

The compiler now loads plugins (MVL-200), so the archetype generator must say how it ingests
(ADR 0005 §8, risk R2).

## Decision

1. **A list is a state.** The mapper writes the list states of root ADR 0061 §4:
   - `Known(items)` inheriting the record's provenance (the version 4 bare array) when a cell
     states items. Findings about malformed parts are unchanged: `list_part_empty`,
     `list_id_repeated`, `list_truncated`, `value_unreadable`, `item_blank`.
   - `Unknown(provenance of the blank cell)` when the cell is blank (missing, short row, the
     compiler's `Unknown`, or text that is only whitespace) or states no item (only delimiters,
     which also keeps the `list_part_empty` finding). No finding: the state is the statement, and it cites the cell. A cell
     whose text is not readable as text is `Unknown` with `value_unreadable`, as a scalar is.
   - `NotCovered` when no rule reads the list (what ADR 0005 §5 could only name in
     `fields_not_covered`, which still names it), when the column or section is absent from the
     table or document, or when the compiler's cell is `NotCovered`.
   - `Known(())` citing the cell only when the compiler's cell is `KnownAbsent` (the source
     states none). Text never gives `Known(())`: no text states "none". A list of parts (`tests`, `hazards`) whose every part is blank is `Unknown`
     citing the row, or `NotCovered` when every column its parts read is absent. A document
     section that shows text but no list item is `Unknown` citing its heading, with
     `value_unreadable`: prose is not a list stated empty.
   - A list read from several cells, one blank and another stating items: `Known(items)` plus
     `list_cell_blank`, now meaning only this case (the list lacks what the blank cell would have
     stated). Whole-list `Ambiguous` and `KnownAbsent` are not written (root ADR 0061 §4).
2. **A civil clock has a companion `civil_time_zone`.** Each `TimestampDomain` the mapper writes for
   a reading without an offset gets one record:
   - `Known(zone)` where the mapping declares an IANA name, `Unknown` where it declares `unstated`;
   - provenance: the clock's own citation (the column's first cell and the reading step), the
     mapper's transform (whose config holds the mapping and so the declared zone), `stated`;
   - an instant (a reading with an offset) gets none: its offset states the instant, and a civil
     zone would claim a civil clock it does not have.
   - Nothing converts. The domain's ticks still count the civil clock, never UTC. The transform
     config still records the declared zone, as it records the whole mapping.
   - A zone must be spelled as an IANA name (root ADR 0061 §1) or be `unstated`; a mapping file
     that declares anything else (`+01:00`, `W. Europe Standard Time`) is refused at load with a
     `MappingError`, because the operator wrote it and a silent `Unknown` would hide the typo.
     Words that pass the IANA syntax but state no zone (`none`, `N/A`, `local`, `Unstated`) are
     refused too: only the exact `unstated` means `Unknown`.
   - The zone stays in the domain's reading step (its citation), as an identity discriminator
     only: two rules reading one column under two zones are two clocks. This keeps every
     `TimestampDomain` byte and id as it was; the authoritative zone is the companion.
3. **The package is written as the records are mapped.** `map_package` passes `iter_records` to
   `write_package_stream`, with spill under the directory `out` is made in (or a `scratch` given):
   - A mapping's table is mapped one row at a time and no record is kept. What a later row needs of
     an earlier one is held compactly: the first holder of each identifier (for
     `identifier_repeated`), the grouped findings and the clocks. The base package is read whole
     (the compiler offers no streaming reader), so memory still grows with the input's rows, not
     with the output's.
   - `plan_tables` builds every mapper, and so every transform, before any row is read; the base's
     lineage to carry is therefore known before the first record is yielded. Documents are mapped
     first and held as a list (a document's size bounds it, D1 §Measurements).
   - A run that names no file, or one twice, is refused before `out` is created
     (`check_declared`); any other error ends the write, which leaves `out` empty.
   - `map_files` and `map_records` stay (lists for a package that fits in memory) and give the same
     bytes.
4. **The archetype generator ingests with `--no-plugins`.** The base packages hold the compiler's
   own adapters only; with Deploy's plugin loaded they would hold its decline in every source's
   probe findings (ADR 0005 §8, R2).
5. **Mapper versions are `0.2.0`** (`deploy_lifecycle_map`, `deploy_document_map`), as ADR 0005 §9
   requires after the tag: output bytes changed (transform ids, new records, list states).

## Alternatives considered

- **Keep `list_cell_blank` beside `Unknown`.** The state already says it and cites the cell, so the
  finding repeated it once per blank cell (0.7 findings per row at 100,000 work orders).
- **`Known(())` for a list no rule reads.** That is the unread-list workaround itself: it states
  none. `NotCovered` is what the model offers.
- **Drop the zone from the domain's reading step.** Every domain id and every timestamp's domain
  reference would change, for no gain: the step discriminates clocks and is not read as a zone.
- **Unknown on an invalid zone with a finding** (root ADR 0061 §1's rule for a source's text). A
  zone in a mapping file is the operator's declaration, not hostile input, and fails once at load.
- **A civil zone for an instant.** It would say the offset-bearing text is also a civil clock.
- **Hold the mapped records and write at the end.** That was 746 MiB at 100,000 rows (root ADR 0065).
- **Stream the documents too.** A template's records are bounded by its documents; the measured
  cost (32,000 paragraphs, 138 MiB) does not justify a second planning pass.

## Consequences

- A reader of a mapped package finds every list as an array (items stated) or a state, and joins
  `civil_time_zone` to a domain to learn the declared zone. A package that holds any of these is
  written at schema version 6 and a version 4 or 5 reader refuses it by version.
- The lifecycle goldens change (every record's transform id, new `civil_time_zone` records, list
  states, fewer findings), as the PR explains. The committed base packages
  (`archetypes/packages/*`) change too, but not by the mapper: `--no-plugins` drops the
  `neptune.plugins` transform, and the fleet's `neptune.validate` transform and its
  `source_incomplete` finding (which already existed) have new ids because the compiler's
  `dangling_reference` rule went from version 2 to 3.
- Memory of a run is the base package's records plus one row plus the findings: the D1 gate's
  100,000-work-order case is measured again in `docs/reviews/d1-gate.md`.
- Revisit if a multi-million-row export needs the base read as a stream (a compiler reader), or if
  a source declares zones per row (a per-row domain and companion, root ADR 0061 §3).
