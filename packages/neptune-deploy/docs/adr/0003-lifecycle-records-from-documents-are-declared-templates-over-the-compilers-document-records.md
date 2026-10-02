# 0003 — Lifecycle records from documents are declared templates over the compiler's document records

- Status: Accepted
- Date: 2026-10-02
- Issue: MVL-114
- Builds on: Deploy ADR 0002 (the mapper, §2 not an ABI adapter, §4 values, §5 clocks, §7 lineage);
  root ADR 0038 (PDF and Markdown adapters), root ADR 0051 (lifecycle kinds)

## Context

Risk assessments, commissioning reports, incident reports and SOPs arrive as PDF and Markdown. The
compiler's document adapters already read them (root ADR 0038): a `DocumentRecord` per file, a
`DocumentBlock` per unit of text with its role, level and exact `[Page, Span]`, and, for a tagged PDF
or a GFM table, a `StructuredTable` and a `StructuredRecord` per row whose cells cite their spans.
What Deploy adds is meaning, and meaning is a declaration. A hazard table in one site's form has
`Severity | Exposure | Avoidance | Risk reduction`; another site's has `Severity of injury | Exposure |
Avoidance | Risk level`, and a label such as `Date` is an approval date in one and an assessment date in
another. Forces:

- Deploy adapters are leaves that never read bytes they have no decoder for, and no member may import a
  format adapter (ADR 0002 §1, §2). A second PDF reader would disagree with the first about text and
  citations.
- A document is evidence. A lifecycle record states what the document states, as it states it
  (non-negotiables 1 and 2 of this package): no ranking a severity, no summarising a root cause, no
  reading a table as a safety case.
- All input is hostile, and documents differ: a scan has no text, a page may be rotated, and a form is
  revised.

## Decision

1. **The same mapper, one more input.** `neptune_deploy.lifecycle.documents` reads `DocumentRecord`,
   `DocumentBlock`, `StructuredTable` and `StructuredRecord` from an ingest package, through declared
   **document templates**, and writes lifecycle records into the same new package as the table mappings
   (`map_files(base, mappings, templates)`). It never opens a document's bytes. It reuses the table
   mapper's value reading (`_Values`: shapes, scalars, lists, parts, clocks), so a field has the same
   shape, the same `Unknown` and the same time rules whichever evidence it comes from. A document is
   not read through the ABI, for ADR 0002 §2's reason.
2. **A template is a declared, versioned JSON file** (`neptune-deploy.document-template/1`), in the
   style of a mapping file (ADR 0002 §3): `id`, `version`, the lifecycle `kind`, the document `formats`
   it reads, a `separator`, a `zone`, an optional `form`, named `tables`, `requires`, `ignore` and the
   `fields`. A **registry** holds templates by `(id, version)`: two versions of one template coexist and
   one version twice is refused. A malformed template, an unknown kind or field, a selector a field's
   shape cannot take, a table that is declared and unused or used and undeclared, a column its header
   lacks: refused before any document is read. It is the operator's configuration, and it fails loudly.
3. **Matching is exact, and it is an observation.** A template applies to a document when:
   - the document's format is one of the template's;
   - if the template declares a `form`: the document shows the form's id label with its value, and the
     version label with the declared version, exactly, as text (a label shown on every page counts once
     if every statement agrees; one that disagrees is not the form, and two versions are a mismatch).
     The form's id and version labels are identifiers, not prose: each reads only the rest of its own
     line (§4's wrapped values do not apply), and the lines after it stay for other labels or `text_unread`.
     The same id with another version is not a match and never a near one: it is `template_version_mismatch`, no record, naming the version found;
   - every `requires` label, heading and table is present: a label by its text, a heading by its text, a
     table by its exact header cells. A template with a form that lacks structure is
     `template_structure_missing`; one without a form that lacks structure is simply not that document's.
   Two matching templates match none (`template_ambiguous`, an error), and a document no template
   matches is `document_unmatched`. A match is recorded as an `info` finding, `template_matched`,
   whose `related` cites each place the structure was seen, whose `records` names the record it
   produced, and whose details list the kind's fields the template does not cover. A finding is the
   compiler's own statement about evidence (root ADR 0017 §9), which is what an observation is; the
   lifecycle record's values stay `stated`. No lifecycle record is ever the observation itself.
4. **A field names what it reads by `label`, `section` or, inside a table's rows, `column`.**
   - `label`: an inline `Label<separator> value` at the start of a line of a paragraph, list item or
     untyped block, or a row of a headerless table whose first cell is the label (a form's
     `Field | Value`). An extractor wraps a paragraph into one block of several lines, and a form may
     put a value under its label, so a value is the rest of the label's line **and every following
     line of the same block up to the next line that starts with a label the template names** (a
     field's, a required or ignored one, the form's), joined by one space. The value cites one span
     from its first line's text to its last line's; one citation cannot hold text joined that way, so a
     value of several lines also gets a `label_value_wrapped` finding with the line count and the spans of
     its first lines (capped like every finding's list, so a block of thousands of lines stays small). A value
     that goes on in another block or on another page is not followed (§9). Reads are tracked per
     line: a line no label took (before the first label of its block, say) is `text_unread` with its
     own span. A label shown twice is `label_repeated` and `Unknown`, unless every statement is the same
     value, which is then that value (as for a form, §3); one the document lacks is `NotCovered` with
     `label_absent`; one shown with nothing after it is `Unknown`.
   - `section`: the blocks under a heading, up to the next heading at its level or above. As a text
     field it is **free text, copied verbatim and cited by one span** from the first block to the last
     (blocks on one page, each starting one LF after the last ends: the page text's own layout). Text
     that cannot be one span (it crosses a page, holds a figure, or has a gap) is `Unknown` with
     `section_not_contiguous`, never a shortened or joined guess. As a statements field it is the
     section's list items, each citing its own block.
   - `{"rows": <table>, "each": {...}}`, a top-level field only (never inside a part, and never for
     scores; a template that tries is refused): a list of parts, one per row of every table with exactly that
     header (a table that repeats its header on each page is several tables, read in document order),
     each cell cited. A part whose cells are all blank is not listed (`item_blank`).
   Everything else follows ADR 0002 §4: a cell the compiler held `KnownAbsent` stays so, a value that
   does not read under its declared format is `Unknown` (`value_unreadable`), nothing is converted.
5. **Free-text sections are never interpreted.** Description, root cause and hazard text are copied as
   the compiler extracted them and cited by span. There is no summary, no classification and no model
   in this path: model extraction belongs to Memory G4, a later consumer of these spans.
6. **Lineage as ADR 0002 §7.** Each template is a transform: `adapter_id` `deploy_document_map`,
   version `0.1.0`, config `{base_package, template, template_sha256}`, upstream the compiler transforms
   of the documents it matched. Findings about documents no template matched sit under one more
   transform (config: the base package and every template's hash). A document and a template make one
   record, whose provenance is the document's whole bytes, so a different template, or a template
   edited, is new lineage beside the old. Clocks follow ADR 0002 §5: the template's zone is in the
   transform's config alone, and a `TimestampDomain`'s scope is empty.
   A table of a document that a template matched is that template's: a mapping file never reads it
   again, so one cell is never two records.
7. **Findings, coded `deploy_document_map.*`**, each documented with its severity and category in
   `lifecycle.documents.FINDINGS`: `template_matched`, `template_version_mismatch`,
   `template_structure_missing`, `template_ambiguous`, `document_unmatched`, `no_text_layer`,
   `page_rotated`, `text_unread`, `column_unmapped`, `label_absent`, `label_repeated`,
   `label_value_wrapped`, `section_absent`,
   `section_repeated`, `section_not_contiguous`, `value_unreadable`, `value_blank`, `list_cell_blank`,
   `list_part_empty`, `list_id_repeated`, `item_blank`, `identifier_repeated`,
   `record_unrepresentable`. `value_unreadable`, `list_cell_blank`, `list_part_empty` and
   `list_id_repeated` are one finding per value, naming the record and the field's JSON pointer, never
   capped (ADR 0002 §6); the number, text and list rules are the table mapper's. In particular:
   - `no_text_layer`: a document with no text and no table (a scan, or one the compiler could not
     read) is a finding and has no record; OCR is a derived annotation and never done here.
   - `page_rotated`: a declared rotation changes no span and no value, because spans index extracted
     text and regions stay in the page's own coordinates; the finding says which pages, so a reader
     who draws regions knows.
   - `text_unread` and `column_unmapped`: a matched document's blocks, headerless-table rows and table
     columns that no field, required or ignored structure accounts for are listed (first ten cited), so
     nothing is dropped silently. Running headers and footers are page furniture and are not counted.
     `ignore` names, on purpose, labels, headings, tables and columns a template leaves unread.
8. **A procedure is not an event.** An SOP is written to be followed, not to say anything happened. A
   template makes a `maintenance_event` of an SOP only through the labels of a work record (a work
   order and a date performed) that its `requires` lists, so a blank SOP with steps and no work
   record does not match. Its steps are then the actions stated, in order, each citing its list item.
9. **What the compiler's document records do not give** (listed, not worked around; no change to
   `src/neptune`):
   - **Fillable form fields.** The PDF adapter reads page content, not an AcroForm: the values of
     form fields and the state of check boxes are not in any record, so a template cannot read them.
   - **Tables of an untagged PDF.** The adapter does not guess tables (root ADR 0038 §5), so such a
     document offers labels and sections only; a template that requires a table does not match it.
   - **A citation across pages.** An `EvidenceRef` holds one page, so text that continues on the next
     page cannot be one citation (§4). Markdown paragraphs are separated by blank lines that are not
     in any block, so a multi-paragraph Markdown section cannot be one span either.
   - **Clock zone** and an **`Unknown` list**: as ADR 0002 §Consequences.

## Alternatives considered

- **A Deploy adapter that parses PDFs.** A second PDF reader, drifting from the compiler's text and
  spans, and an import the member rule forbids. Lost.
- **Heuristic extraction** (key-value by nearest text, table finding by geometry, a language model).
  Inference, nondeterministic in the model case, and it moves meaning out of a declaration into
  code. Lost; geometry and models are `derived/` and Memory G4.
- **Matching the nearest registered version of a form.** Silently reads a revised form by an old
  declaration; a changed table or label would give plausible wrong values. Lost: a version mismatch
  is a finding and a new template version is a deliberate act.
- **Putting the template into the mapping file schema.** A mapping file names columns of a table; a
  template names labels, sections and tables inside a document, and has matching rules a mapping does
  not. One schema would blur both. Lost; the two share the field shapes and the value reading.
- **Summarising free text into the record.** Interpretation of evidence (non-negotiable 8). Lost.
- **Marking the match `observed` on the record's own provenance.** ADR 0051 fixes lifecycle records
  as `stated`; the match is a separate fact, recorded where Neptune makes statements about evidence.
  Lost.

## Consequences

- A new site's form is a template file, not code. Two formats of risk assessment need two templates,
  and two revisions of one form need two versions.
- A document's receipt says what matched, what the template does not cover, and what the template
  left unread, so an incomplete template shows as findings, not as a record that looks complete.
- One record per document per template, with every hazard, test or timeline entry a part of it. Several
  documents may state one identifier (a revised report): they are kept apart (`identifier_repeated`).
- Fixtures are compiler packages from generated tagged PDFs and one Markdown file
  (`tests/fixtures/documents/make_document_fixtures.py`): an AMR fleet, a manipulator cell and a
  legged robot, plus a scan, a rotated page, a revision mismatch and a section that crosses a page.
- Revisit if a record spans several documents, if a template needs a join between two tables, or if
  the compiler gains form fields or untagged tables.
