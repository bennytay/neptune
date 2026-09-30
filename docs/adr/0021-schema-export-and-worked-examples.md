# 0021 — The canonical JSON Schema and the worked examples

- Status: Accepted
- Date: 2026-10-01
- Issue: MVL-70 (sub-issue of MVL-1)

## Context

ADR 0017 §1 made the Python types the source of truth and promised a language-neutral JSON
Schema generated from them. A consumer outside Python (a memory learner, a retrieval service, a
data engineer with `jq`) needs a contract it can validate against without reading Python. The
model is frozen at the M1 gate, so the schema has to be complete and exact enough by then.

MVL-1's acceptance also asks for worked examples of four platforms that validate against the
schema and resolve back to evidence. Examples made of invented bytes would only show that records
can be constructed. They would not show that citations land on real bytes.

## Decision

1. **Generated, committed, drift-checked.** `neptune.model.schema` walks every record kind's
   dataclass fields and type hints and writes JSON Schema draft 2020-12 to
   `docs/schema/canonical.schema.json` (`make schema`). A unit test fails when the committed file
   differs from what the model generates, so a model change and its schema change land together.
   - Field names are JSON keys. The few types whose JSON has another shape (location tags,
     record-range ticks, a header-less cell) are written out beside the generator, and tests
     validate every shape `to_json` produces.
   - `Knowledge[T]` becomes one definition per value type, tagged by `knowledge`.
   - `$id` is `urn:neptune:schema:canonical:<SCHEMA_VERSION>`. The file changes whenever the
     schema version does.
   - Every record kind in `neptune.model` must be in the schema; a test finds them by their
     `family`.
2. **The schema checks shape; the readers check everything.** The schema checks keys, types,
   tags, enums and id syntax. The Python readers also check ordering, uniqueness, ranges,
   non-empty text and cross-field rules, and they tell `1` from `1.0`, which JSON Schema cannot.
   So a line a reader accepts always passes the schema, while a line the schema accepts may still
   be refused by a reader.
3. **Worked examples on real bytes.** `tests/fixtures/model/` holds four examples: a drone (PX4
   ULog), a quadruped (ROS 2 bag, URDF and mesh), a manipulator (MCAP and a hand-eye calibration)
   and a mobile robot (ROS 1 bag, a site register and a photo).
   - Each example's sources are real files, generated deterministically by small writers and
     checked once against each format's official reader.
   - Its records are golden JSON Lines, one table per kind sorted by id, with the ledger beside
     them. A builder plays the future adapters by the rules of ADRs 0017 to 0020.
   - An integration test checks that every golden line validates against the schema and reads
     back byte-identically. It resolves every citation: artifacts hash to the committed files,
     byte ranges land on real records, and pointers, rows and cells resolve. Every id a record
     names must exist in the example.
4. **Test-only dependency.** `jsonschema` (and its type stubs) joins the dev group as an
   independent validator. Nothing in `src/` depends on it.

## Alternatives considered

- **A hand-written schema.** It would drift from the types the first time someone forgot it, and
  ADR 0017 already decided the direction of generation.
- **A schema that encodes every reader rule** (sorted keys, non-empty strings, cross-field
  checks). Much of it cannot be expressed in JSON Schema, and the rest would double the generator
  for a contract the readers already enforce exactly.
- **Examples built from invented content ids.** Cheaper, but they could not show that a citation
  resolves, which is the acceptance.
- **A home-grown validator in the tests.** An independent implementation of the standard is the
  point of validating against a schema.
- **Examples as one package per robot, with a manifest and receipt.** The package layout and the
  receipt are MVL-5's. The record tables here are what a package will contain.

## Consequences

- Non-Python consumers can validate any record line against a committed, versioned file.
- A model change that alters a JSON shape fails the drift test until `make schema` is rerun and
  committed. The schema's diff then shows the contract change to reviewers.
- The examples are a regression net for the whole model: any change to ids, shapes or rules shows
  up as a golden diff, which ADR 0003 requires to be explained.
- When M4 to M6 adapters land, their outputs can be compared with these examples. Where a real
  adapter decides differently, the example changes in the same PR.
- Revisit if consumers need the schema split per record kind, or published beyond the repository.
