# What a claim is

A **claim** is one statement about the world, with its evidence attached:

> *subject* — *predicate* — *object*, valid over *[start, end)* on one named clock,
> recorded at Ledger transaction *n*, asserted as *observed*, *stated* or *inferred*,
> because of *this evidence*, produced by *this transform*.

For example: run `pallet_2026-09-14` (subject) was recorded by (predicate) machine `ARM-3A` (object). Or:
the arm-cell camera was calibrated with configuration `CAL-ARM3A-0911`, from the time that calibration
starts until the next one. Or: a quadruped's patrol stream has a `gap`: its declared first or last
instant says samples should be there, and the recorded series holds none.

## The parts

| Part | What it holds |
|---|---|
| subject, object | Nodes: a machine, sensor, site, zone, asset, run, stream, configuration, event, document… An object can also be a value (text, a quantity with its unit as declared, an instant on its own clock) or a pointer to a Ledger record. |
| predicate | A word from a registered vocabulary (`recorded_by`, `has_configuration`, `in_zone`, `same_as`…). Each says whether a subject can hold one object at a time or many. |
| valid | When the statement is true in the world: half-open `[start, end)` on **one** clock. The end may be open. |
| recorded_at, superseded_at | When Neptune learned it, and when a later version replaced it: Ledger transactions. See [what "as of" means](as-of.md). |
| assertion_kind | `observed`, `stated` or `inferred`. See [evidence and inference](evidence-and-inference.md). |
| confidence | `not_applicable` for observed and stated claims, because they come from deterministic code. Only an inferred claim carries a probability, or says it is unknown. |
| provenance | The evidence (a source file and a locator down to the message, row, span or field), the Ledger records, and the transform: consolidator id, version and config hash. An inferred claim also names its model. |

A claim's id hashes its content, so the same evidence through the same code gives the same claim every
time.

## What claims never do

- **They are never edited or deleted.** A new version supersedes the old one and lists it in
  `supersedes`; the old version stays in the history, and a query as of a transaction before the change still sees it.
- **They never merge identities.** Two identical URDFs are not the same robot. "The CMMS stop and the
  controller's stop are the same event" is a `same_as` edge that queries follow when asked. It is drawn
  only from evidence (a declared identifier, configuration lineage, or a person's `same_identity`
  assertion), never from a model's guess.
- **They never fill a blank.** If no configuration is stated for a run, the graph says so
  (`configuration_unknown`) instead of borrowing the nearest one. Missing evidence is a value of its own:
  known, known absent, unknown, not covered, not applicable or ambiguous.
- **They never compare instants on different clocks** unless a named clock mapping relates them. A cell
  PC whose clock runs 96.7 s ahead of the HMI's does not get silently corrected.

## Where to go next

- The full claim and finding model: [Memory graph schema](../packages/neptune-memory/docs/graph-schema.md#the-claim-and-the-finding)
  and its [JSON Schema](../contracts/graph-schema/index.md).
- What Memory guarantees about the graph as a whole: [Memory's guarantees](../packages/neptune-memory/docs/guarantees.md).
- Where the evidence comes from: the compiler's [canonical data model](../docs/canonical-data-model.md) and
  [provenance and identity](../docs/provenance-and-identity.md).
