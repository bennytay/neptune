# 0020 — The run consolidator reads the compiler's run_declaration

- Status: Accepted
- Date: 2026-10-06
- Issue: MVL-191

## Context

ADR 0009 §1 read what a manifest says a run involved from a Memory-local stand-in,
`run_declaration {id, run, machine, site, task, evidence}`, whose `run` named the run by a declared
logical id. Package-schema 9 (root ADR 0072) emits the real record: `run` is the id of one `Run`
record, `logical_id` is the name the manifest gives it, `machine` / `site` / `task` are `Knowledge`
of a `LogicalId`, and `provenance` cites the manifest entry. Real records carry `schema_version`
and `provenance`, so the stand-in parser refused every one as malformed and no run carried its
declared machine, site or task.

## Decision

1. `run_records.declaration` is the compiler's `run_declaration_from_json` (with the usual
   `inferred` refusal and blank-or-padded id check). The stand-in shape and parser are removed.
2. A declaration's run is `run_node(view.runs[declaration.run])`: the node of the Run record it
   names, keyed as ADR 0009 §2 keys that run. A run id no admitted Run record has is a
   `dangling_declaration` finding (details: the run id); nothing is guessed from the declaration's
   `logical_id`, its paths or anything else.
3. `logical_id` (the user's run name) is read and not used. It never keys or merges a run node: a
   renamed manifest entry must not re-key a run, and a name is not the run's own declaration.
4. `machine`, `site`, `task` keep their `Knowledge` shape and the ADR 0009 policy: `Known` when every
   ground names one id, `*_candidate` claims when they disagree or a field is `Ambiguous`, nothing
   when `Unknown`. A declaration's evidence is `(provenance.evidence,)` and its record is cited.
5. Snapshot pins are ordinary `snapshot_binding` records, already read by `memory.configuration`;
   this consolidator does not read them.
6. The run consolidator's version becomes `2`: its declaration-grounded claims are a new lineage.

## Alternatives considered

- **Keep the stand-in path beside the real one, as ADR 0018 does for threads.** ADR 0018 keeps a
  stand-in because the archetype goldens are built from it. No archetype golden, worked example or
  acceptance fixture carries a `run_declaration`, so a second parser would serve only tests; the
  tests now build the record with the compiler's `RunDeclaration` class.
- **Key the run node by the declared `logical_id`.** Gives a readable node id, but makes a manifest
  edit re-key a run and lets two manifests' names split or join one recording.
- **Emit `has_name` from `logical_id`.** A display name is useful, but `has_name` is single-valued
  and two entries can name one run differently; deferred until a reader needs it.

## Consequences

- Runs ingested under a manifest now carry `recorded_by`, `at_site` and `executes_task`, citing the
  declaration (the compiler's three manifest goldens are the tests' fixtures).
- Where a log states its own machine id and the manifest states another (an aerial log's
  `sys_uuid` beside the manifest id it is an alias of), the run's machine is two candidates and a
  `declarations_disagree` finding: unifying aliases is identity's (ADR 0008), never this policy's.
- A compiler `run_declaration` shape change is a package-schema bump read through
  `run_declaration_from_json`; Memory has no shape of its own to keep in step.
