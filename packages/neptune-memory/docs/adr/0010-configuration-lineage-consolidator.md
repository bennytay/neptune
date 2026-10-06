# 0010 — The configuration lineage consolidator

- Status: Accepted; §2's `succeeds` at each chain change and §6's reading of `succeeds` superseded by 0019 §2; §3's `configuration_unknown` for a bound `configuration_snapshot` no thread holds superseded by 0022, itself superseded by 0024
- Date: 2026-10-05
- Issue: MVL-127

## Context

"What changed before the incident" is a question about configuration: which configuration a machine was in, which
one a run ran with, and whether either was authorised. The compiler states each piece separately and as declared:
lifecycle records (root ADR 0051) place a configuration id on machines at one instant on the record's clock
(`commissioned`, `performed`, `effective`); `SnapshotBinding` (root ADR 0050 §8) binds a run to a snapshot record
over a window of the run's own clock; `AuthorisationEnvelope` approves a configuration at a site over
`[valid_from, valid_until)`. `HardwareConfiguration` and `SoftwareConfiguration` state no time at all.

Getting this wrong is worse than saying nothing: filling a run's configuration from the nearest commissioning
record, ordering two same-day changes by record id, or bridging a maintenance gap would each state a configuration
the evidence never did, and the incident review would rest on it. Claim objects cannot be `Unknown` or `Ambiguous`
(ADR 0002 §2), and two clocks are never compared (ADR 0002 §3).

## Decision

### 1. Reading, and which node a record names

`consolidate/configuration_records.py` parses with the compiler's strict readers (`commissioning_baseline`,
`maintenance_event`, `change_record`, `requalification_record`, `authorisation_envelope`, `run`,
`snapshot_binding`, the four snapshot kinds of `SnapshotKind`, `timestamp_domain`); `consolidate/configuration.py`
(`memory.configuration`, version `1`, no configuration) decides. A refused record is
`configuration.malformed_record`, an inferred one `configuration.inferred_record`, one id with two contents
`configuration.record_conflict` (neither used). A field that is not `Known` keeps its state: `known`, `ambiguous`
(every candidate), `unknown` (`Unknown`, `NotCovered`) or `absent` (`KnownAbsent`, `NotApplicable`).

Nodes are the identity consolidator's, read through `identity.node_threads` (ADR 0008 §4), so nothing is keyed
twice. A declared id names `node_ref(type, id)` only if a Ledger thread declares it; otherwise
`configuration.unthreaded_id` and nothing is placed. A record that declares no id (a `Run` without a `Known`
`logical_id`, every snapshot record) names the node whose thread cites the record's own record-level evidence ref:
the Ledger's anchored-thread key (Ledger ADR 0003 §2), expressed through the `ledger_thread` stand-in's `evidence`.
None is `unthreaded_id`; several is `configuration.ambiguous_anchor`. Instants on a clock that declares itself
civil are placed on that `CivilClock`, as identity does (ADR 0008 §2).

### 2. Machine chains

Each lifecycle record with a `Known` instant places its configuration on each `Known` machine it names. A record
with no instant is `configuration.untimed_record`; one with no `Known` machine (or with machine entries that are not
`Known`) is `configuration.unplaced_record` for what it cannot place. An `absent` configuration places nothing: the
record states it is bound to none. Per machine, steps are grouped by clock; a machine with steps on several clocks
is `configuration.clock_split` and each clock is its own chain. On one clock, steps are ordered by instant, and the
records at one instant are read together:

- all `known` and equal: **decided**;
- all `unknown` (or `known` with no thread): **unknown**;
- anything else (two configurations, `known` beside `unknown`, an `Ambiguous` field): **candidates**, every reading,
  and `configuration.chain_overlap` when several records disagree. Record order never breaks a tie.

Consecutive instants in the same state are one span, from its first instant to the next span's (or `open`). A
decided span is `has_configuration(machine → configuration)`; at each decided-to-decided change the successor
`succeeds` its predecessor, valid over the successor's span, citing the records on both sides of the change. An
unknown span is `configuration_unknown(machine → record)` and `configuration.chain_gap`; a candidate span is one
`configuration_candidate` per reading, each citing the records naming it. No `succeeds` is claimed across an
unknown or candidate span: whatever happened in it is not stated. All chain claims are `stated`, as the records are.
A requalification naming the configuration already in force extends its span and adds its evidence.

`HardwareConfiguration` and `SoftwareConfiguration` records state no instant, so they place nothing on a chain;
they are what bindings name, and their configuration node is found by anchor (§1). Machine chains (civil time) and
run claims (run clocks) meet at the configuration node, never by comparing clocks.

### 3. Runs

For each `Run` with a node, each `SnapshotBinding` naming it gives `configuration_active_during(run →
configuration)` over the bound window: the binding's `Known` bounds, else the run's own `[first, last + 1 tick)`
(`last` is inclusive and ticks are integers, so this is exact), else the run thread's start (a convention, ADR 0008
§2) and `open`. `assertion_kind` is the binding's. Each bound keeps its state: stated (`Known`), stated open
(`KnownAbsent`: the run's own bound) or unstated (`Unknown`, `NotCovered`, `Ambiguous`). The claim names the run
either way, but a window is **stated** only if both bounds are stated or stated open and, for a stated-open start,
the run states `first`; coverage (§5) is decided over stated windows only. The compiler's own stated bindings carry
`Unknown` validity (root `derived/bindings.py`), so their coverage is undecided until a source states the window. An
`Ambiguous` validity gives one `configuration_candidate` per reading and `configuration.ambiguous_window`, and
decides no coverage. Bindings of one snapshot kind naming different configurations
over overlapping windows are `configuration.binding_overlap`, and each becomes a `configuration_candidate`. A
binding naming a run or snapshot the Ledger does not hold is `configuration.dangling_binding`; a snapshot with no
configuration thread leaves the window `configuration_unknown(run → binding record)`. A window whose end is not
after its start on one clock (a binding starting after its run ends, a run whose `last` precedes its `first`) is
`configuration.untimeable_window`; for such a run, the end is not placed (`open`). A run no binding names is
`configuration_unknown(run → run record)`, `observed`, over the run: never the nearest configuration in time.

### 4. Missingness is the predicate

As for identity (ADR 0003 §1.3): `configuration_candidate` claims are the `Ambiguous` readings, one per candidate
with its own evidence; `configuration_unknown` is the `Unknown`, with the record that leaves it open as its object.
Before a machine's first placement nothing is claimed: `NotCovered`, as for any node the graph does not describe.

### 5. Authorisation

Each envelope with a `Known` configuration, `Known` site and `valid_from` gives `authorised_configuration(site →
configuration)` over `[valid_from, valid_until)` (`valid_until` is the exclusive end, as a validity window's is;
`open` only where the envelope states it has none, `KnownAbsent`). What it cannot place is
`configuration.envelope_unplaced` (or `untimeable_window`); a `valid_until` that is not stated (`Unknown`,
`Ambiguous`, `NotCovered`) is `envelope_unplaced` and no site claim: an unstated end is never "until further
notice".

For each `configuration_active_during`, the parts of the bound window that no envelope naming its configuration
covers are `not_covered_by_authorisation(run → configuration)`, `observed`: a fact about the envelopes in the
Ledger, not a judgement of the run. It is decided only on one clock, and never from a blank. It is
`configuration.authorisation_undecided`, with no claim, when an envelope naming the configuration cannot be
compared (another clock, no `valid_from`); when an envelope that might cover it (an `Ambiguous` one including it, one
whose configuration is not `Known`, or one naming it with no stated `valid_until`, from its `valid_from` on) cannot
be compared or overlaps an uncovered part; when the bound window is not stated (§3); or when an `open` run
window is only partly covered (the run may end before or after the envelope does, so only a run uncovered over all
of its window is surely uncovered). Coverage is by configuration and time; site and machine scope wait for MVL-131's
run placement.

### 6. Vocabulary and contract

`succeeds`, `configuration_active_during`, `configuration_candidate`, `configuration_unknown`,
`authorised_configuration` and `not_covered_by_authorisation` join `CORE_PREDICATES`, all `many` (a configuration
can succeed different ones on different machines; a run runs hardware and software configurations at once);
chain spans reuse `has_configuration`. `VOCABULARY_VERSION = 4`, so the resolver generation changes (ADR 0006 §7).
The exported schema names the vocabulary version in an annotation (never a bound, so newer vocabularies still
validate), which makes a vocabulary change a schema change the registry publishes. graph-schema **1.2.0** is a
minor release: the 1.0.0 and 1.1.0 goldens validate and pass the suite. The golden graph's plan is unchanged.

## Alternatives considered

- **Nearest record in time for a run without a binding.** The guess the issue forbids; a run on a machine with a
  change the same morning would be assigned either configuration by accident of clocks.
- **Order same-instant records by record id or kind.** Record ids are hashes; a change and a rollback on one day
  would be ordered by chance.
- **`one` cardinality for `succeeds` or `configuration_active_during`.** The resolver would supersede a legitimate
  second reading (another machine's chain, a software beside a hardware binding) instead of keeping both.
- **Key snapshot configurations by record id.** Record ids change with a parser upgrade (root ADR 0062 §5); the
  Ledger keys configuration threads by evidence anchor, and so does this.
- **Convert run clocks to civil time to check coverage.** Clock mapping is MVL-130's; until its claims exist,
  comparing across clocks would be a silent assumption.
- **Bound `vocabulary_version` in the schema.** A consumer validating a newer graph against its pinned minor would
  fail; an annotation publishes the change without refusing anything.

## Consequences

- Context and Deploy can answer "what configuration, since when, authorised?" from claims, with every gap,
  tie and undecided comparison visible as a finding or an explicit claim.
- When MVL-130 publishes clock mappings, coverage across clocks moves from findings to claims (a new version of this
  consolidator); when MVL-131 places runs at sites and on machines, coverage gains site scope.
- When Memory reads the catalog API (MVL-85), anchored nodes come from `ThreadKey`s instead of the stand-in's
  `evidence`; the rule does not change.
