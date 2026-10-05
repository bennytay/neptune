# 0013 — Evidence packs compile cited claims from a frozen Memory snapshot

- Status: Accepted
- Date: 2026-10-06
- Issue: MVL-159
- Uses: Memory ADR 0002, 0006 (graph-schema), 0010 (configuration lineage), 0013 (event index, PR #125);
  Ledger ADR 0004 (catalog API); platform ADR 0002 (contracts registry)

## Context

An evidence pack is the document a reviewer, an auditor or an incident board reads: "which configuration was
ARM-06 in, what changed, what was authorised", "what happened around INC-C3-0011". It is only worth having if every
sentence in it can be traced to evidence, if the same question always gives the same document, and if nothing in it
is a guess. The facts already exist as Memory claims (configuration lineage, events). The pack has to state them
without adding any.

The risks are specific. Filling an Ambiguous configuration span with one candidate would put a guess in front of a
reviewer. Comparing a run's own clock with the civil clock invents an order. Letting an inferred claim read like
evidence breaks the stated/inferred line. A PDF library that embeds the time or compresses differently by version
breaks byte identity. A template edited in place quietly changes what an old pack id means. MVL-161 (the
configuration traceability report and the incident reconstruction timeline) must be templates only, so the section
model has to carry those reports already.

## Decision

### 1. `PackSpec`

A spec (`neptune-deploy.pack-spec/1`, JSON) names a template id and version; a subject (`site`, `deployment` or
`machine`, as a graph-schema node); an interval (`[start, end)` or `[start, open)` on **one** clock, a graph-schema
`Timestamp` domain); the snapshot id; and an inference policy (`exclude`, the default, or `include`). A spec built
in code is held to the same rules as a spec file.

### 2. The snapshot is a graph-schema document, and Deploy imports no Memory code

The pack reads one frozen `#/$defs/Graph` document (`kind: memory.graph`) of `contracts/graph-schema`. Deploy
validates the shapes it uses and keeps each claim's JSON verbatim. It does not import `neptune_memory`, so it adds no
workspace dependency and is not tied to Memory's HEAD; the registry's lagging pin is the point (platform ADR 0002).
Node types are read as tokens and locator steps as objects with a `kind`, so a later minor (1.6.0 adds `event`)
still reads.

The contract names a graph by `head` (Ledger transaction) and `generation` (resolver configuration hash); neither
names the claim set, since two Ledgers at one head differ. **Snapshot id** = `snapshot:` + sha256 of the document's
canonical JSON. The spec names it, and compiling over any other document is refused (`snapshot_mismatch`). A
document that breaks a contract shape is refused whole, with a JSON pointer (`snapshot_malformed`), never partly
used. A pack that silently dropped one claim would misstate the snapshot it names.

### 3. Pack id and determinism

**Pack id** = `pack:` + sha256 of the canonical JSON of `{spec, template sha256, compiler id and version}`. The spec
holds the snapshot id, so the same inputs always name the same pack, and a compiler upgrade (`COMPILER_VERSION`)
names a new one. Every collection is sorted by a total key (node, interval, predicate, object bytes, claim id).
Nothing reads a clock, the environment or a random source. A test renders both fixture packs in three processes
with different `PYTHONHASHSEED`s and pins their digests.

### 4. Templates are data, versioned and locked

A template (`neptune-deploy.pack-template/1`) is JSON: subject types, and sections, each with a `kind`, the
`about` paths from the subject (hops along a predicate, `out` from subject to object or `in` from object to
subject), and `predicates` mapping each selected predicate to the knowledge state it expresses (`known`,
`ambiguous`, `unknown`). Shipped templates are `packs/templates/<id>@<version>.json`, and `lock.json` pins each
one's canonical sha256. Loading refuses an edited file (`template_lock_mismatch`), and a registry refuses a second
body for a registered version (`template_version_changed`). A change is a new version. The pack records the
template hash it was built with. Shipped: `configuration-lineage@1` and `event-timeline@1`.

### 5. Sections, entries, statements

- A **statement** is one claim, cited by id, with its predicate, object, assertion kind and the template's role.
  Nothing in a pack states a fact that is not a claim.
- An **entry** is statements about one node over one valid interval, with a state:
  - `known`;
  - `ambiguous`: Memory's `*_candidate` claims, or a decided reading beside them. Every reading is shown and none is
    chosen;
  - `unknown`: `*_unknown` claims, whose object is the record that leaves the value open;
  - `conflict`: two objects of a `one` predicate in the snapshot's own vocabulary, in one entry or in two entries of
    one node whose intervals overlap, or (timelines) one event placed at two times on the pack clock. Every
    statement is shown. A conflicting event's placements on the pack clock are all shown, including those outside
    the interval, because the disagreement is the point.
- Section kinds:
  - `claims`: one entry per claim;
  - `states`: one entry per node and interval, which is how a configuration chain reads;
  - `timeline`: one entry per event placement on the pack clock, in onset order. Each entry names the records cited
    by that placement but not by every claim of the event, which is the clock mapping and target clock a mapped
    placement cites (Memory ADR 0013 §2).
- **Scope**: the nodes the section's paths reach. Hops follow current claims whatever their valid time, and each
  node lists the claims that reached it, so a run "of ARM-06" cites its `recorded_by`.
- **Clocks**: a claim overlapping the interval on the pack clock is an entry. A claim on the pack clock outside the
  interval is counted (`excluded.outside_interval`). A claim on any other clock is listed under `other_clocks`,
  "never compared". A timeline lists there only events with no placement on the pack clock. A timeline therefore
  renders on the clock the spec's interval chooses.
- **Missingness**: a section with neither entries nor other-clock entries is `not_covered`. Its `reason` names the
  snapshot, the predicates, the nodes in scope, the counts left out, and any predicate the snapshot's vocabulary
  lacks (an event section over a vocabulary-4 graph says so). A section whose `subject_types` exclude the
  subject is `not_applicable`, with both type lists. Ambiguous and Unknown are entry states with their citing
  claims (above).
- **Resolver findings** (`clock_mismatch`, `overridden_on_arrival`) that name a cited claim are listed with the
  section. Their claims join the pack's claim set, and a superseded one is shown as superseded. Under `exclude`,
  an inferred claim a finding names stays out: the finding lists its id, and the PDF marks it
  `[INFERRED:excluded]`.
- The pack's `claims` is every claim it cites (statements, scope hops, findings) in graph-schema form, by id. This is
  the claim set MVL-161 exports.

### 6. Inference

`exclude` leaves out every inferred claim: it is never a statement, and it is never a hop, so a run linked to a
machine only by an inferred `recorded_by` is not in the pack. The count is reported per section and for the pack.
`include` uses them. Each inferred statement then carries `inferred: {model, confidence}` in the JSON, and every
inferred statement and scope hop is marked `[INFERRED]` in the PDF. The header says which policy applied.

### 7. The appendix resolves through the Ledger

For every evidence ref the cited claims hold, the appendix lists the claims citing it and how to resolve it:
- a content-id source with a locator: a catalog-api 1.6.0 `ResolveRequest`;
- an external object: its connector, object id and revision (the catalog resolves content ids only);
- a whole-source ref with no locator step: `via: none` with the reason, since `resolve` requires a step.

For every record (provenance records, and record objects), it lists `ThreadsOfRequest` and `LineageRequest`. Each
request is pinned `as_of` the snapshot head (graph-schema `LedgerTx` is the Ledger's commit sequence number), so the
answer is the Ledger state Memory read. At head 0 there is no `TxSeq`, so no `as_of` is given. Tests validate every
request against `contracts/catalog-api/v1.6.0`.

### 8. Refusals are structured

`PackError(code, message, pointer)`: `spec_malformed`, `snapshot_malformed`, `snapshot_unsupported`,
`snapshot_mismatch`, `template_malformed`, `template_unknown`, `template_lock_mismatch`, `template_version_changed`,
`subject_type_unsupported`, `output_refused`. Inputs are bounded (snapshot 512 MiB, spec and template 1 MiB),
duplicate keys and NaN are refused, deep nesting is refused, and a lone surrogate cannot be hashed and is refused.

### 9. Renderers: canonical JSON, and a hand-written PDF 1.4

- JSON is `neptune.identity.canonical_json`. The JSON is the pack.
- The PDF uses no dependency. A ~150-line writer emits PDF 1.4 with the standard Type1 fonts (Helvetica-Bold for
  headings; Courier, Courier-Bold and Courier-Oblique for body) in `WinAnsiEncoding`. It writes no compression,
  since a compressor's bytes may change between library versions. Content streams are 7-bit (octal escapes), and
  the cross-reference table is computed from the bytes written.
- Fixed metadata: `/CreationDate` and `/ModDate` are `D:19700101000000Z` (a pack has no wall-clock time; the snapshot
  head says when), and `/ID` is the first 16 bytes of the pack id. Info strings are ASCII.
- Layout is a fixed line flow on A4. Courier wraps by its exact 0.6 em width. Headings wrap as if every glyph were
  1 em, which no Helvetica-Bold glyph exceeds, so no AFM table is needed.
- **WinAnsi rule**: printable ASCII is kept, and so is every other character Windows-1252 encodes. Anything else,
  control characters included, becomes `<U+XXXX>` (at least four uppercase hex digits). Nothing is dropped, and the
  JSON keeps the original. The `<U+…>` form is visually ambiguous with source text that already contains it, so the
  JSON is the reference.
- pypdf reads both fixture PDFs strictly. It is used as an oracle outside the project (`uv run --no-project --with
  pypdf`), never as a dependency. In-repo tests check the structure: offsets, `/Length`, `startxref`, `/ID`,
  7-bit body.

### 10. Command line

`python -m neptune_deploy pack --spec <file> --snapshot <file> --out <dir>` writes `pack.json` and `pack.pdf`. Inputs
are read at most one byte past their limit, so an oversized file is refused, not loaded. Every output is checked
before any is written. An existing file with the same bytes is left alone; one with other bytes, or a symlink, is
refused, because a pack never changes. Each file is written aside and renamed into place, so it appears whole.

## Alternatives considered

- **Import `neptune_memory.schema` (as Context does).** Deploy would follow Memory's HEAD instead of its pin, and it
  would gain a workspace dependency, which widens merge-freshness and CI selection. The graph document is the
  published contract.
- **Snapshot id = `generation@head`.** It does not name the claims: two tenants or Ledgers at one head collide.
- **reportlab, fpdf or pypdf to write PDFs.** They are new dependencies, they embed creation times and producer
  versions, and their output changes with the library version.
- **Pick the most recent candidate, or the commissioning record, for an Ambiguous span.** This is the guess the
  issue forbids.
- **Convert run clocks to civil time to place runs in the interval.** Clock mapping is Memory's (MVL-130). Comparing
  without a stated mapping is a silent assumption.
- **Templates as Python classes.** Every report would be code, so MVL-161 could not be templates only, and editing a
  class would change old packs without changing their version.
- **Partial snapshots (skip a malformed claim, keep the rest).** The pack would name a snapshot it does not
  represent. Memory's documents are contract-validated, so a malformed one is an upstream defect to surface.

## Consequences

- MVL-161 adds `configuration-traceability@1` and `incident-timeline@1` as template files, with lock entries, and no
  compiler code: `states`, `claims` and `timeline` sections, scope paths, on-clock selection, conflicts and
  exported claim sets already exist. Spatial and episodic sections are new templates over the same kinds, or a new
  kind if their grouping differs.
- **Event sections wait on #125 only for the pin.** The event fixture is generated in graph-schema 1.6.0's shape and
  validated against 1.2.0 plus the `event` node type. When #125 merges, the test validates it against the
  published 1.6.0 unpatched, and Deploy raises its lock entry to 1.6.0.
- **Gaps for Memory**: the contract has no snapshot id (Deploy hashes the document), and no export command for a
  frozen graph document. The consolidators' own findings (`configuration.chain_overlap`, `events.ambiguous_time`,
  …) are not in the graph document, so a pack can cite only resolver findings.
- Any change to what a pack of the same inputs says raises `COMPILER_VERSION`, which changes pack ids. The golden
  digests in `test_deploy_packs_render.py` make that visible.
- Revisit when Memory publishes a snapshot id or an export, when the catalog resolves whole sources or external
  objects, or when a reader needs non-WinAnsi glyphs, which would mean an embedded font.
