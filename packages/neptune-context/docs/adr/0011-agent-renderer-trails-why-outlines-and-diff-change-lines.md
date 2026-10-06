# 0011 — Agent renderer trails: why outlines and diff change lines, with a checked grammar

- Status: Accepted
- Date: 2026-10-06
- Issue: MVL-149

## Context

ADR 0010 put the structure of a `why` or `diff` answer in the packet (`trails`, `query-packet` 1.2.0), and
rendered it for people. The agent renderer (ADR 0009) ignored it: `neptune_why` returned the root claim as
one fact and `neptune_diff` a flat list of items with no sign of what opened, closed or was superseded.
That is the Demo v1 question ("why did the arm-cell incident happen, what changed"), and it must come back
cited. Three facts shape the grammar:

- ADR 0009 refuses any line that states something without an `[I..][E..]` citation. A why step whose claim
  the packet cannot carry (beyond the pin, never current, cut by the budget) has no item to cite, only the
  evidence on the step. A diff change names claims by id and carries no evidence at all, so an older version
  or a claim cut by the budget has nothing to cite.
- Memory states relations (equal assertions, resolver findings, candidate readings, `supersedes`
  links, intervals), never a cause. A renderer that writes "because" invents one.
- Trail text includes node ids and values from sources, so the injection rules of ADR 0009 §3 apply.

## Decision

1. **Where.** Trail sections follow "What changed since transaction M" and precede **Facts**; Facts stay
   complete, one sentence per item. A packet without trails renders byte for byte as before. The evidence
   footer lists the items' refs first, then those only a why step names (`answer_evidence_refs`), so
   existing keys never move and the MCP resource links cover every cited source. This **amends ADR 0003
   §7**: the property `parse_citations(render(p)) == p.evidence_refs()` becomes "starts with
   `p.evidence_refs()`, then the why steps' refs" for an answer with why trails; `render_text`, which
   renders no trails, is unchanged, and so is every answer without trails.
2. **Why outline.** Heading `Why Memory holds <claim id> (explain clause i, as Memory knew it at
   transaction N; K claims, R repeated, G gaps listed under Not answered):`, then one line per step in the
   packet's pre-order, indented two spaces a level: `- <label>: <sentence>. <citations>`.
   - Labels: `Root claim`, `Corroborated by`, `Conflicts with (resolver finding <id>)`, `Alternative reading`.
   - A claim the packet carries reads like its fact: `Observed:` / `Stated:` / `INFERRED (model ...,
     confidence ...)`, the claim in words, then `[I<n>]` and its evidence keys.
   - A claim it does not carry reads `<mark>: <claim id>, whose content is not in this packet.` and cites
     only the step's evidence keys; an inferred one says model and confidence are not in the packet.
   - A repeat says `already shown above and not expanded again` (carried: `item I<n> is ...`).
   - `G` counts the packet's gaps at the clause's pointer, so a cut tree (depth, fan-out, steps, budget,
     inference withheld) says so in its heading and the gaps are under **Not answered**.
3. **Diff lines.** Heading `What changed about <node>[, with its declared identities ...] between <point>
   and <point> (explain clause i; K claims, G gaps ...):`, then `Predicate <p>:` groups, each
   in the order opened, closed, superseded, between.
   - `Opened`, `Closed`, `Superseded` and `Between` are cited sentences of the claim. `Closed` and
     `Superseded` name the earlier version and indent `Narrowed to` or `Replaced by` lines under it. A claim
     that nothing replaced says `Memory no longer holds it at the later transaction` (or `it no longer holds
     at the later instant`).
   - `Between` says `held only between the two points, at neither of them`, always.
   - A diff claim the packet does not carry is a fixed-form **named-only** line: `<label>: <claim id> is
     not carried in this packet, so its content and its evidence are not here[; <note>][; neptune_why with
     as_of N reads it].` It carries no citation because the packet holds no evidence for it. `N` is the
     transaction the diff compared the claim at (or the packet's snapshot on a world-time diff); a version
     that opened and closed in between was current at no transaction and gets no `as_of`.
   - An empty diff is its heading with `0 claims`. The heading states no change count: an opened change
     naming several claims renders several lines, so the count could not be checked.
4. **No cause, no reading.** Sentences are the claims' own words plus the labels above; the renderer adds
   no adjective, no "because", no count beyond the heading's.
5. **The grammar is checked.** `parse_answer` accepts, in a trail section, only:
   - the two heading forms and `Predicate <token>:`;
   - a cited line (one `[I]` key, which must be a claim item, then `[E]` keys, all in the footers);
   - an evidence-only line of the uncarried why-step form (`[E]` keys only);
   - a named-only diff line of exactly the form above.

   It also checks, reading each line with its quoted strings removed so source text cannot satisfy a check:
   - the outline's shape: one root first, one level deeper at a time, a claim shown in full once, a
     repeat naming a claim shown earlier and the item it cites, nothing under a repeat;
   - the diff's shape: `Narrowed to` only under `Closed`, `Narrowed to` or `Replaced by` under `Superseded`
     (Memory's closure of the old claim is one of the replacing versions), both only within their own
     predicate group, and a closed or superseded line saying nothing replaced it exactly when it has none
     beneath it;
   - the note ending a diff sentence: one per label and axis (`held only between the two points, at neither
     of them` only on `Between`, `no longer holds` wording only on `Closed` and `Superseded` and by the
     diff's axis), and the `neptune_why` transaction of a named-only line: the diff's own transaction for
     its side, the packet's snapshot on a world-time diff, none on a transaction-diff `Between`;
   - that a conflict names its finding, that a trail section comes before Facts, and that each
     heading's claim, repeat and gap counts match the lines and the gaps. It returns
   `trail_lines`, one `TrailLine` per line with claim, label, depth, evidence, finding, predicate and
   repeat, so the tested property extends ADR 0009's: `parse_answer(render_answer(p))` recovers every claim,
   relation, depth, finding and evidence ref of every trail of `p`, in order.
6. **Untrusted text** goes through `quote` as before (node ids, claim values). The only unquoted
   tokens are validated by the packet: ids, predicate tokens, node types.

## Alternatives considered

- **Cite named-only claims with the diff's other evidence.** That would attach a source to a change it did
  not come from, against "no information not in the packet". Lost: a fixed-form line with no citation.
- **Drop claims the packet does not carry from the answer.** The diff would silently omit changes and the
  gap would be the only trace. Lost: gaps stay visible and so do the claim ids.
- **Render the Markdown form for agents.** Links and code spans carry no citation keys and Markdown is live
  syntax in a model's context. Lost.
- **Replace Facts with the trail.** Keeps one sentence per claim, but breaks the ADR 0009 property (fact `n`
  cites `I<n>`) and every packet without trails. Lost; Facts stay and the trail is the structure.
- **Say "because" for corroboration.** Memory states agreement, not support or cause. Lost.

## Consequences

- `neptune_why` and `neptune_diff` return an outline and change lines instead of a flat list; the demo
  transcript records them byte for byte (`tests/golden/agent/transcript-arm-cell.json`), and its diff step
  asks `max_items: 20` so the twelve claims it names are all carried.
- A trail line is longer than a fact because a claim is described again where it is placed. The server
  instructions and the skill say a named-only line means "ask for a larger `max_items`".
- The grammar has two exceptions to "every statement cites an item and a source" (an uncarried why step,
  a named-only change), each a fixed form that adds no fact.
- Revisit when Memory publishes support relations or a by-id read that lets a diff carry old versions, or
  when a trail grows past a page.
