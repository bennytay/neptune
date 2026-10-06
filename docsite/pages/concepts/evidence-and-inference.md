# Evidence and inference are kept apart

Every value Neptune produces says how it is known. There are three answers, and they are never mixed.

| `assertion_kind` | Meaning | Examples |
|---|---|---|
| **observed** | Decoded or measured directly from the source. | A joint-torque message in an arm's MCAP recording; the number of samples a humanoid's IMU stream holds; the time range a marine vehicle's sonar log covers. |
| **stated** | The source explicitly says it about something else. | A URDF declares a legged robot's joint limits; a CMMS work order says the gripper fingers were changed; an asset register says which AMR is at which site; an engineer's assertion says two stops are the same event. |
| **inferred** | Produced by a model or a heuristic. | "This topic looks like an IMU"; a caption for a camera frame; an embedding; a clock mapping estimated from paired readings. |

## Why the split matters

- **Evidence is stable, inference is not.** An observed or stated value changes only when its parser
  changes, and then as new lineage beside the old. An inference can be regenerated, with a better model,
  at any time. Mixing them would let a model update silently rewrite what the evidence says.
- **Different questions need different trust.** A safety review, an incident reconstruction or an auditor
  asks for evidence only. Every query can exclude inferred claims (`include_inferred=False`) and gets
  back exactly the observed and stated ones.
- **An inference must point at what it is about.** An inferred claim names its model and cites the
  evidence it read; evidence never points at an inference.
- **Uncertainty stays honest.** Evidence carries no confidence score: where a source is unclear the value
  is *ambiguous* (with every reading), *unknown* or *not covered*. Only an inferred claim carries a
  probability.

## How the code enforces it

The split is a package boundary, not a naming convention.

- In the compiler, canonical records under `model/` accept only `observed` and `stated`; anything a
  model or heuristic produces lives under `derived/` with its own provenance type, and the type checker
  and the runtime both refuse it on a canonical value.
- In Memory, the consolidators that turn records into claims are deterministic and import no model code;
  inference lives in Memory's own `derived/`.
- In Context, anything placed on a clock through an *estimated* clock mapping is labelled `inferred` in
  the answer, and every packet item carries its `assertion_kind` through to what an agent renders.

## Where to go next

- [Architecture: two kinds of output, kept apart](../docs/architecture.md#two-kinds-of-output-kept-apart)
- [The provenance record](../docs/provenance-and-identity.md#provenance-record-modelprovenancepy-adr-0016) and
  [epistemic states](../docs/canonical-data-model.md#epistemic-states--knowledget-adr-0004-adr-0011-modelknowledgepy)
- [What a claim is](claims.md)
