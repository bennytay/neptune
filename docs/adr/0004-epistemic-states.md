# 0004 — Epistemic states (`Knowledge[T]`) and the field-scope rule

- Status: Accepted
- Date: 2026-10-01
- Issue: MVL-55

## Context

The most common way ingestion lies is by turning missingness into a fact. Some examples:

- A blank "defects" cell becomes "no defect".
- A missing `frame_id` becomes `""`.
- An absent unit becomes metres.
- A covariance matrix of zeros becomes "perfectly certain".

Once a value like that reaches a memory or training system, no one can tell it was never in the evidence.

`Optional[T]` / `None` cannot express the distinctions that matter. "The source says there is none" is not
"the source says nothing". Neither is "this source could never have told us" or "the question does not apply".
Epistemics was originally scheduled as an M8 hardening pass (MVL-40). The audit moved it to M1 because it
cannot be retrofitted onto a model whose fields are already bare. The audit ranks the wrapper's ergonomics as
risk #3: too heavy and it gets bypassed; too light and silent assumptions return.

## Decision

1. **`Knowledge[T]` is a closed tagged union with six states:**

   | State | Meaning | Example |
   |---|---|---|
   | `Known(value)` | the evidence asserts this value | URDF `<limit effort="30"/>` |
   | `KnownAbsent` | the evidence asserts there is no value | register column "defects" = "none", where the register defines that token |
   | `Unknown` | the evidence could have said and did not | blank register cell; calibration YAML with no units key |
   | `NotCovered` | the evidence could not have said: outside what it records | IMU message with `orientation_covariance[0] == -1` (no orientation estimate, per the message definition) |
   | `NotApplicable` | the field has no meaning for this entity | wheel count of a fixed manipulator, when the model has that field |
   | `Ambiguous(candidates)` | the evidence supports more than one reading | naive timestamp string with no zone; two conflicting serial numbers in one file |

2. **Provenance travels with the state.**
   - `Known`, `KnownAbsent` and each candidate of `Ambiguous` carry provenance (ADR 0006) pointing at the evidence
     that asserts them.
   - `Unknown` and `NotCovered` carry the transform that made the determination, plus evidence of where the
     adapter looked, when a location exists.
   - `Ambiguous` has at least two candidates, in deterministic order, and never picks a winner.

3. **Field-scope rule.** A field is `Knowledge`-wrapped **if and only if** both hold:
   - a source can legitimately fail to state it;
   - a consumer could draw a wrong conclusion from a default.

   This always includes units, clock and time-domain properties, frame and axis conventions, versions and
   software identity, calibration values, identities (tier-3 ids, serials, names used as identity) and coverage.

   Structural fields are **not** wrapped:
   - record ids and provenance itself;
   - `schema_version`;
   - references to child records;
   - collections of things the adapter decoded, such as a run's streams.

   A structural collection is an exhaustive statement of *what the adapter decoded*. Incompleteness (truncation,
   an unreadable chunk) is conveyed by `IngestFinding`s and by a wrapped coverage field on the container, never
   by wrapping the list.

4. **`None` is not a value in canonical records.** A `None`, or a JSON `null`, in a `model/` record is a bug.
   Type checking and validation reject it.

5. **Blank and default handling for adapters.**
   - A blank, empty or missing field becomes `Unknown`.
   - It becomes `KnownAbsent` only when the source or its format specification defines that blank or token to
     mean "none". The provenance then points at that definition.
   - A sentinel value defined by the format specification (for example ROS `sensor_msgs` covariance conventions)
     is mapped to the state the specification gives it, with the provenance pointing at the field.
   - Values that are not sentinels by specification are `Known`, however implausible they look. Judging
     plausibility is validation (MVL-41) or derivation, not parsing.

6. **No numeric confidence on evidence-layer values.** A deterministic parser either decoded a value or it did
   not. Uncertainty in the evidence is expressed *structurally*, through `Ambiguous`, `Unknown` or
   `NotCovered`, not as a probability.
   - Confidence scores belong to the derived layer (`derived/`, `inferred`) and to adapter `probe` results
     (ADR 0008).
   - Measurement uncertainty that the source *states*, such as a covariance or a declared accuracy, is ordinary
     `Known` data, not confidence metadata.

7. **Columnar form.** In Parquet series (ADR 0002), a wrapped column is a value column plus a dictionary-encoded
   state column. A null in the value column is permitted only where the state column is not `known`.

The serialised JSON shape (tag names, candidate layout), the Python type design and the parser guidance document
are MVL-40's to define within these rules. The shape must be a tagged form that no bare `T` can be confused with.

## Alternatives considered

- **`Optional[T]` plus a side-channel "reason" field.** It is easy to set the value and forget the reason, and
  `None` stays ambiguous at every read site. It recreates exactly the conflation we are trying to remove.
- **Wrap every field.** Maximally uniform. It makes record ids, provenance and child lists pointlessly
  indirect, pushes authors to bypass the wrapper, and gives nothing: a decoded list of streams is not an
  epistemic claim.
- **Fewer states** (for example Known / Unknown / NotApplicable). Collapsing `KnownAbsent` into `Known(None)`
  brings `None` back. Collapsing `NotCovered` into `Unknown` loses the distinction between "we should chase this
  down" and "this source can never tell us", which drives coverage reporting and evidence requests.
- **Confidence scores on every value** (as MVL-40's original deliverables suggest). A score on
  deterministically decoded evidence is either always 1.0 (noise) or invented (a silent assumption).
  Probabilistic confidence is kept for inferred records, where it means something.

## Consequences

- A blank can never become a fact without a provenance-bearing definition that says it should.
- Reading an epistemic field means handling six states. Helpers such as `.known_or_raise()` and pattern matching
  are MVL-40's responsibility to make this cheap, because the ergonomics decide whether the rule holds.
- Coverage reports ("how much of this run's calibration is known?") fall out of counting states.
- The scope rule needs judgement at the edges. MVL-1 applies it field by field, and later disagreements are
  resolved by amending the entity schema through an ADR, not by bypassing the wrapper.
- Revisit if review finds adapters routinely bypassing the wrapper, which would mean the ergonomics failed. Also
  revisit if a real source needs a state the six do not express.
