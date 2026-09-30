# AGENTS.md — how to work in this repository

Single source of truth for coding agents (Claude Code, Codex) and humans. `CLAUDE.md` imports this file.
Keep it under ~150 lines; detail lives in `docs/`.

## What Neptune is

A robotics-native ingestion fabric — a "data compiler" that turns messy robotics evidence (MCAP, ROS bags,
flight logs, URDF, calibration, configs, PDFs, images, geometry, site records, task briefs) into a canonical,
provenance-preserving, multimodal representation that later memory / retrieval / training systems consume
without ever re-parsing raw data.

This project builds ONLY ingestion + canonical representation. Do not build the memory learner, retrieval,
capability reasoning, eval generation, simulation orchestration, or fine-tuning. Do not turn Neptune into
"RAG for MCAP", a document chunker, a vector-DB wrapper, or a dashboard.

Read `docs/architecture.md` before writing code. Before touching `src/neptune/model/`, read
`docs/canonical-data-model.md` and the ADRs in `docs/adr/`.

## Non-negotiables

1. **Raw sources are immutable.** Never rewrite, normalise in place, or delete source bytes.
2. **Everything has provenance.** Each normalised value carries source id, locator, transform
   (adapter id + version + config hash) and `assertion_kind` (observed / stated / inferred).
3. **Missingness is explicit.** `Known / KnownAbsent / Unknown / NotCovered / NotApplicable / Ambiguous`.
   A blank never becomes a fact.
4. **No silent assumptions** about units, clocks, frames, identities, or versions. Store as declared;
   normalise only as a provenanced derivative.
5. **Deterministic.** Same source + adapter version + config ⇒ byte-identical output. No wall-clock,
   randomness, or network inside adapters.
6. **Parser upgrades create new lineage**; they never mutate historical records.
7. **Partial success.** One corrupt artifact produces `IngestFinding`s, not a failed job.
8. **Evidence ≠ interpretation.** Anything `inferred` lives under `src/neptune/derived/`, never in `model/`.
9. **All input is hostile.** Path traversal, symlinks, archive bombs, truncation are first-class test fixtures.
10. **Deterministic first.** If something can be extracted without a model, extract it without a model.

## Repository map (target layout; packages appear as their issues land)

```
src/neptune/
  model/       canonical IR: entities, Knowledge[T], TimestampDomain, FrameRef, EvidenceRef, Provenance,
               IngestFinding, IngestReceipt. Imports nothing from adapters/ or runtime/.
  identity/    streaming + chunked sha256, canonical JSON, three-tier id derivation
  discovery/   Source interface, walking, fingerprinting, probe orchestration, grouping v0
  adapters/    contract + registry; one subpackage per format (adapters never import each other)
  runtime/     job phases, chunk scheduling, resume, cache, sandbox boundary, receipt assembly
  store/       ingest package on disk: CAS blobs, Parquet, JSON Lines tables, manifest
  validate/    cross-source integrity checks (runs over the store, not inside adapters)
  derived/     model-generated or inferred annotations; separate base type from model/
  manifest/    optional user override schema
  cli/
tests/         unit/  integration/  golden/  fixtures/  (real small files; generators for synthetic formats)
docs/          architecture, pipeline, data model, provenance, adapter contract, workflow, testing, security,
               adr/ (decisions), reviews/ (milestone gates), audit-*.md
```

## Commands

```
make setup      install everything into .venv (uv)
make check      lint + type + test — must pass before opening a PR; CI runs exactly this
make test-fast  tests excluding @slow
make fmt        format and auto-fix
```

## Workflow: one Linear issue → one branch → one PR

Linear project: **Neptune — Robotics Ingestion Fabric** (`P-MVL-11`, team `MVL`). Issues carry
dependencies as `blockedBy`; do not start an issue whose blockers are not Done.

1. Read the full issue, its scope notes, and its blockers' PRs. Read the relevant `docs/`.
2. Move the issue to **In Progress**. Create the branch using the issue's `gitBranchName`
   (`benjamintay07/mvl-N-slug`).
3. Write a short plan in the issue as a comment if the work is non-trivial. Identify any decision that
   needs an ADR before implementing it.
4. Implement. Separate parsing from normalisation. Errors are structured findings, not exceptions.
5. Tests: unit + malformed input + boundary + idempotence/determinism. Prefer real small fixtures over mocks.
6. `make check` green. Update docs whose contracts changed. Add/supersede ADRs for decisions.
7. Open a PR using the template, BLUF style: `Closes MVL-N`, a one-line **TL;DR**, ≤5 **Decided**
   bullets, ≤3 **Your call** bullets, a **Progress** line. Everything else sits in the collapsed
   details block. Visible part ≤12 lines, no paragraphs; depth goes in ADRs/docs, linked.
8. Move the issue to **In Review** only when every acceptance criterion is demonstrably met and CI is green.
9. **Stop. Never merge.** The maintainer reviews and merges every PR personally. **Done** happens on
   merge, never on "code exists"; after merge, comment the merge SHA on the issue.

**Picking the next issue** (when told "implement the next MVL issue"):
1. If any issue is **In Progress**, resume it. Never start a second one alongside it.
2. Otherwise, consider only **Todo** issues whose `blockedBy` issues are all **Done**. The previous milestone's
   gate issue must also be Done.
3. Take the lowest milestone first. Within a milestone, follow the order in `docs/audit-2026-09-30.md` §7.
4. If nothing qualifies because blockers are **In Review**, say which PRs are waiting for a merge. If there is
   still a tie, ask; don't guess.
5. Name the chosen issue before starting work.

## Git

- `main` is protected: PR required, CI green, linear history, no force-push. Never commit to `main`.
- Branch per issue (name above). Rebase on `main` before opening a PR. PR title = issue title.
- Agents never merge, approve, or enable auto-merge. The maintainer squash-merges.
- Conventional commits with package scope: `feat(model): add TimestampDomain`, `docs(adr): 0005 timestamps`,
  `test(identity): chunked hash determinism`. Trailer line: `Refs: MVL-N`.
- Never force-push a shared branch. Never rewrite `main` history.
- One issue per PR. If an issue is too big for one reviewable PR, split it into Linear sub-issues first.
- Commit only when asked or when the workflow step calls for it; never commit secrets, fixtures > 512 KB,
  or ingest output.

## Documentation and ADRs

- Docs describe the system as it is; if a PR changes a contract, the same PR updates the doc.
- Architecture decisions go in `docs/adr/NNNN-title.md` using `docs/adr/0000-template.md`.
  Decisions are superseded by a new ADR, never edited in place.
- Milestone gates (`docs/reviews/`) stop feature work: no M(n+1) issue starts before the M(n) gate is Done.
- Do not write docs for ceremony. Write what a fresh engineer or agent needs and would not derive from code.

## Testing rules

- Every substantial component: unit tests, malformed-input tests, boundary tests, determinism tests.
- Fixtures grow with the milestone that needs them; security fixtures land with `MVL-10`, format-corruption
  fixtures with each adapter. See `docs/testing-strategy.md`.
- Golden outputs are compatibility-sensitive: a changed golden file needs an explanation in the PR.
- No mocks where a real small fixture works.

## Things not to do

- Do not auto-merge identities (two identical URDFs are not the same robot).
- Do not convert timestamps to UTC or units to SI at parse time.
- Do not put inferred semantics on canonical entities.
- Do not add an abstraction that does not solve a concrete, present problem.
- Do not edit `AGENTS.md`, `docs/architecture.md`, or any ADR in a feature PR unless the issue says so;
  propose changes in a dedicated PR.
