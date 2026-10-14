# 0020 — Compact, grouped answers that name things, and the compare_runs trail

- Status: Accepted
- Date: 2026-10-15
- Issue: MVL-110

## Context

A QA run of the filmed Demo v1 asked "what changed on ARM-3A since its last good run?". `neptune_compare_runs`
answered 96 items as 77 600 characters of text plus 100 resource links (102 MCP content blocks). Every fact line
repeated a full node id (`event "record:rec:sha256:<64 hex>/actions/2"`), a full clock id and the same
`valid from … until …`; the `Items:` footer gave each item two content addresses and the `Evidence:` footer
repeated each source's content id per key. Hashes tokenise badly. Claude Code reported the output as too large
to display, or the agent asked again with a small budget and read 10–13 of the 96 items, missing the key
changes (WO-26-0911's finger set, TCP z and deferred recalibration; CAL-ARM3A-0818 → -0911; SOP-CELL-021 rev B
→ C, 0.8 → 2.0 px). The items came in packet order (item id, since every compared claim scores 1), so a work
order's name, cause and actions were scattered through the list.

## Decision

1. **The compare_runs trail** (`query-packet` 1.7.0, a minor). `compare_runs` now answers a trail,
   `{"kind": "compare_runs", "at", "before", "after", "sections": [{"section", "claims"}]}`, naming each claim
   it carried under the part of the comparison it answers: `maintenance`, `configurations`, `procedures`,
   `part_procedures`, `runs` (that order; each claim once, under the first section that carried it; claim ids
   sorted). It adds no fact and no relation: the section is why the clause (ADR 0015, 0017, 0019) carried the
   claim, which only the explainer knows. Every 1.x packet stays valid; supersedes ADR 0015 §1's "it produces no
   trail". `answer_problems` checks the trail names the clause's two runs; the Markdown renderer lists it by
   section.
2. **Facts are grouped and named** (agent renderer, ADR 0009). A comparison's Facts are its sections, each headed
   with its fact count; any other answer's facts are grouped by record in rank order (a record's first fact
   leads), then items that are not claims under `Other items`. Within a group a heading names the record once
   and states the validity its facts share; each fact line then states only the part of the record it is
   about (`"/actions/3"`), its predicate and object, and its own validity only when it differs. Fact numbers
   (`[I<n>]`) follow the order shown; evidence keys keep the packet's order (ADR 0003 §7).
3. **Handles, not hashes.** A node whose id is readable is written as it is (`asset "serial:FS-0340"`). A node
   whose id holds a content address gets a key `N<k>` and is written by the names evidence in the packet states
   for it (`event named "WO-26-0911" (N8)`; an inferred `has_name` never names), its parts by the record's
   handle and the path. Two records with one name differ by key. Clocks are `C<k>`. The `Names` and `Clocks`
   lists after `Not answered` give each key's full id once; a gap's refs use the handles. Trail headings and the
   header keep full ids.
4. **A comparison opens with a summary**: the two runs, each section's fact and record counts, and the
   unknowns (stated blanks, gaps, facts the budget cut).
5. **Compact footers.** `Items:` names a claim by its claim id only (`[I3] claim:sha256:…`, what `neptune_why`
   takes); other items by kind and item id. `Evidence:` and ADR 0016's `Sources:` list merge into one
   `Sources` footer: each file once (`F<k>`, its corpus paths when the index places it, and its exact content
   id or external ref), then `[E<n>] F<k> <place>` per key, steps joined by ` > `. A step is written in a
   person's words (rows, columns, pages and characters from 1; a cell by column number and name) exactly when
   the words read back to the same step; otherwise it is its hardened canonical JSON. So the footer still
   recovers every evidence ref exactly. Supersedes ADR 0016 §4's cell wording and §5 (the label is no longer
   beside an unchanged `Evidence:` footer).
6. **Drop repeated words, keep the markers.** A text object is its quoted string (`has_name "WO-26-0911"`);
   declared values keep their declared type and unit note; every line keeps `Observed`/`Stated`/`INFERRED`.
7. **No resource links without a catalog.** `python -m neptune_context.mcp --memory` attaches no Ledger
   catalog, so reading a link could only answer `unavailable`: its packet answers carry none
   (`build_server(..., links=False)`). Servers with an engine that hydrates keep them.
8. **`parse_answer` learns the shapes**: summary lines, section and record headings (end with `:`, no bracket
   outside a quoted string, a section's count must match its facts), the `Names`/`Clocks` lists, the new
   `Items:` lines and the `Sources` footer. `ItemKey.item_id` is `None` for a claim. The ADR 0003 §7 citation
   contract (`render_text` + `parse_citations`, the consumer conformance check) is unchanged; the agent answer
   was never that contract's renderer of record and is not a registry contract.

The demo comparison is 96 items in about 29 700 characters of text (77 600 before), with no links from
`--memory`; the test holds it under 30 000.

## Alternatives considered

- **Classify sections in the renderer** from predicates and node types. A work order and a procedure revision
  are both `event` nodes; telling them apart needs `event_kind`, which the comparison does not carry. Carrying it
  adds 11 items (past the 100-item default) for a fact the explainer already used. Lost.
- **Keep the exact locator JSON beside the place.** About 7 000 more characters for the demo; the round-trip
  rule gives the same exactness. Lost.
- **Shorter claim ids** (a prefix `neptune_why` would resolve). Ambiguity and a tool contract change for 1 300
  characters. Lost: the `Items:` footer (about 8 000 characters) is the price of exact follow-ups.
- **Shorten handles to a basename** (`CAL-ARM3A-0911.yaml`). An edit of what the source states. Lost: names are
  quoted whole.
- **Cap the item budget lower** so the answer fits. Drops the very facts that answer the question. Lost.

## Consequences

- One call returns the whole demo comparison in an agent's reading, ordered maintenance → configurations →
  procedures → vendor advice → runs, each work order read top to bottom.
- Every agent answer changes text (persona and trail goldens, the transcript); packets change only for
  `compare_runs` (the trail). Deploy raises its `query-packet` lock to 1.7.0.
- An answer under about 30 000 characters still costs about 8 000 for claim ids; revisit if `neptune_why` gains
  a stateless short reference, or if Claude Code's limits move.
