# 0063 — Task and world context records from declared structure: a pass over parsed records

- Status: Accepted
- Date: 2026-10-02
- Issue: MVL-33
- Extends: ADR 0020 §1 (who writes `Site` and `Asset`), ADR 0017 §4 (the `task` family's kinds),
  ADR 0037 §1 (schema version 5, provisional), ADR 0036 §8 (a new derived table)

## Context

Site manifests, asset registers, SOPs, task briefs and requirement documents arrive as YAML, CSV,
Markdown and PDF. The document (ADR 0038), tabular (ADR 0042) and configuration (ADR 0037) adapters
already turn them into blocks with exact spans, rows of cited cells and cited values, but nothing
says "this row declares asset AMR-07" or "this line states requirement INS-1 for task TB-117". The
acceptance is that later reasoning can ask which source stated a requirement or an asset's identity
without re-parsing the corpus. Forces:

- Adapters never import each other (ADR 0008 §4), and one source has one adapter. A register is a
  table first: its row stays a `StructuredRecord` (ADR 0020 §1) and the asset is a second reading.
- Explicit facts must be `stated`; anything a heuristic proposes is `inferred` and lives in
  `derived/` (non-negotiable 8). No model, no ML (non-negotiable 10).
- Identity is declared, never resolved here (ADR 0003; MVL-35 resolves).
- The `task` family was reserved by ADR 0017 §4 with no kinds. Deployment lifecycle kinds
  (interventions, maintenance; MVL-83, ADR 0051) describe work done, not the request for it.

## Decision

1. **A package-level pass, `neptune.context`, over records, not bytes.** It runs at assembly beside
   stream introspection (ADR 0049), over the admitted sources' committed records: `DocumentRecord`
   and `DocumentBlock`, `StructuredTable` and `StructuredRecord`, `ConfigurationSnapshot` and
   `ConfigurationValue`. It reads no source byte, calls no adapter, and lives in a new package,
   `src/neptune/context/` (imports: model, identity, derived), so adapters stay leaves.
   - **One transform per upstream adapter transform**: `neptune.context` 0.1.0, config `{}`,
     `upstream` that one transform. A record's spans and cells are in the text or table its upstream
     produced, and its id (`evidence_record_id`) moves only when that lineage does. A transform that
     declared nothing is not written; a package with no declarations gains no transform or table.
   - It emits a `context_extracted` event with its counts. Its findings are `context.*` (§8).
   - It holds only what it reads: streams, documents and their blocks, configurations, tables, and
     only the rows of tables that can declare something (a register's, an undeclared table's first
     row, a JSON row whose keys name a kind). A telemetry table's rows are streamed past.
2. **Registers and manifests (tables and configurations) → `Site`, `Asset`, `TaskBrief`,
   `Requirement`, `WorkOrder`.**
   - Keys are compared case-folded with runs of spaces and dashes as `_` (`Asset ID` = `asset_id`).
   - A table is a register only when its header is declared (`Known`: a CSV read with
     `csv_header=first_row`, a Markdown or Parquet table). The first of `requirement_id`,
     `work_order_id`, `task_id`, `asset_id`, `site_id` among its columns names the kind; the others
     are references (a task register lists its assets; a work order names its task). A JSON table names each cell's column by its own pointer's key.
   - A configuration document whose root mapping has a `site`/`sites`, `asset(s)`, `task(s)`,
     `requirement(s)` or `work_order(s)` section declares one entry per mapping in it, keyed by
     `id`. Nesting is the only relation read from structure: a site's own `assets` are at that site,
     a task's own `requirements` are for that task. A document with a root `neptune` key is
     Neptune's own manifest (ADR 0047) and is not read.
   - Fields come from fixed key lists (`name`/`title`, `aliases`, `category`/`type`, `site_id`,
     `parent_id`, `task_id`, `objective`, `status`, `text`/`statement`, `assets`, `machines`/
     `robots`, `latitude`/`longitude`/`altitude`/`crs`). A list in one cell is split at `;` and `,`,
     each item citing its span inside the cell (`[RowCell, Span]`, ADR 0020 §1). Other keys stay in
     the row or configuration, cited there.
   - Ids: the entry's own as `(<kind>, value)`; `serial`/`serial_number` as `("serial", …)`;
     `external_id` as `("external", …)`; any other `<scheme>_id` column as `(<scheme>, …)`
     (`cmms_id` → `("cmms", …)`). References use the namespaces `site`, `asset`, `task`, `machine`,
     `procedure`, `work_order`. Equal ids in two declarations are two records.
   - A position is built from a latitude and a longitude written as decimals, citing the entry; its
     CRS is the entry's `crs` (`EPSG:4326`) or `Unknown`; angle unit, height unit and height
     reference are always `Unknown`. A table with no such column says `NotCovered`; a blank cell or
     a missing manifest key says `Unknown`; a JSON or YAML null is `KnownAbsent`.
3. **Documents → `TaskBrief`, `WorkOrder`, `Requirement`, `SOPSection`, by explicit labels only.**
   Blocks are read line by line (code blocks never), each line citing its exact span inside its
   block's citation (after the block's `Page` step in a PDF).
   - `<Label>: <value>` for the labels `Task ID`, `Procedure ID`/`SOP ID`, `Work Order (ID)`,
     `Title`, `Objective`, `Status`, `Site (ID)`, `Asset(s)`/`Asset ID`, `Robot(s)`/`Machine(s)`.
     The first value of a label is read; a different later one is `context.label_repeated`.
   - A document with a `Work Order` label is a `WorkOrder` and its `Task ID` names the task it
     serves; otherwise a `Task ID` label makes it a `TaskBrief`.
   - `Requirement <id>: <statement>` (or `REQ <id>:`) anywhere is a `Requirement`; its `task` is the
     document's `Task ID`, else `Unknown`.
   - A block whose first line is `Step <number>: <title>` (`.`, `)` or a dash for `:`) is an
     `SOPSection`. It spans that block and the following ones up to the next step block or the next
     heading at its level or above (any heading, when the step block is not a heading). Its
     `procedure` is the document's `Procedure ID`, else `Unknown`. Its text stays in its blocks.
   - The title of a brief or order is its `Title:` label, else the title the document declares.
4. **Four kinds in the `task` family, since schema version 5** (`model/task.py`). Every one carries
   `declared_in`: the `DocumentRecord`, `StructuredTable` or `ConfigurationSnapshot` holding the
   declaration. All references are declared `LogicalId`s.
   - `TaskBrief` (`task_brief`): `identifiers`, `name`, `objective`, `site`, `assets`, `machines`.
   - `Requirement` (`requirement`): `identifiers`, `text` verbatim (the modal verb stays in the
     text; no priority is parsed), `task`. This is the issue's "RequirementSource": the record is the
     source's statement, cited, not a requirement in the abstract.
   - `SOPSection` (`sop_section`): `procedure`, `number` (as written), `title`, `order`, `blocks`.
   - `WorkOrder` (`work_order`): `identifiers`, `name`, `status` verbatim, `site`, `assets`, `task`.
     It is the request. The work done is a lifecycle record (ADR 0051, MVL-83), which refers to the
     order by its declared `("work_order", …)` id; no lifecycle kind is duplicated here.
   - A brief, requirement or work order names what it declares: an identifier or a stated name
     (text). A step spans at least its own block, each block once.
5. **`Site` and `Asset` are reused unchanged** (ADR 0020). Their record-level citation is the row
   or manifest entry; a document's `Site:` line is a reference, never a `Site`.
6. **Provenance.** Every record and field is `stated` by its context transform and cites the exact
   cell, span or JSON pointer; a record cites its row, entry, label line or step block. A document's
   declared title is re-cited by the context transform at the title's own citation.
7. **Candidates are derived** (`derived/context_candidate.jsonl`, `neptune.derived.context`):
   `numbered_heading_in_procedure` (0.5: a heading `3. Restore power` in a document that has steps
   or a procedure id), `modal_sentence_without_label` (0.4: an unlabelled prose line with `shall` or
   `must`), `undeclared_header_names_register` (0.6: a table whose header is undeclared but whose
   first row names an id column). Each names the record it read, the kind it proposes, the rule,
   the matched text and its citation. None becomes a record: a user who agrees adds the label or
   declares the header, and the next ingest states it.
8. **Malformed input costs findings, never the job.** `context.label_repeated` (inconsistent),
   `context.requirement_without_text` (missing; the record keeps `text` `Unknown`),
   `context.unnamed_declaration` (missing; no record), `context.section_not_entries`
   (unsupported), `context.coordinate_not_decimal` (unrepresentable; location `Unknown`),
   `context.value_not_text` (unsupported; the field `Unknown`), `context.crs_not_a_code`
   (unrepresentable; the CRS `Unknown`). A decimal no finite double holds is not a coordinate. If a
   reader still fails on one document, table or configuration, what it wrote is dropped and
   `context.failed` (failed, error) names that holder; the rest of the package is unaffected. All
   patterns are anchored with bounded repetition, so a hostile line costs time linear in its length.
9. **Schema version 5, provisional** (ADR 0037 §1): the four kinds are `since` 5, so packages that
   hold none of them keep their bytes. Package-schema 5.0.0, catalog-api 1.5.0 (programme rule:
   the integer is the registry major). The Ledger's projections (Ledger ADR 0009) are regenerated
   by its tool: `task_brief.site` and `work_order.site` fill the existing site columns, so
   migration 0007 is a guard. The coordinator renumbers at merge if another kind-adding change
   lands first.

## Alternatives considered

- **A dedicated adapter per register or document type.** It would have to re-parse CSV, Markdown
  and PDF (or import the adapters that do), and a source would be either a table or a register,
  never both, losing the row's other cells (ADR 0020 §1).
- **Typed records inside each adapter** (the tabular adapter emitting assets). Every adapter would
  learn every vocabulary, and a new label would change every format's adapter version.
- **Header-name guessing for undeclared CSVs.** A first row that reads `asset_id` is very likely a
  header, but the tabular adapter refuses to decide (ADR 0042 §2); deciding here would make the
  same file a register or not depending on which pass looked. It is a candidate.
- **Headings and modal verbs as requirements and steps.** High recall, no declaration: inferred,
  so candidates.
- **Record ids for references** (an asset's site as the `Site` record's id). Tier-2 ids are
  lineage-scoped and two declarations of one site are two records; declared ids are what MVL-35
  links.
- **One `TaskContext` kind with a type field.** Briefs, requirements, steps and orders have
  different fields; one kind would be a property bag.
- **A `WorkOrder` with performed-work fields** (dates, technician, outcome). That is the lifecycle
  intervention and maintenance records' (ADR 0051); duplicating them would give two answers.
- **Emitting `Site` and task records from Neptune's own manifest** (ADR 0047 §9). Right eventually,
  but its sections have another shape (aliases by namespace) and ADR 0047 owns it; a follow-up.

## Consequences

- "Which source stated INS-1?" and "which register states AMR-07's serial?" are answered from the
  package's `requirement` and `asset` tables, down to the span or cell
  (`tests/integration/test_context_job.py`).
- Every package with declarations gains one `neptune.context` transform per upstream adapter
  transform and a `context_candidate` table; a package without any is unchanged.
- The label and key vocabularies are part of `neptune.context` 0.1.0: adding a label is a new
  version, so a new lineage, never a rewrite.
- A CSV register needs `csv_header=first_row` (a manifest's `adapters.tabular` options) to be read
  as one; until then it yields a candidate.
- A JSON object of lists (`{"assets": [...]}`) is read by the text adapter (ADR 0037 §7), so it
  declares nothing here; YAML and TOML manifests, CSV, Parquet and JSON-array tables do.
- `docs/architecture.md`'s package table does not list `context/` yet (a dedicated docs PR).
- Revisit when MVL-35 needs more declared ids, when a dialect adapter (GeoJSON, MVL-31) states
  sites itself, or when users need labels in other languages.
