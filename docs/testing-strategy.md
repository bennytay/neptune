# Testing strategy

Status: policy agreed 2026-09-30; suites grow with each milestone.

## Categories

| Directory | Marker | What |
|---|---|---|
| `tests/unit/` | — | one component, no I/O beyond small fixtures; runs in seconds |
| `tests/integration/` | `integration` | multiple components against real fixture files |
| `tests/golden/` | — | serialised canonical packages and receipts compared byte-for-byte |
| `tests/fixtures/` | — | real small files + generator scripts; organised by format |

`make test-fast` excludes `slow`. CI runs everything.

## Mandatory per substantial component

- **Malformed input**: truncated, corrupted, empty, wrong-magic, renamed/extensionless.
- **Boundary**: zero records, one record, chunk-boundary-sized inputs, maximum declared sizes.
- **Determinism**: run twice, compare bytes. Property-based with Hypothesis where the input space is generative.
- **Idempotence**: ingest, ingest again, store unchanged.
- **Partial corruption**: one bad chunk ⇒ findings for that chunk, success for the rest.
- **Lineage**: bump adapter version in a test ⇒ new record ids, old records untouched.
- **Backwards compatibility** (after the M1 gate): old golden packages still load.

## Fixture policy

- Prefer real files. Where formats are generative (MCAP, bags, ULog), commit a generator script and the small
  generated output, so the fixture is both reproducible and inspectable.
- Fixtures over 512 KB are rejected by pre-commit. Large corpora (MVL-49) live outside the repo and are
  fetched by a script with pinned hashes.
- Fixtures **grow with the milestone that needs them**: security fixtures (path traversal, symlink loop,
  archive bomb, truncation) landed with MVL-75 in `tests/fixtures/hostile/` (generator plus committed
  archives; the symlink tree is built at test time); per-format corruption fixtures land with each
  adapter; MVL-50 consolidates and audits coverage, it does not start from zero.
- Every adversarial fixture has an expected structured outcome: salvage, explicit ambiguity, unsupported,
  or safe rejection.

## Golden tests

Golden outputs are compatibility-sensitive infrastructure. A PR that changes a golden file must explain the
change in its description; the reviewer treats an unexplained golden diff as a defect. Parser upgrades are
expected to produce golden diffs — that is what lineage tests assert.

## No mocks where a fixture works

Mocks hide format reality. Use them only for genuinely external systems (object stores, network) and keep a
real-fixture path for the same code.

## Static checks

`ruff` (lint + format), `mypy --strict` over `src` and `tests`. Both are part of `make check`.
