# 0005 — D1 gate: bounded mapping, container-index tables, explicit coverage and stable clocks

- Status: Accepted
- Date: 2026-10-02
- Issue: MVL-116
- Amends: ADR 0002 §4, §5 and §6; ADR 0003 §3 and §7. Review: [`docs/reviews/d1-gate.md`](../reviews/d1-gate.md)

## Context

The D1 gate put a safety lead's questions to both archetypes (ADR 0004) and attacked the mapper
(ADR 0002) and the document templates (ADR 0003) with hostile and boundary inputs. It found eight
defects. Two were quadratic in a document's size, two let a small cell grow into megabytes of
records and findings, one let an unread list field look like a list stated empty, one moved every
record's clock id when a single time cell was damaged, and two concerned findings that were
unbounded or reported tables that hold no exported rows. The coordinator added three items: skip the
compiler's container-index tables by a named list, cap `column_unmapped`, and confirm the
`deploy_lifecycle` probe still declines now that MVL-200 loads plugins. This ADR records how each is
decided, so that code can be checked against it.

## Decision

1. **Container-index tables are skipped by a named list.** `mapper.CONTAINER_INDEX_TABLES` names the
   compiler tables that index their container instead of holding rows an export states. Each entry
   gives the adapter that emits it (from the table's transform) and either the last locator step of
   the table's citation or the names the table states:
   - `tabular` with step `tabular:xlsx_workbook`: an XLSX workbook's sheet list, sheet states and
     date system (root ADR 0059 §4, MVL-201);
   - `tabular` with steps `tabular:schema` and `tabular:row_groups`: a Parquet file's footer, the
     types and chunk layout of its data table (root ADR 0042);
   - `rosbag2` with names `rosbag2_bagfile_information`, `topics_with_message_count`, `files` and
     `relative_file_paths`: a bag's `metadata.yaml`, which the adapter already reads into the bag's
     run and streams.
   No mapping applies to these tables, and no finding reports them as unmapped. The base package
   keeps them. A sheet's `formulas` table and an MCAP file's metadata records are content, not an
   index, so they stay candidates, and so does a Parquet footer's `tabular:key_value` metadata. Mapping files do not change. Adding an entry to the list changes
   the mapper's output, so it is a decision recorded here or in a later ADR.
2. **Every finding has a bounded size.** `column_unmapped` (from both the mapper and the templates)
   names the first `NAMED` (10) columns and gives `column_count` for all of them. `list_id_repeated` is
   one finding per cell, as ADR 0002 §6 says. Its subject is the cell. Its `related` lists the
   statement that was kept and then the repeats, `NAMED` in all, and its `count` is the number of
   repeats. Before this, each repeat was its own finding. `list_part_empty` is also one finding per
   cell, with `count` the number of empty parts.
3. **A list cell is read into at most `MAX_LIST_PARTS` (1,000) parts.** Past that, the record keeps the
   first 1,000 parts and a `list_truncated` finding (warning, category `limit`) names the record, the
   field and the cell, with `limit`. Its `related` cites the cell's text from the first part not
   read to the end, so what was not read is cited, not counted. The cell is scanned part by part and
   the scan stops there. This follows the compiler's precedent (the rosbag2
   adapter's `MAX_ROWS` and the XLSX limits): keep what was read, and say so. A thousand ids in one CMMS
   cell is not an export; a hostile cell is, and each part costs about a hundred times its bytes in
   cited output. A list a document states one block per item (a section's list items, a table's rows)
   is not split, so it is not capped: each part is already a compiler record.
4. **Reading a document is linear in its blocks and tables.** The lines a template took are grouped
   by block once. The block that holds a table's header is found through a per-page index of block
   spans. The index relies on the compiler's invariant that a page's blocks are disjoint spans (a
   page's text is its blocks with an LF between each, root ADR 0038), so the block with the greatest
   start at or before a span is the only one that can hold it. Blocks that share that start are
   tried in reading order. On input that breaks the invariant, a
   block may go unfound and is then reported in `text_unread`. That is never a wrong value.
5. **What a declaration does not read is named, lists and parts included.** A scalar a rule does not
   read is `NotCovered` in the record. A list it does not read is `()`, and the model cannot mark a
   list as not covered (MVL-202). Each rule that makes records therefore has one `fields_not_covered`
   finding (info, `missing`) per table. The finding names the kind's unread fields and, for parts it
   does read, their unread fields as `field/part_field`, with the records it made (amends ADR 0002 §6).
   A document's `template_matched` `not_covered` lists part fields the same way (amends ADR 0003 §3).
6. **Deploy compares no records.** When two sources disagree (a work order dated before the incident
   it repairs), both values are kept as stated, each citing its own cell or span on its own clock.
   No finding judges the disagreement. Comparing records is a reader's job (package non-negotiable 2).
   The gate's reader orders two times only in these cases: they are on one clock; they are two readings
   of one record; or they lie further apart than civil offsets can explain (26 hours plus the coarser
   resolution). Under that rule a same-day order across two sources whose zones are `unstated` is not
   stated. MVL-202 (a declared civil zone in the model) is what makes it stated.
7. **A table's clock cites its column's first cell.** Under ADR 0002 §5, a `TimestampDomain` cited the
   first cell read on it. Damaging that one cell made the next cell first, which moved the clock's id
   and so every other record's timestamps. A table clock now cites the column's cell in the first row
   that has one, whatever that cell holds (amends ADR 0002 §5). One column may be read as several
   clocks (date-times and dates, or wall-clock times and instants). So, as the compiler's MCAP adapter
   does with its time-field step, the clock's citation ends with a step of the mapper's own,
   `<mapper id>:clock`, with fields `instant`, `resolution` and (for a wall clock) `zone`. Each clock
   therefore has its own id. A document's clocks are scoped to the document and cite the first value
   read, with the same step. Two different clocks under one id are refused as an internal error,
   never merged.
8. **`deploy_lifecycle` still declines every source.** Its probe returns confidence 0 with reason
   `deploy_lifecycle.no_reader` for every head, and ADR 0002 §2 keeps the mapper outside the ABI.
   MVL-200's loader (PR #83) admits the adapter and probes every source with it. Its own test,
   `test_neptune_deploy_installed_its_adapter_is_probed_on_every_source`, shows that every record is
   the record a job without plugins makes. The only difference is that an unread file's
   `neptune.probe.unsupported` finding lists Deploy's decline. Because of that difference, once MVL-200
   merges, the archetype generator ingests with `--no-plugins` so that the committed base packages are
   the compiler's alone. The harness's drift check over this corpus (MVL-181) ingests the same way.
9. **The mapper versions stay `0.1.0` through the `d1-gate` tag.** D1's outputs were never released,
   and the golden files in this PR record the change. From the tag on, any change to output bumps
   `MAPPER_VERSION` or `DOCUMENT_MAPPER_VERSION`, which makes new lineage beside the old (root
   non-negotiable 6).

## Alternatives considered

- **Report container-index tables as `table_unmapped` (as before).** Every bag added four info
  findings, and every workbook would add one, about tables no lifecycle mapping could mean. That noise
  hides the findings that matter. Lost.
- **Recognise index tables by shape (a two-column `property | value` header).** A register can have
  that shape too. Lost: the list names producers, and producers are not guessed.
- **Drop a list cell longer than the limit entirely (all or nothing).** Cleaner, but it discards what
  was read, and the compiler's limits keep the first part. Lost. A finding marks the list as partial.
- **Bound reading time by a wall-clock budget.** Not deterministic. Lost.
- **A finding when two sources' dates contradict.** Deciding that two records describe one event, and
  which date wins, is inference across sources (non-negotiable 2). Lost: it belongs to a later layer.
- **One clock per mapping and zone, shared across columns and tables.** It would let a reader order
  two columns without the one-record rule. But a fleet export spans sites in different zones under one
  `unstated` declaration, so a shared clock would be a false statement. Lost.

## Consequences

- Both archetypes' golden lifecycle packages change. The bag metadata's four `table_unmapped` findings
  and their run transform are gone, and `fields_not_covered` findings are added (one per rule and
  table). Each clock's citation gains its reading step, so clock ids, and the timestamps that name
  them, move. Finding ids move with their content.
- An XLSX date column cannot be read as a time yet. The reader keeps a date as its serial number (root
  ADR 0059 §3), and reading the serial needs the workbook's date system, which sits in the skipped
  index table and so would need a join. Such a column is `value_unreadable` today. Revisit when an
  XLSX export is in a deployment's corpus.
- The same robot carries one identifier per source (`register.robot:AMR-07`, `cmms.asset:AMR-07`,
  `fleet.amr:AMR-07`, `servicenow.ci:AMR-07`). Joining them is identity resolution (MVL-35), never
  Deploy's.
- Revisit §3 if a real export needs more than 1,000 parts in one cell, §4 if the compiler's blocks may
  overlap, and §6 when MVL-202 lands.
