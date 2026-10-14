# 0028 — Vendor procedures named by their heading, the parts they apply to, and parts in identity (graph-schema 2.4.0)

- Status: Accepted
- Date: 2026-10-14
- Issue: MVL-191
- Amends: ADR 0027 §1-§2 (a declaration may name the procedure by a heading, need no revision label and
  read parts; an effective date may be followed by text), ADR 0019 §1 (identity also names parts)

## Context

### Where SB-2026-117's evidence stops (traced on corpus 2.2.0, `make demo` at a6782579)

The demo question "did the maintenance on ARM-3A before INC-C3-0011 follow the vendor's procedure?" needs the
vendor service bulletin's applicability and steps beside what work order WO-26-0911 did. Stage by stage:

- **Compiler.** `sites/PLANT-2/vendor/SB-2026-117_PG-80_finger_sets.md` (Markdown) gives one `document_record` and
  12 `document_block`s: the level-1 heading `Service bulletin SB-2026-117: PG-80 long finger sets`, one paragraph
  holding two label lines (`Applies to: PG-80 grippers fitted with finger set FS-0340 (long, +6.0 mm).` and
  `Issued: 2026-08-30 by the gripper vendor's field service.`), a `Summary` paragraph, a level-2 `Procedure`
  heading over four `list_item`s, then the hidden HTML comment (gold trap T1) as a block whose role is `Unknown`
  (no paragraph, no list item), and a `Contact` section. Every block cites its decoded character span.
- **Deploy and the Ledger.** No template matches (`document_unmatched`); the records are in the export Memory
  reads. WO-26-0911 is Deploy's `maintenance_event`, whose `parts` state `serial:FS-0340` installed and
  `serial:FS-0291` removed (the CMMS `Installed Serial` and `Removed Serial` columns).
- **Memory.** `memory.events` states WO-26-0911 `involves` the asset `serial:FS-0340`. ADR 0027's declaration
  reads only documents with `Procedure:` and `Revision:` labels, so the bulletin is not read: no claim cites it.
  Nothing relates the bulletin's "FS-0340" to the CMMS serial, and nothing should by equal text alone: the
  vendor's part number and the plant's serial are ids of two systems (ADR 0021 §1, Platform ADR 0013).
- **Context.** `neptune_compare_runs` carries WO-26-0911's actions and SOP-CELL-021's revisions; nothing reaches
  the bulletin.

### Plan

Read the bulletin under a second declaration of `memory.procedures`: named by its one level-1 heading, dated by
its `Issued` label, its steps under `Procedure`, and the part its `Applies to` line names as an asset in a
declared vendor namespace. The corpus states, as a reviewer's `same_identity` assertion, that the CMMS serial
and the vendor part are one finger set (Platform ADR 0014); identity joins them with a stated `same_as`, so a
path WO-26-0911 → `serial:FS-0340` → `vendor.part:FS-0340` → the bulletin exists only because someone said so.
Context's comparison carries the procedures for the parts the maintenance changed (Context ADR 0018).

Forces: nothing inferred; the bulletin is the vendor's stated advice, which may say what should have been done
and never what was (gold trap T5); its hidden comment must never be read as a step, a value or evidence (gold
trap T1); a blank is never a fact; two equal strings in two namespaces are never one thing without a statement.

## Decision

1. **A declaration may name the procedure by a heading.** `procedure` is a label (ADR 0027) or
   `{"heading": <level 1-6>}`: the text of the document's heading at that level is the `has_name`, citing the
   heading. A document named by its heading has nothing else saying it is a procedure, so it is one only when it
   also shows the declared `effective` label; two headings at that level with different text are
   `title_repeated` and not read. `revision` is optional: a document without one is one revision of itself.
2. **`parts` (optional): `{"label", "pattern", "namespace"}`.** Each match of the pattern's one group in the
   label's value (one line, ADR 0027 §1) is an id: `involves` the asset `<namespace>:<id>`, citing the id's own
   span. A pattern must compile, have exactly one group and be at most 200 characters. No match is
   `part_unmatched`; an id that is no logical id is `part_unreadable`. The asset is the document's own reference:
   Memory joins it to nothing else.
3. **An effective date may be followed by text after a space** (`2026-08-30 by the gripper vendor's field
   service.`): the date is the leading `YYYY-MM-DD`, and the event's time cites those ten characters alone; the
   `declared_value` keeps the whole line. Anything else is still `date_unreadable`.
4. **Identity v5: parts are nodes an assertion may name.** An asset that a `memory.events` or
   `memory.procedures` claim `involves` and no Ledger thread holds becomes a node, placed where the claim naming
   it with the fewest records (then the smallest id) starts, as event nodes are (ADR 0019 §1). A `same_identity`
   assertion naming two of them gives a stated `same_as` citing the assertion; without one, nothing joins them.
   Identity now runs after `memory.procedures` too.
5. **Only declared places are read.** Label lines of paragraph blocks, list items under the steps heading and
   the title heading; never a block of another role (the HTML comment), a summary paragraph or a heading's text
   as a value. `memory.procedures` is version 2.
6. **graph-schema 2.4.0, golden-only.** No predicate, kind or node type is added (`procedure_revision`, `involves`,
   `same_as`, `asset` exist); identity's version moves the goldens' claim ids. Context and Deploy raise their
   locks in the same change.

## Alternatives considered

- **Read the bulletin's ids into the CMMS's `serial` namespace by declaration.** One config line, no identity
  change; lost because it is an equal-string join of a vendor's part number and a plant's serial that no source
  states (Platform ADR 0013's rejected alternative).
- **A manifest declaration of parts.** Manifest version 1 has no place for parts or documents; a new section is a
  compiler and package-schema change for one fact a person can state with an existing assertion.
- **A new event kind (`vendor_advice`).** A vocabulary minor through every consumer; a bulletin is a procedure
  issued on a date, as an SOP revision is, and its name and path say whose it is.
- **Reading the Summary paragraph, or every paragraph, as advice.** Free text with no declared label; the steps
  already say it, and the less read the less a hostile document can plant.
- **Skipping HTML comments by text (`<!--`).** The compiler already gives an HTML block no paragraph or list role;
  reading only declared roles covers comments and every other non-content block alike.

## Consequences

- The acceptance snapshot gains the bulletin (10 claims) and one `same_as` (16 to 17); every claim of
  `memory.procedures` and `memory.identity` changes id (their versions and config), with the same content.
- A deployment can declare any vendor document whose title is its heading and whose applicability names part ids.
- Revisit when the compiler or Deploy states procedure documents or part identities as records, when a document's
  applicability names machines and parts in one label, or when a part's lifetime matters (a `same_as` from an
  assertion with no window holds from the first claim naming the part, a convention).
