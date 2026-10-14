# 0018 — Stated same_as names one entity, and plain-language answers

- Status: Accepted
- Date: 2026-10-14
- Issue: MVL-110

## Context

A recorded dry run of Demo v1 ("why did the 1.86 px hand-eye calibration on ARM-3A pass acceptance before
INC-C3-0011?") got a correct, cited answer that still read badly on camera:

- `neptune_entities` answered `"ARM-3A": ambiguous between` six machine ids, so the agent said it "did not
  confirm" they were one robot. They are: the corpus manifest states the aliases, and Memory states
  `same_as` from `cmms.asset:ARM-3A` to the other five (Memory ADR 0021, Platform ADR 0013). ADR 0005 §3 and
  the entity index (ADR 0009 §4) never read `same_as`, so every alias was a separate candidate.
- Tool text leaked into the answer: `carried, not ordered against the two runs`, `no lexical or vector channel
  is attached`, `(not applicable, as declared)`, `declared_value declared value at`.
- The answer opened with paragraphs of hedging before it answered.

## Decision

1. **Ids memory states are the same entity are one entity.** `GraphEntities` (the `neptune_entities` and
   planner resolver) joins two offered ids when a `same_as` claim current at the snapshot relates them, is
   evidence (`stated` or `observed`) and has both ends of one node kind. Joins are transitive, also through
   nodes not offered as names (an unnamed syslog event). An inferred `same_as` never joins, whatever
   `include_inferred` says: an inference never settles an identity. A `same_as_candidate` is never read.
2. **The representative is the least declared id** of the offered ids in a group (code-point order). It does
   not depend on claim direction, claim order or which id the caller typed. It carries `SameAs`: the other
   entities, the joining claims' ids, their sources' content ids and the `same_as_depth` that reads every node
   of the group from any of its offered ids (the greatest number of `same_as` steps from one of them to any
   node). Every id stays declared on its own (`lookup`), so a query or a plan naming an alias still works.
3. **One line, one candidate.** A mention whose candidates a group joins has the representative as its one
   candidate. Ids nothing joins stay `ambiguous between ...`, never settled. A listing shows the group once.
   The line reads `machine "cmms.asset:ARM-3A", also declared as "incident_report.machine:ARM-3A", ... (one
   machine: 5 same_as claims in evidence join these ids, cited from "neptune.yaml", ...; query it with
   "same_as_depth": 2 to read every id)`. Files are named only when the server has a source index (ADR 0016).
   If the depth is beyond `MAX_SAME_AS_DEPTH`, the line says to list every id instead. The planner's model
   sees the other ids as `same_as` on the candidate.
4. **A query and a comparison read the same claims, whichever joined id they name.** A query whose subject is
   any joined id at that depth reads the same items and gaps (tested over the acceptance snapshot for all six
   ARM-3A ids). `compare_runs` follows `same_as` from the later run's machine transitively (it followed one
   step), so it no longer depends on which alias the run sheet names. The demo's comparison is unchanged.
5. **Answers use plain words; nothing they need is dropped.** In the agent renderer:
   - a value's unit reads `(unit "mm", as declared)` when stated, `(no unit stated)` when the source gives
     none, nothing when a unit does not apply (a text), and `(unit stated absent)`,
     `(unit not covered by the source)` or `(unit ambiguous between ...)` otherwise. This supersedes the
     `(<unit>, as declared)` form of ADR 0014 §3;
   - under the `declared_value` predicate the literal reads `at <path>: <type> <value>`, not
     `declared value at`; elsewhere it keeps the prefix;
   - a gap line drops its retrieval channel, which a reader cannot act on. Code, query member, detail and
     concerned ids stay, so the `Not answered` grammar and `parse_answer` are unchanged.
   Gap details say what is missing in an engineer's words: maintenance and procedure revisions dated on their
   own clock are `listed, but not placed before, between or after the two runs; give the date it states`. An
   engine without a catalog says `sources are cited but not re-read`; one without a text index says
   `the text was not searched: name the subjects to read instead`. Assertion kinds, `INFERRED`, clocks and
   ticks, citations, the `Sources:` list and both footers are unchanged.
6. **Answer voice.** The skill and the server instructions tell the agent to lead with a one- or two-sentence
   answer built from cited facts, then the facts, then at most three plain caveats. It never opens with
   disclaimers or repeats internal terms, still never states a cause Neptune does not state, still says what
   is unknown, and still marks inferences.
7. **No contract bump.** The packet schema is unchanged and no published golden holds a reworded gap detail,
   so `query-packet` stays 1.6.0 and the consumer pins do not move. Gap details are free text (ADR 0003).
   The renderer's text is Context's own (ADR 0009).

## Alternatives considered

- **Collapse in the server's renderer only.** The planner and the SDK `find` would still disagree with the
  tool. Resolution belongs in the resolver.
- **Representative by most claims, or the manifest's id.** Data-dependent (it moves as sources are added) or
  corpus-specific. The least id is stable and needs no knowledge of namespaces.
- **Join on `same_as` with `include_inferred: true`.** That lets a model's guess decide what a name means.
  `include_inferred` governs which facts are shown, never identity.
- **Make the MCP default `same_as_depth` the maximum.** Hidden widening for every query, and it would also
  follow inferred `same_as` when inferences are included. The entity line states the depth instead, and the
  agent writes it into the query, where it is visible.
- **Drop the catalog and text-index gaps.** Missingness must stay explicit. The skill tells the agent they are
  about the server, not the question.

## Consequences

- The demo agent reads ARM-3A as one robot, with the manifest cited for why, and queries it at depth 2.
- Packets of `compare_runs` and of engines without a catalog or text index change bytes (the reworded gap
  details). Persona and trail goldens change only in rendered text.
- Two same-named entities that the sources join are no longer asked about. Two that nothing joins still are.
- Revisit if Memory starts stating `same_as` across node kinds, or if groups grow beyond
  `MAX_SAME_AS_DEPTH` steps often enough that listing every id becomes the common case.
