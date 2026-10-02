# D1 gate: stress test of lifecycle records on both archetypes

- Date: 2026-10-02 · Issue: MVL-116 · Reviewed: Deploy ADRs 0001 to 0004, `neptune_deploy.lifecycle`
  (mapper, mapping files, document templates, presets), the `deploy_lifecycle` adapter, and both
  archetypes (`tests/fixtures/archetypes/`).
- Method: every scenario was run, not walked through on paper. Each is a test in
  `tests/test_deploy_d1_gate.py`. The tests read the committed base packages and run only the mapper
  (members never ingest). The scale and hostile figures come from
  `tests/fixtures/archetypes/stress_lifecycle_mapper.py` on Linux 7.0, x86_64, Python 3.14, and its
  output is recorded below. The Platform harness ran twice at `f8f1608` (the branch point): the
  default corpus, and `--corpus packages/neptune-deploy/tests/fixtures/archetypes/sources`.
- Outcome: lifecycle records answer a safety lead's questions on both archetypes. Each answer is a
  cited `stated` value or an explicit `Unknown`/`NotCovered` that a finding explains. That holds once
  the eight defects below were fixed ([ADR 0005](../adr/0005-d1-gate-bounds-container-index-tables-and-explicit-coverage.md)).
  **Verdict: pass.** D2 may start once this is merged and `main` is tagged `d1-gate`. No D2 issue
  starts before this gate is Done.
- Harness: green at `f8f1608`. `contracts ok | compiler: real ok | ledger: stub ok | memory: stub ok
  | context: stub ok`, for both corpora. Over the archetype corpus the compiler stage reproduced both
  committed base packages byte for byte (manifest `sha256:e828a9b2…` for the fleet,
  `sha256:3ec26b9a…` for the cell), so the mapper's input has not drifted from the compiler's output.

## The safety lead's questions

The test's reader orders two times only when the records license it: they are on one clock, they are
two readings of one record, or they are more than 26 hours of civil offsets (plus resolution) apart
(ADR 0005 §6). Deploy itself orders nothing.

| Question | Archetype | Answer from the records | Test |
|---|---|---|---|
| Which configuration was authorised at the time of the incident? | fleet, INC-0007 | `NotCovered`. The envelope ENV-S007-04 (AMR-07 in PICK-A, valid from 2026-03-09 to 2026-09-09, both before and after the incident) states no configuration, and its rule's `fields_not_covered` says so. The incident report states none either (`template_matched`, `not_covered: [configuration]`). The records state the firmware around it: every AMR-07 work order before the incident left 4.2.0 (each value cites its `Firmware After` cell), and CHG0050023 moved 4.2.0 to 4.3.1 after it. | `test_q1_fleet_…` |
| | cell, INC-C3-0004 | `NotCovered`: nothing the cell declares states an authorisation, so the package has no `authorisation_envelope`. The commissioning baseline states `cfg-c3-1.4`, and CHG0030012 (5.4.2 to 5.6.0) falls between commissioning and the near miss. The near-miss ticket states an instant (-04:00) and names no machine. Its rule says `machines` is not read, so the empty list does not mean "none". The inspection after it (an `INSP` work order) has no record and is named by `row_unmatched`. | `test_q1_cell_…` |
| What changed since commissioning? | cell | Both change records (CHG0030012, CHG0030013), the joint drive and both finger-set swaps, and three later calibrations (CAL-ARM3A-0415, -0623, -0818). Each is a cited record after the baseline's `commissioned`. The finger change is stated twice, by the CMMS and by the SOP's work record: two records in two namespaces, never merged. | `test_q2_cell_…` |
| | fleet | `NotCovered`: no commissioning record exists in the fleet's evidence. | `test_q2_fleet_…` |
| Was the requalification complete before return to service? | fleet | RQ-S007-0007: yes. Three tests are stated, the result is PASS, and `performed` precedes the return decision within the one record. RQ-S012-0003: the return time is `Unknown`, citing the blank `Decided On` cell, with a `value_blank` finding. The third test pair is blank (`item_blank`). Whether CHG0050023 (2026-04-14 18:00) preceded RQ-S007-0007 (2026-04-15 09:30) is **not stated**: two sources, `unstated` zones, 15.5 hours apart (MVL-202). | `test_q3_fleet_…` |
| | cell | RQ-2026-005: yes, within the record. RQ-2026-006: the return time is `Unknown` and cited, with the decision "Returned to service with speed restriction". | `test_q3_cell_…` |

## Attacks

| Attack | What happened | Verdict |
|---|---|---|
| CMMS and incident report dates contradict (WO-26-0402, the repair of INC-0007, dated 2026-03-30) | Both values are kept as stated, each citing its own cell or span on its own clock. The reader sees the repair precede its incident by three days. Deploy's findings are exactly the clean run's, because it judges nothing. | holds (ADR 0005 §6) |
| A maintenance event with no configuration | The SOP's event: `NotCovered`, named by `template_matched`. A blank `Firmware After` cell: the event is kept and its configuration is `Unknown`, citing that cell. The column gone from the export: every event's configuration is `NotCovered`, with `column_absent` and `column_unmapped`. | holds |
| An SOP revision without a change record (Revision A changed to B) | The SOP's event is still made. The revision line, which the template does not read, is in `text_unread` with its own span. No change record is invented and no gap is flagged: the absence is a query over records, not a fact Deploy states. | holds |
| Partial success (an unreadable date, a blank machine, a lossy number in three rows) | Every other lifecycle record is byte-identical to the clean run's. Before the fix, a damaged first time cell moved every record's clock id (D8). | holds, after D8 |

## Properties

| Property | Test | Verdict |
|---|---|---|
| Every value is `stated` and cites a `RowCell`, JSON pointer or `Span` in a ledgered source. Every `Unknown` cites its cell. | `test_every_value_is_stated_…` | holds |
| Every field a declaration does not read is `NotCovered` or named by a finding, lists included | `test_every_unread_field_…` | holds, after D7 |
| Lineage: the new package cites the base (`base_package`, upstream transforms carried whole, same source ledger) and copies none of its tables or blocks | `test_the_lifecycle_package_cites_the_base_…` | holds |
| The mapper re-parses nothing: no file is opened while it maps | `test_the_mapper_opens_no_file_while_it_maps` | holds |
| Determinism: both archetypes are byte-identical under `PYTHONHASHSEED` 0, 1, 4242 and random | `test_both_archetypes_map_byte_identically_…` | holds |
| `deploy_lifecycle` declines every source of the corpus (confidence 0, `no_reader`) | `test_the_lifecycle_adapter_declines_…` | holds; see ADR 0005 §8 |
| Container-index tables are neither mapped nor reported | `test_container_index_tables_…` | holds, after D6 |

## Measurements

`python tests/fixtures/archetypes/stress_lifecycle_mapper.py 10000 100000` maps grown copies of
the fleet's base package in memory. It runs each case in its own process, so each RSS figure is that
case's own peak. "map" is Deploy's mapper; "write" is the compiler's `package_files` (canonical JSON
and the receipt).

```
CMMS export, 10,000 work orders              map   1.42 s  write   9.61 s      362 MiB RSS    46.93 MiB out   6945 findings
CMMS export, 100,000 work orders             map  14.87 s  write  96.65 s     2891 MiB RSS   469.74 MiB out  69253 findings
incident report, 4,000 labelled paragraphs   map   0.04 s  write   0.01 s       88 MiB RSS     0.06 MiB out      7 findings
incident report, 32,000 labelled paragraphs  map   0.37 s  write   0.01 s      138 MiB RSS     0.06 MiB out      7 findings
register cell, 1 MiB of ';'                  map   0.11 s  write   0.01 s       84 MiB RSS     0.07 MiB out      7 findings
register cell, 1 MiB of one repeated id      map   0.01 s  write   0.01 s       85 MiB RSS     0.08 MiB out      8 findings
register cell, 1 MiB of one-letter missions  map   0.01 s  write   0.09 s       89 MiB RSS     0.43 MiB out      7 findings
```

- Everything is linear. The mapper costs 0.15 ms per work order. Writing the package costs about
  1 ms per row, which is 87% of the time, and it is the compiler's code. A CMMS export of 100,000
  work orders (one CMMS for a fleet of a few hundred robots over several years) maps in 15 s, writes
  in 97 s and peaks at 2.9 GB (R3).
- The 0.7 findings per row are `list_cell_blank`, one per blank `Related` cell. ADR 0002 §6 keeps
  these per cell and uncapped, so that every emptied list is traceable.
- Before the fixes: 16,000 paragraphs took 6.9 s (8,000 took 1.8 s, so quadratic); 8,000 tables in
  one document took 16.8 s; an 80 KB cell of one repeated id made 10,000 findings (13.8 MB, 13.6 s);
  and a 20 KB cell of one-letter missions made 10,000 statements (3.7 MB). Each 1 MiB hostile cell
  now costs at most 1,000 parts and one finding of bounded size.

## Findings

### Defects found and fixed here (ADR 0005)

- **D1. A document's unread text was quadratic in its blocks.** Each block was matched against every
  line a label took. Lines are now grouped by block once.
- **D2. Finding a table's block was quadratic in tables times blocks.** It is now a per-page index of
  block spans: one candidate per lookup.
- **D3. Repeated ids were one finding each.** A cell that repeats one id 10,000 times made 10,000
  findings. They are now one finding per cell, as ADR 0002 §6 says, citing the first ten repeats.
- **D4. A list cell had no bound.** One-letter parts cost about 180 times their bytes in cited
  statements. A cell is now read into at most 1,000 parts, and `list_truncated` cites the text that
  was not read.
- **D5. `column_unmapped` listed every column** (coordinator scope item). It now lists ten and counts
  them all.
- **D6. Container-index tables were reported `table_unmapped`** (coordinator scope item): four per
  rosbag2 bag, and an XLSX workbook's sheet index. They are now skipped by a named list.
- **D7. An unread list field read as "none".** The Jira preset maps no `machines`, so every incident
  had `machines: ()` and nothing said why. `fields_not_covered` now names each rule's unread fields
  and its parts' fields, as `template_matched` does for a document.
- **D8. One damaged time moved every record's clock id.** A table clock cited the first cell read, so
  damaging that cell re-cited the clock. It now cites the column's first cell, whatever that cell
  holds, followed by a step naming how the clock reads. Each reading of a column (a date, a
  date-time, an instant) is therefore its own clock.

The gate's own changes were reviewed the same way (`/code-review` at high effort). The review
confirmed one defect in the first D8 fix: every clock read from one column shared the first cell's
id, so a date-only cell among date-times collapsed two clocks into one of the wrong resolution. The
clock step above closes it, and `test_one_column_read_at_two_resolutions_is_two_clocks` covers it. A
duplicate clock id is now refused rather than silently merged. The review also closed six smaller
gaps: `list_truncated` cited no text, `list_id_repeated` did not cite the statement it kept, the
Parquet footer's index tables were missing from the list, blocks sharing a start were not handled,
the reader ordered an instant against a wall-clock reading inside one record, and one test assertion
was a tautology.

### Decisions

- Deploy compares no records. A contradiction is visible through two cited records and is judged
  by a later layer (ADR 0005 §6).
- `deploy_lifecycle` keeps declining under MVL-200's loader. Once MVL-200 merges, the archetype base
  packages are ingested with `--no-plugins` (ADR 0005 §8).
- The mapper versions stay `0.1.0` through `d1-gate`, and every output change after the tag bumps
  them (ADR 0005 §9).

### Compiler gaps (listed, not worked around)

| Gap | Effect here | Issue |
|---|---|---|
| No GeoJSON adapter | The fleet's zone maps are read as plain text: `document_unmatched` (info) on each | MVL-31 |
| No URDF adapter on `main` | The URDFs are read as plain text: `document_unmatched` on each | MVL-24, PR #38 |
| An MCAP-storage rosbag2 bag becomes two runs (its metadata and its storage file) | The cell's base package has two runs for one bag | none yet (coordinator to file) |
| MCAP and bag payloads are not decoded | `mcap.payload_not_decoded` (21 info findings in the fleet) | none yet (coordinator to file) |
| The compiler loads no plugin entry points | Deploy runs by library call. Its adapter's decline is checked here, and end to end in PR #83 | MVL-200, PR #83 |
| XLSX `workbook` index table | Skipped by name (D6). An XLSX date is a serial, which the mapper cannot read as a time without the workbook's date system | MVL-201, PR #84 |
| No civil zone on a timestamp, and no `Unknown` list | A same-day order across sources is not stated, the zone lives only in transform config, and a blank list is `()` plus a finding | MVL-202, PR #88 |
| One robot, one id per source | `register.robot:AMR-07`, `cmms.asset:AMR-07`, `fleet.amr:AMR-07` and `servicenow.ci:AMR-07` are joined only by a reader's declaration | MVL-35 |

### Accepted limitations

- **L1.** A list cell keeps its first 1,000 parts. Past that the record's list is partial, and
  `list_truncated` says so.
- **L2.** On input that breaks the compiler's disjoint-block invariant, a table's block may be listed
  as unread rather than found. That is never a wrong value.
- **L3.** Same-day questions across sources ("was the change before the requalification?") are not
  stated until MVL-202 lands.

### Open risks

- **R1.** The base packages are committed ingest output, so the Deploy job does not see adapter
  drift. The harness reproduced them at this gate, and MVL-181 makes that a standing check.
- **R2.** Once MVL-200 merges, a default `neptune ingest` lists Deploy's decline in each
  `neptune.probe.unsupported` finding. The generator and the harness drift check must ingest this
  corpus the same way (`--no-plugins`), or the drift check reports Deploy's presence as drift.
- **R3. Package size in memory.** `package_files` builds the whole package in memory, at about 29 KB
  of resident memory per mapped row (2.9 GB for 100,000 work orders), and writing is about 1 ms per
  row. A multi-million-row export needs streaming package writes from the compiler's store. Coordinator
  to file it against the compiler.
