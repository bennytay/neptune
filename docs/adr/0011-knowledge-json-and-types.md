# 0011 — `Knowledge[T]`: JSON shape, Python types, provenance slot

- Status: Accepted
- Date: 2026-10-01
- Issue: MVL-40

## Context

ADR 0004 fixed the six epistemic states, the field-scope rule and the blank-handling rules. It left three
things to MVL-40: the JSON shape, the Python types and helpers, and the parser guidance. Every
`Knowledge`-wrapped field in every package is written in this shape, so it is a long-lived contract.

One ordering constraint shapes the design. MVL-40 lands before MVL-3, which defines `Provenance`, yet
ADR 0004 §2 says states carry provenance.

## Decision

1. **Python types** live in `neptune.model.knowledge`. There is one frozen dataclass per state: `Known[T]`,
   `KnownAbsent`, `Unknown`, `NotCovered`, `NotApplicable` and `Ambiguous[T]` (with `Candidate[T]`).
   `Knowledge[T]` is their union. Consumers pattern-match, or call `known_or_raise()` where any other state
   is a bug. `map(fn)` transforms values and passes the other states through unchanged. Each state exposes
   `.state` as a `KnowledgeState` tag, and the same tags are the Parquet state-column values (ADR 0004 §7).
2. **Provenance slot.** `Known`, `Unknown`, `NotCovered` and each `Candidate` hold `provenance: Grounding |
   Inherited`. `INHERITED`, the default, means "the enclosing record's provenance" (ADR 0006 §1) and is
   omitted from JSON. `Grounding` is a protocol: anything with `to_json()`. MVL-3's `Provenance` implements it,
   so nothing here changes when MVL-3 lands. `NotApplicable` carries no provenance, because it is a schema fact.
3. **`KnownAbsent` requires explicit provenance.** It cannot be `INHERITED`. It must point at whatever makes
   the blank or token mean "none". A blank therefore cannot become an absence without a citation, and that
   check is made at construction.
4. **Value invariants.** `Known` and `Candidate` reject `None`, NaN, ±Infinity and nested states. `Ambiguous`
   needs at least two candidates with distinct values. The candidates stay in evidence order, and no winner is
   ever chosen.
5. **JSON shape.** The discriminator key is `"knowledge"`, with the tag as its value. Examples:

   ```
   {"knowledge":"known","value":30}
   {"knowledge":"known","provenance":{…},"value":"m"}
   {"knowledge":"known_absent","provenance":{…}}
   {"knowledge":"unknown"}                       // optional "provenance"
   {"knowledge":"not_covered"}                   // optional "provenance"
   {"knowledge":"not_applicable"}
   {"knowledge":"ambiguous","candidates":[{"value":…,"provenance":{…}},{"value":…}]}
   ```

   `from_json` is strict: an unknown tag, an unexpected key (a `"confidence"` key, for example) or a missing
   field raises an error. Values are encoded and decoded by caller-supplied functions, so `T` can be any
   canonical type.
6. **Adapter helper.** `from_text(raw, parse, absent_tokens=…)` encodes ADR 0004 §5 for textual fields:
   - blank ⇒ `Unknown`;
   - an exact match with a token the source defines ⇒ `KnownAbsent`, grounded in that definition;
   - anything else ⇒ `Known(parse(raw))`.

   Parse errors propagate, because an unparseable value is a finding, not a state.
7. **No confidence metadata** on evidence-layer states, as ADR 0004 §6 decides. The strict parser enforces this.

## Alternatives considered

- **Tag key `"state"` or `"type"`.** These are common words, likely to collide with a value object's own keys
  and to be misread as domain fields. `"knowledge"` says what the object is.
- **Optional provenance as `None`.** `None` in canonical records is a bug (ADR 0004 §4). An explicit
  `INHERITED` sentinel keeps "inherits the record's provenance" visible at the call site.
- **Sort `Ambiguous` candidates canonically.** It is deterministic too, but it throws away the order in which
  the evidence presented them, which is itself information. Parsing is deterministic, so evidence order is
  stable.
- **Defining a provisional `Provenance` class here.** It would pre-empt MVL-3's shape. The protocol costs
  nothing and lets MVL-3 decide.

## Consequences

- An adapter author cannot write "blank ⇒ none" without citing a definition.
- Reading a wrapped field is one `match` or one `known_or_raise()`.
- MVL-3 must give `Provenance` a `to_json()`, and MVL-1 applies the scope rule field by field using these
  types.
- Changing a tag name or the discriminator key re-lineages every record that holds a wrapped field, so it
  needs a new ADR.
