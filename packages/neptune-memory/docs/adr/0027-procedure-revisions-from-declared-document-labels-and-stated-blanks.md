# 0027 — Procedure revisions from declared document labels, and stated blanks (graph-schema 2.3.0)

- Status: Accepted
- Date: 2026-10-14
- Issue: MVL-191

## Context

### Where SOP-CELL-021's evidence stops (traced on corpus 2.2.0, `make demo` at 27fb3538)

Demo v1 needs three gold claims that only the hand-eye SOP states: Q1.C5 (1.86 px passed because revision C
relaxed acceptance from 0.8 px to 2.0 px and dropped the 10-pick verification, naming no change record and no
approver), Q2.C4 (revision B to C on 2026-09-11, no change record) and Q4.C4 (nobody is named as approving
revision C). All three were gaps (`in_graph: false`). Stage by stage:

- **Compiler.** `SOP-CELL-021_rev_B.pdf` and `_rev_C.pdf` (tagged PDFs) give one `document_record` each and
  14-15 `document_block`s: the heading, one paragraph per label line (`Procedure: SOP-CELL-021`, `Revision: C`,
  `Effective: 2026-09-11`, `Applies to: ARM-3A, wrist camera WCAM-3A`, `Approved by: ` with nothing after it), a
  `Procedure` heading over one `list_item` per step, and the `Revision history` table as a `structured_table`
  (`Revision | Date | Change | Change record`) with one `structured_record` per row. Revision C's row C holds its
  `Change record` cell as `Unknown` (a blank, cited at its empty span). Every block cites `[Page 0, Span]`. No
  `sop_section` is made: root ADR 0063 reads `Step <n>:` blocks under a `Procedure ID` label, and this SOP uses
  `Procedure:` and an untitled list.
- **Deploy.** No document template matches an SOP (`document_unmatched`): Deploy maps documents only to lifecycle
  kinds (ADR 0051), and none is a procedure revision (Deploy ADR 0003 §8: a procedure is not an event).
- **Ledger.** Both packages are registered whole; the SOP's records are in the export Memory reads.
- **Memory.** No consolidator reads `document_block`, `structured_table` or `structured_record` of a document:
  no claim cites either PDF.
- **Context.** `neptune_compare_runs` (the skill's path for "why" and "what changed") carries maintenance events
  on the machine; nothing about procedures. "What is unknown" does not compare runs at all.

### Plan

A new deterministic consolidator reads the compiler's document records under a declaration in Memory's config
(which labels name the procedure, its revision, its effective date and its machines; the steps' heading; the
revision table's header), and states each revision as an event on the machine it applies to, with each label,
step and history row as a declared value, and each blank label or cell as a stated blank. Context's
`compare_runs` carries what differs between the revisions of a procedure on the later run's machine, and the
skill compares the runs for "what is unknown" too (Context ADR 0016). Nothing is inferred: "it passed only
because" is the answer composing the stated limits, the 1.86 px result and the stated blanks.

Forces:

- A new compiler or lifecycle kind (`procedure_revision`) is a package-schema major (root ADR 0063 §9) through the
  compiler, Deploy, the Ledger's kind registry and every consumer. The facts are already in canonical records;
  what is missing is a declared reading of them.
- A blank is never a fact (non-negotiable 3): `Approved by: ` must not become an approver `""`, and a blank
  `Change record` cell must not become "no change happened". But "the document names no approver" is exactly the
  evidence Q4 asks for, and it must be citable.
- Memory never guesses identity: `ARM-3A` in a PDF joins a machine only through an id a source states.

## Decision

1. **`memory.procedures` (version 1)**, a deterministic consolidator reading `document_record`,
   `document_block`, `structured_table`, `structured_record` and `machine` records (through the `LedgerReader`,
   deduplicated by id; one id with two contents is a `record_conflict` finding and unused). Its config declares
   procedure documents; without one it states nothing:

   ```json
   {"documents": [{"name": "...", "procedure": "Procedure", "revision": "Revision",
     "effective": "Effective", "machines": {"label": "Applies to", "separator": ", ",
     "namespace": "manifest"}, "labels": ["Approved by"], "steps": "Procedure",
     "history": {"header": ["Revision", "Date", "Change", "Change record"], "date": "Date",
     "description": "Change"}}]}
   ```

   A label is read only as the start of a line of a paragraph block, `<label>:` then the rest of that line
   (one line: a wrapped value is not followed, as root ADR 0063 §3's first line). A document is a procedure
   revision when it shows the `procedure` and `revision` labels with values; a document showing a label twice
   with different values is `label_repeated` and not read. Matching is exact text: no fuzzy label, no heading
   guess.
2. **A revision is an event**, node `record:<document_record id>`, at the date its `effective` label states
   (`YYYY-MM-DD` only; another form is `date_unreadable`, absent is `untimed`: no claims, a finding). The
   procedure is not an event; its revision taking effect is a dated statement of the document. Claims, all
   `stated`: `event_kind` `procedure_revision` (a new registered kind), `has_name` the procedure label's value,
   `evidenced_by` the document record, `involves` each machine it applies to, and `declared_value` for each read
   label (`["Revision"]`, `["Effective"]`, ...) and each step (`[<steps heading>, i]`, the `list_item` blocks
   under that heading up to the next heading at its level or above, text verbatim), each citing its own span.
3. **Each revision-history row is an event**, `record:<document_record id>/revisions/<i>` (as a timeline entry is
   for an incident), at its `date` cell: `has_description` its `description` cell, `declared_value` each other
   cell (`["Revision"]`, `["Date"]`, ...), `evidenced_by` the row, `involves` the document's machines. A row
   whose date is blank or unreadable states nothing (a finding).
4. **`stated_blank` (event → text, `many`)**: the record shows this label or column with no value, named as
   written (`"Approved by"`, `"Change record"`). It is evidence about the document, never a value and never
   evidence that nothing exists: "revision C names no approver", not "nobody approved it". It cites the blank's
   own place (the label's line, the cell's empty span).
5. **Machines through stated ids only.** Each `machines` item (split on the declared separator) joins
   `<namespace>:<item>` only when a `machine` record states that id; any other item (`wrist camera WCAM-3A`) is
   counted in a `reference_unresolved` finding and joins nothing. A revision that joins no machine still states
   its claims.
6. **A document's dates are on its own calendar.** The PDF states a civil date and no zone, so it is not
   absolute (`CivilClock` needs a stated zone). Each document's dates are ticks of whole days since 1970-01-01 as
   written, on a clock keyed by the document's bytes (`record_id("memory.document_calendar", {source,
   resolution})`, 86 400 s per tick): two documents are never ordered against each other or a run by Memory.
   An instant is `[d, d + 1)`.
7. **graph-schema 2.3.0, a minor**: the `stated_blank` predicate and the `procedure_revision` kind
   (`VOCABULARY_VERSION = 13`). Nothing is narrowed. Context and Deploy pin 2.3.0 in the same change;
   query-packet 1.6.0 adds `stated_blank` to its predicate enums (Context ADR 0016).

## Alternatives considered

- **A Deploy document template.** The right shape for reading labels, but a template writes a lifecycle kind and
  none fits: a `change_record` would state a change record exists exactly where the document names none, and a
  new kind is a package-schema major across five packages. Revisit when a lifecycle `procedure_revision` exists.
- **Extending `neptune.declared`** (root ADR 0063) with `Procedure:`/`Revision:` labels: a new compiler kind
  (package-schema major) and a re-ingest; the same facts, later.
- **An empty-string approver, or a `declared_value` typed `blank`.** The first is a blank turned into a fact.
  The second changes `DeclaredValue`, which Deploy's snapshot reader refuses beyond its known types; a predicate
  over a text literal reads in every consumer that reads the vocabulary from the document.
- **Revision-history rows as `declared_value`s on the revision** (`["Revision history", 2, "Change record"]`).
  A blank cell would then need a structured path in `stated_blank`'s object; as row events the blank's subject
  is the row, and its object is just the column's name.
- **One clock for every unzoned date at a site.** Would order revision B against C and both against the runs
  with no stated relation. Lost: no silent assumptions about clocks.
- **Inferring "the limit was relaxed".** Interpretation; it is the reader's composition of two stated values.

## Consequences

- A new consolidator adds claims and changes none: every other claim id in the acceptance snapshot is unchanged.
  The snapshot gains SOP-CELL-021 revisions B and C, their steps, their history rows, and three stated blanks
  (revision C's approver, row C's change record).
- `SOP-CELL-014` (a filled work record with no `Effective` label) is an `untimed` finding; the vendor bulletin
  (no `Procedure` label) is not a procedure document under this declaration.
- The declaration is part of `config_hash`: a new label is a new lineage, never a rewrite.
- Revisit when the compiler or Deploy states procedure revisions as records (this consolidator then reads those,
  or is withdrawn), when a label's value wraps onto a second line, or when a document states its zone.
