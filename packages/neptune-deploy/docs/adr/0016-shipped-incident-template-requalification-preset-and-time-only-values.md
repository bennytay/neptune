# 0016 — Shipped incident template, requalification preset, INSP work orders, and time-only values

- Status: Accepted
- Date: 2026-10-06
- Issue: MVL-191
- Amends: ADR 0004 §5 (the inspection work order is now read). Builds on ADR 0002 §3 to §6, ADR 0003
  §2 and §4, ADR 0012 §2, root ADR 0051 and root ADR 0061.

## Context

Demo v1 maps the acceptance corpus (MVL-181, Platform ADR 0007) with the mapper, using only what Deploy
ships. Run over the corpus package at b1d2f72, three gaps showed:

- **INC-C3-0011.pdf**: all five timeline times were `value_unreadable`. The only incident template was a
  test fixture (`incident.amr_report`, declared format `%Y-%m-%d %H:%M`). The report's HMI rows are
  written to the second (`2026-09-14 14:32:38`), so they did not match a format that ends at minutes.
  The rows are not time-only: each one states its own date.
- **Both requalification CSVs**: `table_unmapped`. The requalification mappings were test fixtures too
  (`archetypes/declared/`).
- **WO-26-0709**: `row_unmatched`. `cmms_generic` read only `PM` and `CM` rows, and this work order is
  `INSP`.

HMI alarm logs often print a time of day with no date. When they do, guessing the date is the easiest
mistake a template can make, so the rule is fixed here before any template needs it.

## Decision

1. **A time-only value is never completed with a guessed date or zone.** This applies to every
   mapping file and template:
   - The format grammar (`lifecycle.times`) requires `%Y`, `%m` and `%d`, so a time-only format is
     refused at load. Text that holds only a time of day, under a dated format, is `Unknown` with
     `value_unreadable` citing its cell. Nothing reads a date from another field, the run, or the
     file name.
   - When a source needs its time-of-day rows placed, the template declares the clock for those
     rows: a clock the template names, with a civil zone only if the document states one. The date
     comes only from a date the same document states, such as the report's incident date field. It
     is cited as a second span, and the combination is recorded as a provenanced derivation in the
     transform config. Nothing in the corpus needs this yet, so it is not built. It will be built to
     this rule when a source needs it.
   - If the document states no date, the value is a time of day with an `Unknown` date, and it is
     never placed on an absolute timeline.
   - Rows that cross midnight are read as stated. A date change between two time-only rows is never
     inferred: each row is `Unknown` with its own finding.
   - Nothing is converted to UTC, ever (ADR 0002 §5).
2. **Shipped document templates.** These live in `lifecycle/presets/templates/`, are listed in
   `TEMPLATE_PRESETS`, are loaded with `template_preset(name)`, and are passed on the command line as
   `-T/--template-preset NAME`, in the same way as mapping presets. They are ordinary templates
   (ADR 0003 §2), and their hash and parsed JSON enter the transform config.
3. **`incident_report` (`incident.report`, version 1)** reads the incident-report form that both corpus
   sites use: `Incident no`, `Occurred at`, the `Time | Event` timeline, and the `Description` and
   `Root cause` sections, verbatim.
   - Times use `%Y-%m-%d %H:%M:%S` or `%Y-%m-%d %H:%M`, whichever matches the whole text.
   - The form states no zone, so the zone is `unstated` and `civil_time_zone` is `Unknown`.
   - Each document's timeline is one civil clock (its table and the `Time` column, ADR 0002 §5). The
     model has no field for a clock's name. On INC-C3-0011 the record's verbatim description states
     that the clock is the cell HMI alarm log, and that the cell PC shows the same alarms about 90 s
     later. Aligning the two clocks is Memory's job, through `derived/clock_mapping`, as inferred
     claims.
   - The form has no form id, and the template matches by structure (ADR 0003 §3). Another template
     with the same required structure makes such a report `template_ambiguous`, which is the
     intended loud failure.
4. **`requalification_csv` (`requalification.csv`, version 1)** has two rules over the same sheet
   shape:
   - `Site` or `Cell` names the place. A table with both columns matches both rules and is
     `rule_ambiguous`.
   - Up to three `Test n` / `Result n` pairs are read, with the overall `Result` and the return to
     service (`Decision`, `Decided By`, `Decided On`). Every value is verbatim, and nothing decides
     that a requalification passed (package non-negotiable 2).
   - The zone is `unstated`, and `Inspector` is ignored on purpose.
5. **`cmms_generic` version 2 reads `INSP` as a `maintenance_event`.**
   - An inspection is maintenance work (EN 13306). Its actions are the checks it states, such as
     "no fault found".
   - The work-order type is a vendor value, so it stays in the mapping file (ADR 0002 §3).
   - Any type the preset does not name is still `row_unmatched`.
   - This amends ADR 0004 §5. The archetypes' inspection work orders (WO-26-0314 in the lifecycle
     fixture, WO-26-0420 and WO-26-0709 in the archetypes) are now records, and the golden lifecycle
     packages change by those records, their findings and the preset's transform id. The task ticket
     remains the deliberately unread row.
6. **The corpus is a committed fixture.** `tests/fixtures/demo_corpus/make_demo_corpus.py` takes five
   corpus files byte for byte from the generator: both incident PDFs, both requalification CSVs and
   PLANT-2's CMMS export. It ingests them with `--no-plugins`, as a subprocess, and commits the package
   (ADR 0004 §3, §4). A test checks every source against `harness/acceptance/corpus.lock.json`, so a
   corpus change fails until the fixture is regenerated.
7. **`cmms_downtime` (`cmms.downtime`, version 1) maps each downtime-log row to an `intervention`.**
   - `Downtime ID` is the identifier under `cmms.downtime`. `Stop Type` is the `mode`, verbatim.
     `Reason` is the `reason`, `Stopped` the `start` (required) and `Restarted` the `end`. A blank
     `Restarted` is `Unknown`, never "still stopped". `Related` is split on `;`. `Location` and
     `Reported By` are ignored on purpose: the kind has no place or reporter field.
   - Times follow §1: the log's own civil clock, the zone `unstated`, never UTC. A stop that crosses
     midnight reads the dates it states. A time-only restart is `Unknown` with `value_unreadable`,
     and the next day is never inferred.
   - An `incident_record` would make a planned stop an incident, and it has no end time. An
     intervention has a start, an end, a mode and a reason, and Memory's event index already maps
     an intervention's stated mode to its event kinds. The CMMS stop and the controller's syslog
     stop are kept as each source states them (14:33:10 and 14:32:38 for INC-C3-0011). Only a
     stated same-event assertion joins them, and that is Memory's job.
   - The fixture is corpus 2.0.0's `downtime_log.csv` (harness PR #143), committed as a source and
     ingested into `tests/fixtures/demo_corpus/downtime_package/`. It is pinned by content id until
     2.0.0's lock is on main, which then binds it.
8. **`servicenow_csv` version 2 states the configuration a change results in.**
   - `u_after` is also the record's `configuration`: an id under `servicenow.u_after`, copied as
     written (`5.6.0`, `TCP z=145.5 mm`), never parsed, and citing its cell. A blank cell is
     `Unknown`. Memory reads it to answer "what changed since the last good run".
   - The change record kind (root ADR 0051) has no prior-configuration field. `u_before` stays the
     change item's `before`, which is the field that means exactly "the value before". An id-typed
     prior configuration would be a compiler change, and none is raised for Demo v1.
   - The archetype goldens change in their change records (`configuration` is now stated and leaves
     `fields_not_covered`) and in the preset's transform id.

## Alternatives considered

- **Complete time-only rows from "Occurred at" now.** No corpus source needs this, and building it
  would add a derivation path that nothing exercises. Lost. §1 fixes how it will be built.
- **Fix the fixture template's format in place.** That is a test fixture of ADR 0003 and stays as it
  is. A production template ships instead, so the demo depends on nothing under `tests/`.
- **One arm-cell-only template.** INC-C3-0011 and INC-0007 are the same form with the same
  structure, so two templates would make both reports `template_ambiguous`. Lost.
- **A separate `cmms_inspection` preset.** A table is read by the first mapping that applies
  (`plan_tables`). A second CMMS preset would leave the other's rows `row_unmatched`. Lost.
- **Declare `America/Detroit` for PLANT-2.** The incident report and the requalification sheet do not
  state a zone, and §1 allows a zone only if the document states it. Lost.

## Consequences

- Platform's Deploy map stage can run with shipped names only: `-p cmms_generic -p
  requalification_csv -p cmms_downtime -T incident_report`, plus the other presets it already uses.
- On the corpus, every one of INC-C3-0011's five HMI times and INC-0007's four reads. Four
  requalification records map (three PLANT-2, one S-007), and all 16 CMMS rows of the two sites
  (10 at PLANT-2, 6 at S-007) are maintenance events, WO-26-0709 among them.
- The archetype goldens and two tests moved with `cmms_generic` 2. The reasons are given in §5.
- Revisit when a source prints time-only rows (§1's derivation, with tests for a missing date and for
  rows that cross midnight), or when the model gains a clock name.
