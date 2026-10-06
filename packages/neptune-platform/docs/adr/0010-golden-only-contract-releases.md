# 0010 — Golden-only contract releases for an unchanged schema

- Status: Accepted
- Date: 2026-10-06
- Issue: MVL-195

## Context

ADR 0002 §7 makes `scripts/contracts.py bump` refuse when neither the owner's exported schema nor its
version constant changed, and its Consequences say published goldens stay as they were when the owner's
examples change without a schema change. That held while goldens only drifted cosmetically. It no longer
holds once an owner's output changes lineage without changing shape: Memory's consolidator version bump
(root AGENTS.md non-negotiable 6, parser upgrades create new lineage) changes every claim id in the
`graph-schema` 2.0.0 goldens, while the schema stays byte-identical. The published 2.0.0 goldens are
immutable, so they now disagree with what the owner writes, and the owner has no way to publish the new
ones. Consumers that test against the registry's goldens (Context, Deploy) need the new ids, and they need
to see that the shape did not change, so they can raise their lock without re-reading the schema.

## Decision

1. **`bump <contract> <version> --golden-only`** publishes a new version whose schema is the latest
   version's schema and whose goldens come from the owner's generator. It refuses when:
   - the contract has no published version;
   - the version is a new major over the latest version (a golden-only release is a minor or patch);
   - the owner's exported schema is not byte-identical, in canonical form, to the latest version's
     `schema.json` (that is a normal `bump`);
   - the generated goldens equal the latest version's goldens, by name, pointer and canonical bytes
     (nothing to release).

   Every other `bump` rule still applies: the version must be newer, the constant mapping holds (an
   integer constant equals the major; a string constant equals the version, so the owner raises it
   first), each golden validates, and earlier stable goldens of the major still validate.
2. **Recorded in the version.** The new `version.json` carries `"release": "golden-only"`. A
   version without the field is a schema release. `check` re-verifies a golden-only version against
   the version before it: same major, same `schema_sha256`, at least one golden different. Any
   other `release` value is a problem.
3. **Visible to consumers.** `compatibility.md` shows a golden-only version as
   `2.1.0 (golden-only, schema of 2.0.0)`, and each consumer's announcement states that the schema is
   byte-identical to the earlier version and only the goldens changed.
4. **Published versions stay immutable.** A golden-only release writes only its own `v<version>/`
   directory, the matrix and, when it is the first stable version of its major, the lock (ADR 0002
   §4). It never rewrites an earlier version's files. Locks follow ADR 0002 unchanged: any other
   golden-only release leaves them alone and consumers pick it up as an issue.

This amends ADR 0002 §7 (the "neither the export nor the constant changed" refusal now has this one
exception) and its Consequence on goldens (they are still regenerated only by `bump`, now also by
`bump --golden-only`). Everything else in ADR 0002 stands.

## Alternatives considered

- **Rewrite the latest version's goldens in place.** Lost: published versions are immutable, and a
  consumer locked at 2.0.0 would silently get different bytes.
- **Make the owner change the schema (for example bump a `const` or a description) to force a normal
  bump.** Lost: it invents a schema change that did not happen, and consumers cannot tell it from a
  real one.
- **Allow a plain `bump` whenever the goldens differ, without a flag.** Lost: an accidental golden
  change (non-deterministic generator, stale examples) would publish a version without anyone saying
  so. The flag makes the intent explicit and lets the tool check it.
- **Allow a golden-only major.** Lost: by ADR 0002 §3 a major means reader-incompatible, and an
  identical schema cannot be.
- **Record the base version (`"schema_of": "2.0.0"`) in `version.json`.** Lost: it is always the
  version immediately before, and `check` derives and verifies it; a stored copy could only drift.

## Consequences

- Memory publishes the consolidator lineage change with
  `scripts/contracts.py bump graph-schema 2.1.0 --golden-only`; Context and Deploy see a minor lag
  warning and raise their lock in their own project.
- An owner whose generator output changes without a schema change can now publish it. A
  non-deterministic generator would show up as a stream of golden-only releases; the owner's own
  determinism tests remain the guard against that.
- Each golden-only version carries another full copy of the schema, as ADR 0002 already accepts.
- Revisit if a contract needs a release that changes neither schema nor goldens (for example a note
  only); that is not a release under this ADR.
