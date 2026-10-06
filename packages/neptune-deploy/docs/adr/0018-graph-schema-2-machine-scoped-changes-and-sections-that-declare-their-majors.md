# 0018 — Graph-schema 2: machine-scoped changes, and template sections that declare the majors they read

- Status: Accepted
- Date: 2026-10-06
- Issue: MVL-161
- Amends: ADR 0013 §2 (the snapshot reader reads majors 1 and 2); ADR 0015 §1 (a 2.x document names its own
  release, so the declaration is for 1.x only)
- Uses: contracts/graph-schema 2.0.0 (Memory PR #151, Memory ADR 0019 §2–§3, graph-schema rule 12)

## Context

Graph-schema 2.0.0 is a major release because `succeeds` changed meaning, not shape. Configuration nodes are shared
by every machine that names them, so a `succeeds` claim read off one machine's chain made a change look fleet-wide:
"4.2.0 → 4.3.1" showed on AMR-05, AMR-06 and AMR-07 when only one of them changed. In 2.x a change is one machine's
own: two of its `has_configuration` spans meeting at one instant on one clock, with different objects (rule 12,
Memory's `transitions(claims, machine)`). `succeeds` now holds only where a source states it of two configurations,
and no consolidator claims it.

Deploy's `@1` templates select `succeeds` for their change sections. Run against a 2.x graph they would fall empty
and read as "no changes". The `@1` files are locked by hash (ADR 0013 §4) and cannot change. The repository rule is
that a major bump raises every consumer's lock in the same PR, and Deploy's reader refused every other major.

## Decision

### 1. Lock

Deploy's `contracts/lock.toml` entry, `docs/contracts.md` and `snapshot.GRAPH_SCHEMA_PIN` name **2.0.0**.
`GRAPH_SCHEMA_1X_PIN = "1.6.0"` is the 1.x minor that a 1.x document is read against strictly, as before.

### 2. Reader

- `GRAPH_SCHEMA_MAJORS = (1, 2)`. The major is checked before anything else, and any other is
  `snapshot_unsupported` at `/graph_schema_version`.
- **2.x**: `graph_schema` is required and must be a release of the document's own major (`snapshot_malformed`
  otherwise). The reader takes the minor from the document, so `--snapshot-schema-version` is not needed. If it is
  given, it must name the same release, or the read is `snapshot_unsupported` at `/graph_schema`. A release newer in
  minor than the pin gets ADR 0015's treatment: its unknown keys are reported once per key path as
  `snapshot_key_unread`, and never read or rendered. Optional `builds` are checked (claim ids unique and
  pattern-valid, recorded no later than the head, a non-empty list) and kept, but they are never rendered.
- **1.x**: as ADR 0015, with two refusals. The flag declares a newer 1.x minor. A declaration of any other major
  contradicts the document and is `snapshot_unsupported` at `/graph_schema_version`, with or without unknown keys;
  it is never dropped. A `graph_schema` key on a major-1 document is `snapshot_malformed` at `/graph_schema`
  whatever is declared, never a newer-minor key reported as `snapshot_key_unread`.
- The pack's `snapshot` block carries `graph_schema_version` (the major) and, for 2.x, `graph_schema`. The PDF
  header shows the release for 2.x and `graph-schema 1` for 1.x, as before.

### 3. A `changes` section kind

A template section of kind `changes` reads each scope node's boundaries from the spans its `predicates` select:
`known` predicates are decided spans, and `ambiguous` and `unknown` predicates are candidate and open spans.

- A **boundary** is a `(node, Stamp)` where some spans of that node end and others start. A single `Stamp` means
  a single clock, as in Memory's `transitions`.
- A span is **decided** when its predicate's role is `known` and the claim is not `inferred`. An inferred span is
  never decided, even under `inference: include`: it may show, marked `[INFERRED]`, but never forms a change.
- Each pair of decided spans there with different objects is one **change** entry, `knowledge: known`. This is
  Memory's `transitions` over the claims the section selects, with two differences: inferred claims are left out
  as above, and there is no consolidator filter (see Alternatives). Concurrent configurations pair as
  `transitions` pairs them, because `has_configuration` is `many`, and a restated configuration is no change.
- If a candidate, inferred or unknown span meets the boundary, there is one more entry for it, holding every span
  at that boundary: `unknown` if any span is unknown, otherwise `ambiguous`. With no change at that instant, no
  change is read across it. If two decided spans also meet there (decided A ends while decided B and candidate C
  start), the A→B change stands, as `transitions` reads it, and the boundary entry carries `beside_change: true`.
  Its caption says that another reading also starts or ends there, beside the change, so the two entries never
  contradict each other.
- Spans that do not meet (a gap, two clocks, two open ends) form no boundary. Nothing is ever read from
  `succeeds` or from another node's claims, so a change is attributed only to the node whose spans it is.
- An entry carries `change: {at, before, after}` (claim ids) in place of `valid`. A boundary on the pack clock
  inside the interval (`start ≤ at < end`) is an entry, one outside it is counted in `outside_interval`, and one
  on another clock is listed under `other_clocks`. A section with no boundary is `not_covered`, and its reason
  adds `spans_read`.
- A `changes` section must name at least one `known` predicate.

### 4. How a template declares the majors it reads

A section may carry `graph_schema_majors`: a non-empty list, without repeats, of majors Deploy reads. Leave it out
and the default is derived: every major, from 1 upwards, in which each predicate the section names (`predicates`,
`about` hops, `same_event`) still means what it meant in graph-schema 1. The contract knowledge lives in
`snapshot.MEANING_CHANGED = {2: {"succeeds"}}`, from Memory ADR 0019 §3.

The `@1` templates, which declare nothing, therefore read as follows: their `succeeds` sections read major 1 only,
and every other section reads 1 and 2. `event-timeline@1` reads 2.x unchanged. The `@2` change sections declare
`[2]`. The declaration is per section, because a major changes the meaning of particular predicates, not of a whole
report.

### 5. `section_not_covered`

When a section does not read the snapshot's major, it is `not_covered`, selects nothing and cites nothing. Its
reason is:
`{code: "section_not_covered", graph_schema_major, section_reads_majors, meaning_changed, reason}`. The pack also
lists each such section under top-level `findings`, after any `snapshot_key_unread`, with its `section` id. The PDF
shows a `Not covered: section N (id), …` header line and `NOT COVERED (section_not_covered) - …` in the section.
It is never an empty section that reads as "no changes".

### 6. Templates

`configuration-traceability@2`, `configuration-lineage@2` and `incident-timeline@2` are each their `@1` with the
`succeeds` section replaced, in place, by a `changes` section over the same machines (`[[]]`, the
`located_at`/`deployed_at` paths, and `involves`/`involves_candidate`). Each reads `has_configuration`,
`configuration_candidate` and `configuration_unknown` and declares `[2]`. Every other section is byte for byte the
`@1` section. `event-timeline` has no change section and gets no `@2`. The template schema stays
`neptune-deploy.pack-template/1`: the kind and key are additive, and every `@1` file reads as before.

### 7. Compiler version

`COMPILER_VERSION` stays **3**. ADR 0013 raises it when the same inputs give a different pack. Every input that
compiler 3 accepted (a 1.x snapshot, a shipped or registered template without the new kind or key) compiles to the
same bytes. The new content appears only for inputs it refused: a 2.x snapshot, an `@2` template or a `changes`
section. The existing goldens and the committed sample PDF do not change.

## Alternatives considered

- **Declare majors per template.** That leaves the hash-locked `@1` files either unread on 2.x as a whole
  (`event-timeline@1` and every configuration-chain section included) or wrongly read. Meaning changes per
  predicate, so the declaration is per section.
- **Keep reading `succeeds` on 2.x.** It holds only for statements about two configurations and says nothing about
  which machine changed, which is the fleet-wide misreading 2.0.0 exists to stop.
- **Let `@2` derive changes on 1.x too.** 1.x spans exist, but rule 12 is a 2.x guarantee, and the 1.x
  configuration lineage stated changes as `succeeds`. One pack per major keeps one meaning per section.
- **Filter spans to `memory.configuration`.** Rule 12 names that consolidator, and it is the only one that claims
  these predicates in vocabulary 11. A filter would hide another lineage's spans silently. Every entry cites its
  claims, so the consolidator shows in the claims index.
- **Raise `COMPILER_VERSION`.** It would change every 1.x pack id and golden while no 1.x pack says anything new.

## Consequences

- Deploy is current on graph-schema 2.0.0. The harness no longer stubs Deploy for being a major behind.
- The next graph-schema major means three steps: add it to `GRAPH_SCHEMA_MAJORS`, record in `MEANING_CHANGED` the
  predicates it narrows, and ship new template versions for the sections that select them.
- A 1.x snapshot still needs `--snapshot-schema-version` for a newer 1.x minor. A 2.x snapshot never does.
- A change entry has no `valid`. Readers of pack JSON branch on `change`.
- Revisit this if Memory publishes changes as their own claim type. A `changes` section would then select that
  claim type instead of deriving changes from boundaries.
