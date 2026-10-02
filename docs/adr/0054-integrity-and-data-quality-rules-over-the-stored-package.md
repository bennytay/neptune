# 0054 — Integrity and data-quality rules run over the stored package

- Status: Accepted
- Date: 2026-10-02
- Issue: MVL-41

## Context

Adapters report what is wrong inside one source as they parse it (ADR 0008). What none of them can
see is what only shows once every source has landed: a stream whose declared count its series does
not hold, a clock that runs backwards across chunks, two declarations of one robot that disagree,
a calibration for hardware the robot no longer has, a frame named in a graph nobody declared.
Consumers must learn these before they trust the package, and must learn them from the receipt,
not by re-parsing. The checks must not become a second parser, must not guess units or limits
(non-negotiable 4), and must stay deterministic (5) and bounded on hostile input (9).

## Decision

1. **Where.** `neptune.validate` runs over a package the store has read and verified
   (`read_package`): records, series files and derived tables, never a raw source or an adapter.
   The job runs it in its `validate` phase. If it finds anything, the job stages the package
   again with the validator's `TransformRecord` and findings added (`store.assemble.amend`: tables,
   receipt and manifest rewritten, series and blobs moved, not copied) and verifies it again.
   A package with nothing to report is byte-identical to one built before this ADR.
2. **What a finding is.** An `IngestFinding` (no new record kind, no schema change), code
   `neptune.validate.<rule>`, from the transform `neptune.validate` at `VALIDATOR_VERSION`
   whose config is every rule's version and the bounds. `details.rule` is `<code>/<version>`. The
   subject is the evidence the problem is in (a field's own provenance where it has one, else its
   record's; a series row's evidence for a time fault; a whole source for a roll-up); `related`
   cites the other evidence involved; `records` names the records it qualifies. Rules read every
   finding but their own, so validating a validated package adds nothing.
3. **The rules on.** Each has a fixed category and severity:

   | Rule | Checks | Category |
   |---|---|---|
   | `source_incomplete` | per source, roll up every `corrupt` finding about its bytes; name the records read from it (a `limit` stopped an intact file and is not damage) | corrupt |
   | `count_mismatch` | a stream's `Known` declared message count against its series' rows, unless a `skipped` finding names the stream (rows left out on request, as `mcap.not_selected`) | inconsistent |
   | `time_regression` | per stream and clock, samples out of time order in source order (below), on a clock that declares itself monotonic | inconsistent |
   | `time_out_of_order` | the same, on a clock that does not declare it (`info`: an observation, not a contradiction) | inconsistent |
   | `interval_reversed` | run and stream `first > last`, calibration `valid_from > valid_until`, same clock only | inconsistent |
   | `missing_metadata` | `Unknown` in a field consumers need (stream topic, schema name, message encoding; clock resolution; transform direction), one finding per (source, kind, field) | missing |
   | `schema_conflict` | streams of one run on one topic declaring different schema names or encodings | inconsistent |
   | `row_shape_mismatch` | table rows whose cell count differs from the declared header | inconsistent |
   | `duplicate_id` | one source stating one logical id for two records of a kind | ambiguous |
   | `id_conflict` | records with one logical id stating different values for an attribute (model, name, run machine); never a merge | inconsistent |
   | `dangling_reference` | a record naming a run, clock, table, document, configuration or transform record (`frame_transform`) the package lacks | missing |
   | `frame_unresolved` | a frame named in a missing graph, or in a graph no frame or transform declares it in | missing |
   | `calibration_revision_mismatch` | a calibration's hardware revision is none its machine's configurations declare | inconsistent |
   | `calibration_out_of_window` | a run of the machine lies outside every stated window of one calibrated subject (after the last `valid_until`, before the first `valid_from`; an open end covers all), same clock | inconsistent |
   | `software_conflict` | one release of named software declared with two commits, one build with two digests, one commit with two builds | inconsistent |

   *Time order.* A series is sorted by clock 0, then `seq` (source order). Clock 0 is in order
   exactly when `seq` rises through the file; `descents` counts the falls of `seq` among rows with
   a known clock 0, and the finding cites the sample written later and stamped earlier. Other
   clocks are judged in file order only when file order is source order, else left unjudged
   (`other_clocks: not_judged`). Equal ticks and unknown cells are never a fault. Whether the
   clock declares itself monotonic is reported (`declared_monotonic`), not required.
4. **The rules off.** Three rules read inputs no record kind on main carries. Each is written
   against a `Protocol` (`neptune.validate.pending`), tested, and off; the report lists it as not
   covered with its reason, and it runs when its input is supplied (`Inputs`). When the kind
   lands, its issue maps records onto the Protocol and adds the rule to the default set.
   `declared_limit_exceeded` (series values beyond a limit the evidence declares, compared only
   when the limit's and the column's units are both known and equal: no unit assumed or
   converted; waits for joint and actuator limit kinds); `stale_document_revision` (a document
   revision another revision in the package supersedes; waits for document revision kinds);
   `run_software_conflict` (a run bound to configurations that disagree; waits for MVL-38).
   "Impossible units" need no rule: the model already refuses a unit of the wrong dimension.
5. **Severity.** The ranking is the receipt's fixed order `error > warning > info`
   (`model.package.SEVERITY_ORDER`), and each rule has one severity, judged by ADR 0017's
   meaning: validation never loses evidence, so no rule is `error` (that stays with the producer
   that lost it; the roll-up names it). A rule is `warning` when values in the output are in
   doubt, and `info` when it records an observation that contradicts nothing:
   `time_out_of_order` (a clock that never claimed an order, such as MCAP `log_time`, which writers
   may interleave) and the cap notice. A clock that declares itself monotonic and is not gives
   `time_regression`, a `warning`.
6. **Bounds.** Every rule is linear or `n log n` in the records it reads (grouping by sorted
   keys; calibrations against runs by bisection); series are read `batch_rows` (65,536) rows at a
   time, `seq` and time columns only, and one row group to cite a row. Output is capped:
   `findings_per_rule` 256, `records_per_finding` 256, `related_per_finding` 16,
   `values_per_detail` 16. A cut is counted in the finding (`records_omitted`,
   `related_omitted`) or reported once per rule as `neptune.validate.findings_capped` (info).
   Bounds are in the transform's config, so changing them is a new lineage. A rule that raises
   costs nothing else: its drafts are dropped, `neptune.validate.rule_failed` (failed, warning)
   names it and the exception's class (never its text), and the other rules run. Messages are
   forced to one printable line of at most 1,000 characters; the facts stay whole in `details`.

## Alternatives considered

- **Checks inside adapters.** Each sees one source; none of the cross-source checks are possible,
  and every adapter would repeat the rest. Rejected by the architecture (validate is a stage).
- **Checks before staging, over the workspace's chunks.** Avoids restaging, but validates
  something other than what consumers read; a package-level check is the contract.
- **A new `validation_report` record or receipt field.** A schema change while schema bumps are
  queued; `IngestFinding` already carries code, severity, evidence and records, and the receipt
  already lists and ranks findings.
- **Report off rules in the package** (a `not_covered` finding each). Every package would change
  and carry noise about Neptune rather than the evidence; coverage is the job event's
  (`package_verified.validation`) and the report's.
- **Range checks against physical limits** (latitude, joint travel). Needs a unit and a limit;
  without a declared limit it is a guess, so it waits for limit kinds.

## Consequences

- The receipt now says what is wrong across sources, ranked and cited, before anything downstream
  reads the package; the platform harness's pinned findings for a truncated recording gain the
  roll-up (`neptune.validate.source_incomplete`).
- A package with findings costs two more passes over its series and blobs (amend hashes them
  again and verifies the whole before moving them); reusing the manifest's hashes is a later
  optimisation, not a contract.
- The receipt lists `neptune.validate` among the transforms that read a source it cites, as it
  already lists the runtime and discovery for theirs: a finding's citation counts as a read.
- Adding a rule, or changing one's logic, bumps its version and so the validator transform:
  packages with its findings get a new lineage; packages without stay identical.
- Revisit when the limit, document-revision or run-binding kinds land (turn the pending rules
  on), when alignment (M7) produces clock mappings (time checks across clocks), or if `warning`
  proves too coarse for consumers.
