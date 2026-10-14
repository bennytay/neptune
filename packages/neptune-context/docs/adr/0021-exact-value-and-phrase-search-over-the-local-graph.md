# 0021 — Exact value and phrase search over the local graph

- Status: Accepted
- Date: 2026-10-15
- Issue: MVL-110

## Context

QA of a filmed demo (Claude Code over `python -m neptune_context.mcp --memory <graph> --sources <index>`): asked
"Prove that the TCP z offset on ARM-3A was changed from 145.5 to 151.5 mm", the agent searched for `151.5`, got
"this engine has no text index", and failed, although `work_orders.csv` row 7 ("Set TCP z 145.5 -> 151.5 mm on
pendant (tool1)") is a stated claim in the graph. "Why did the 1.86 px hand-eye calibration pass acceptance?"
failed the same way. The lexical channel (ADR 0008) existed but `--memory` ran only the graph channel; it
indexed text and declared values but no other literal (an integer, a real, a quantity); and its analyser cut
`151.5` into `151` and `5`, so the query `151` found `151.5`.

## Decision

1. **`--memory` runs the lexical channel beside the graph channel.** `mcp.__main__.graph_channels` returns
   `GraphChannel` and `text_channel(document, reader)`: a `LexicalChannel` over a `LexicalCorpus` built from
   every version of every claim of the graph document (`through` its head), read back from the reader at each
   query's snapshot. The harness's catalog-attached client runs the same two. They are peers under fusion (ADR
   0007); the engine's "no text index" gap is no longer emitted there. No model, embedding or network.
2. **What is indexed (field `claim_text`): each literal object as the agent renderer writes it.** Text as is;
   an integer, a real or a boolean as canonical JSON (`30`, `151.5`, `2.0`, `1e-05`, `false`); a non-finite real
   as its word (`nan`, `inf`, `-inf`); a quantity as its number and its unit symbol as declared (`145.5 mm`; no
   symbol when the unit is not known); a declared value as its key path steps and every value as above
   (`reprojection_error 1.86`). A real written with an exponent also carries its positional form when that is at
   most 64 characters (`1e-05 0.00001`). Instants, clock maps, computed deltas and record references have no
   words a source wrote and are not indexed. Field `declared_id` is unchanged: the node ids a claim names
   (names are `has_name` text). Stored values are never altered; this is the index's view of them, named in the
   channel config (`literals: literals-as-rendered/1`).
3. **A decimal number is one term.** In analysis (ADR 0008 §2), a compound that is exactly digits, one `.` and
   digits (`151.5`, `0.0745`) is one term (`numbers: decimal-one-term/1` in the backend settings). So `151.5`
   finds `151.5` and nothing else; `151`, `5` and `51.5` never find it. Every other compound keeps its parts
   (`2.4.1`, `1e-05`, `SN-1.86`, `v2.4`). A sign is punctuation (`-0.0745` finds `0.0745`).
4. **What matches, exactly.** Case-folded and NFKC-normalised (full-width `１５１．５` is `151.5`; `TCP Z` is
   `tcp z`). Numbers match as written: no trailing-zero, rounding or unit equivalence, so `151.50` does not find
   `151.5` and `1.10` does not find `1.1` (a version can be either). Words of prose are stemmed as before
   (ADR 0008); compound parts, ids and numbers never are. A quoted phrase is one required, adjacent run of terms,
   so `"reprojection error"` finds `reprojection_error 1.86` and "the reprojection error is at most 2.0 px";
   unquoted words are BM25-ranked alternatives. Ranking is BM25 then key, deterministic (ADR 0008 §1).
5. **`neptune_entities` with text also lists the subjects that state it.** Under the names it finds, the tool
   runs one `neptune_query` (through the same client) with the whole text as one quoted phrase over
   `claim_text` and `declared_id`, 10 items, the call's `include_inferred`, `as_of` and `kind`, and lists each
   matching claim's subject and predicate under `Stated in`, with the text clause that answers them cited.
   Nothing is added when nothing matches or the engine has no text index, so a question that states no value
   reads as before. The planner is unchanged: its rule 8 already puts words to search for in `text`.
6. **Contracts unchanged.** The query language already routes text to `lexical` over `claim_text` and
   `declared_id` (ADR 0002 §5); no field, value or packet member is added. query-packet stays 1.6.0. The
   engine's config hash moves (it names the lexical channel's settings), so packet ids over `--memory` move.
7. **The skill and the server instructions** tell the agent to prove or find a value, id or phrase by searching
   for it as written, quoted, with `neptune_query` `text` (lexical; `claim_text`, `declared_id`).

## Alternatives considered

- **Number equivalence (`151.50` = `151.5`).** Values in robotics text are also versions and part numbers
  (`1.10` is not `1.1`); conflating them is a silent assumption. Lost: numbers match as written, as the answer
  shows them.
- **A separate exact-match channel or regex search.** A second text channel duplicates ADR 0008's bounds and
  provenance; a regex over every claim is unbounded on hostile input. Lost: the one lexical channel, widened.
- **Index numbers inside identifiers as numbers too (`SN-1.86` finds `1.86`).** Identifier parts are searchable
  only as parts of a compound or a quoted phrase (ADR 0008); a serial is not a measurement. Lost.
- **Return cited claims from `neptune_entities`.** It would duplicate `neptune_query`'s answer with its own
  footers. Lost: it lists subjects, and names the query that cites them.

## Consequences

- Text questions over `--memory` are answered with cited claims; the demo's two QA questions resolve to
  `work_orders.csv` row 7 and to the calibration's `reprojection_error` beside SOP-CELL-021's limits.
- The corpus is built at server start from the whole document (about 1000 claims in the demo; linear in the
  claim count, within ADR 0008's bounds). A graph near those bounds gets `index_full` gaps, never a crash.
- Revisit when a vector channel joins `--memory`, when a catalog's record passages are served locally, or if
  an agent's questions need a unit- or value-aware numeric search (a typed range query, not text).
