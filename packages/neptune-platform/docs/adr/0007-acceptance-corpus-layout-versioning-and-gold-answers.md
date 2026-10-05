# 0007 — Acceptance corpus: generated in `harness/acceptance`, locked by version, gold answers cited by path and selector

- Status: Accepted
- Date: 2026-10-05
- Issue: MVL-181
- Amends: ADR 0004 §5 (the harness's corpus)

## Context

The programme needs one fixture that every layer's gate is measured against: a messy hand-over of
two sites and three robot types, with one incident storyline that needs every evidence kind to
explain, and gold answers that Context's C4 benchmark, Deploy's D3 evidence packs and the Demo v1
score against ("why did the arm-cell incident happen, what changed, which configuration was
active, what is unknown"). Forces:

- **It must stay small and reproducible.** No file over 512 KB, no LFS or bucket (the programme is
  one monorepo), byte-identical on every host, and growable by script.
- **It must not fork the D1 archetypes** (Deploy ADR 0004): the warehouse fleet and the manipulator
  cell already exist as generators.
- **Gold answers must outlive compiler versions.** Record ids are content-derived: a new adapter
  version changes them. Citing them alone would make every compiler change a gold-answer change.
- **Gates must name what they passed.** "Harness green" is meaningless if the corpus moved under it.

## Decision

1. **Where and how.** `harness/acceptance/` (Platform owns `harness/`). `generate.py` builds every
   file in memory; `harness.acceptance.build()` returns them by POSIX path, `materialise(root)` writes
   them. The D1 generator (`packages/neptune-deploy/tests/fixtures/archetypes/make_archetypes.py`)
   is imported by path, and through it the compiler's MCAP writer and Deploy's tagged-PDF writer; the
   corpus takes `fleet()` narrowed to site S-007 and `cell()` regrouped under PLANT-2, and adds the
   storyline, the legged robot and the plant documents. Nothing generated is committed except
   `corpus.lock.json`; the corpus is generated at harness time. No clock, randomness, network or
   compression library is used, so the bytes are the host's own only through the writers above.
2. **Layout.** One hand-over folder, ingested as one case (one package), so cross-site and
   cross-robot questions have one package to answer from:
   `neptune.yaml` (machines with embodiments, sites, declared runs, the CSV header option),
   `records/asset_register.csv`, `sites/S-007/` (the AMR fleet: URDFs, nav configs, zone map, zone
   register, CMMS, changes, requalification, MCAP runs, INC-0007) and `sites/PLANT-2/` (`cmms/`,
   `changes/`, `maps/`, `survey/`, `vendor/`, `cell3/` for ARM-3A, `legged/` for LEG-01). New
   material goes under the site it belongs to; a third site is a new `sites/<id>/`.
3. **Versioning.** `VERSION` in `harness/acceptance/__init__.py`, semantic: **major** when a gold
   answer changes meaning or cited evidence is removed or moved; **minor** when files or questions
   are added and every existing answer still holds; **patch** when bytes change and no answer does
   (a writer fix, a typo outside cited text). `corpus.lock.json` records the version, every file's
   sha256 and size, and a tree id (sha256 of the sorted `path NUL digest LF` lines). A test fails
   when a fresh build differs from the lock, when the lock's version is not `VERSION`, or when a file
   passes 512 KB; `python -m harness.acceptance lock` rewrites the lock after the bump. A change to
   the imported writers changes the corpus the same way, so `harness.yml` runs on their paths too.
4. **Gates quote it.** The harness's default corpus is the acceptance corpus. Its report carries
   `corpus: {name: "acceptance <version>", version, tree, locked, problems}`, and a run whose build
   does not match its lock is red. A gate states "harness green at <sha>, corpus acceptance
   <version> (tree <id>)". `--corpus-name worked-examples` keeps the compiler's four worked
   examples for quick runs; `--corpus DIR` any folder.
5. **Gold answers** (`harness/acceptance/gold.json`, `gold_format: 1`). Questions with an id, the
   question, phrasings, tags, a reference answer and **claims**; each claim has `knowledge`
   (`known` or `unknown`: "what is unknown" is answered by unknown claims with the evidence that
   shows the gap), `assertion` (`observed`, `stated`, `inferred`, as the provenance model uses
   them) and the **evidence ids** it rests on. Evidence items name a corpus path and a selector
   (`source`, `document_text`, `table_row`, `no_table_row` for a cited absence, `config_value`,
   `calibration`, `stream`, `message`, `finding`, `clock_mapping`; their rules are in
   `harness/acceptance/resolve.py`), never a record id. `traps` name the evidence an answer must
   not misread (prompt injection, stale config and register, the duplicated run, the corrupt bag),
   and each question lists `must_not_cite`.
6. **Resolution and scoring.** `harness.acceptance.resolve` maps every evidence item to the records
   of a compiled package (and, for `message`, the series rows) by reading package-schema's files;
   the harness's compiler stage resolves them on every run and fails on any that resolve to
   nothing. Consumers score against the resolution of the package they read: a claim is supported
   when the answer cites at least one record (or row) of one of its evidence items; an answer fails
   a question when it cites a `must_not_cite` item as support or asserts a trap's wrong reading.
   Each consumer owns its scorer; this is the rule they share.

## Alternatives considered

- **Commit the generated corpus.** Lost: 700 KB of binaries that duplicate what a script makes in a
  tenth of a second, and every regeneration a large diff. The lock gives the same pinning in one
  reviewable file.
- **A `neptune-integration` repository with LFS or a bucket** (the issue's first plan). Lost: the
  programme is one monorepo (Platform ADR 0001), and nothing here needs large-file storage.
- **Copy the D1 archetypes into the corpus.** Lost: two sources of the same fleet that drift apart.
  Importing means a D1 change shows up as a corpus change, which the lock then makes deliberate.
- **Cite record ids in the gold answers.** Lost: they change with every adapter version, so gold
  answers would be rewritten for unrelated compiler work. Path plus selector is stable; the
  resolution gives the ids for the package in hand.
- **Free-text gold answers scored by similarity.** Lost: not deterministic and not checkable here.
  Claims with cited evidence are checkable, and the reference answer stays for people.
- **One case per site.** Lost: "what changed" and "what is unknown" cross the cell, the legged robot
  and the plant's documents; one package keeps every citation in one place.

## Consequences

- The harness ingests about 50 sources by default (about 25 s); `--corpus-name worked-examples`
  is the quick path. The platform tests ingest it once per session (`test_acceptance_ingest.py`).
- A Deploy change to `make_archetypes.py` or a compiler change to its fixture writers fails
  Platform's lock test until a Platform PR bumps the version; `harness.yml` runs on those paths.
- A compiler change that stops producing cited evidence (a finding renamed, a field no longer
  decoded) fails the harness with the evidence id; the fix is in the compiler or a new corpus
  version, never a silent gold edit.
- Revisit when a consumer needs per-claim weights, when the corpus needs a file over 512 KB, or when
  record ids become stable across adapter versions.
