# What "as of" means

Every claim has two times, and Neptune never confuses them.

- **Valid time** is when the statement is true *in the world*: "this calibration was in force on the arm
  from 09-11 until the next one", "the AMR was in zone C3 between these two instants". It is a
  half-open interval `[start, end)` on **one named clock**: a recording's log clock, a controller's clock,
  or a civil time with a declared timescale.
- **Recorded time** is when Neptune *learned* it: the Ledger transaction that registered the evidence.
  Transactions are numbered and only go up. A claim is current from the transaction that recorded it
  (`recorded_at`) until the one that superseded it (`superseded_at`, open while it is current).

This is called *bi-temporal*. It lets you ask two different questions and get two honest answers.

## Asking "as of"

A query's `as_of` is a Ledger transaction (or `head`, the latest). The answer holds exactly the claims
that were current at that transaction, each presented as it was known then: later corrections are
masked, so nothing learned afterwards leaks in. A query's `during` is valid time, on a clock you name.

Take a manipulator cell whose evidence arrives in two hand-overs. The first registers the run logs and
the old calibration. The second, a week later, brings a maintenance work order saying the gripper
fingers were changed and the camera bracket refitted before that run.

- *As of* the first transaction, "which configuration was the arm running during the incident?" answers
  with what the team could know then, citing the first hand-over.
- *As of* `head`, the same question answers with the work order taken into account. Nothing is
  deleted: claims the new evidence replaces are superseded at the second transaction, not removed, so
  asking as of the first transaction still returns the first answer exactly.

That is what makes an incident review reproducible. A context packet records the transaction it was
answered at, so anyone can replay the answer later and get the same claims, even after newer evidence
arrives, on the same Memory **generation**. The generation is the hash of Memory's resolver
configuration (priorities, vocabulary and vocabulary version); adding a consolidator, changing a
priority or extending the vocabulary starts a new generation, and answers are never compared across
generations. An `as_of` beyond the latest
transaction is refused rather than guessed.

## Clocks are named, never coerced

Valid time is always on one clock, because robots disagree about time. A cell PC that lost its NTP sync
can stamp a recording 96.7 s later than the HMI's incident log; a legged robot's boot-time clock and its
GPS time are two different clocks. Neptune stores each instant on the clock that wrote it. A claim valid
on another clock is returned alongside the answer, never dropped and never silently shifted; it is placed
on your clock only through a named clock mapping, and through an estimated one it is labelled
`inferred`.

## Where to go next

- [Memory's reading guarantees](../packages/neptune-memory/docs/graph-schema.md#guarantees) (as-of is a
  true snapshot; one answer per `as_of`; clocks are never coerced) and
  [why they hold within one generation](../packages/neptune-memory/docs/graph-schema.md#caveat-a-resolver-configuration-is-a-store-generation)
- [Context's query language: `as_of` and `during`](../packages/neptune-context/docs/adr/0002-query-language.md)
- [Time in the canonical model](../docs/canonical-data-model.md#time-adr-0005-adr-0012-modeltimepy)
