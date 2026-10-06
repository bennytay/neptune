# 0008 — Harness deploy stage: corpus-declared Deploy mappings between compiler and ledger, and the assertion selector

- Status: Accepted
- Date: 2026-10-06
- Issue: MVL-191
- Amends: ADR 0004 §2 (the stages), ADR 0007 §3 and §5 (corpus 2.0.0, a new selector)

## Context

The harness ran compiler → ledger → memory → context. Deploy's lifecycle mapper never ran, so
the acceptance corpus's work orders, change records, incident tickets and authorisation envelopes
reached the Ledger only as untyped table rows, and Memory could not answer "what changed" from
claims (MVL-191 triage, 2026-10-06). Forces:

- **Each corpus needs its own mappings.** The acceptance corpus has CMMS, ServiceNow, Jira and
  zone-register exports; the worked examples have none. Deploy is adding templates and presets
  (an arm-cell incident template, a requalification preset), and adding one must not need a
  harness code change.
- **No green-on-nothing.** A mapper that silently maps zero rows (a renamed column, a dropped
  preset) must turn the harness red. A table no mapping reads is normal: Deploy reports it as a
  finding, and the stage must not fail on it.
- **The compiler's manifest is strict** (root ADR 0047 §1): unknown keys are refused, so it cannot
  carry Deploy's settings. Any file inside the corpus folder is ingested as a source.
- **Deploy owns no contract.** Its mapped package is package-schema, written by the compiler's
  writer through Deploy's command line (Deploy ADR 0002 §8).
- **The calibration rewrite moves cited evidence.** easy_handeye's result file holds frames and a
  transform only: no robot, time or reprojection error. Gold items that cite
  `/translation/z` and `/reprojection_error_px` in the old files cannot keep their locators.
- **An operator's statement must be citable.** The same-event assertion for INC-C3-0011 (root
  ADR 0062) is the evidence Memory joins two stops on; citing the whole file would also support any
  other assertion in it.

## Decision

1. **Stages.** compiler → **deploy** → ledger → memory → context. The deploy stage is owned by
   `neptune-deploy`, checks the `package-schema` contract it writes, and names an `entry` module
   (`neptune_deploy.lifecycle`) that must be importable for a real run, beside ADR 0004 §3's rules.
   Its stub maps nothing, and the compiled packages flow on.
2. **The declaration.** A case may carry a Deploy declaration (`Case.deploy`); the acceptance
   corpus's is `harness/acceptance/deploy.json`, next to `gold.json` and outside the generated
   tree:
   `{"deploy_format": 1, "corpus", "corpus_version", "presets": [shipped preset names],
   "templates": [repository-relative template files or directories], "at_least": {kind: n}}`.
   Like `gold.json` it names the corpus version it was written for, and a test checks that version.
   It is used whole or not at all. The stage refuses it, and fails the case, when the format is
   not 1, a list is not names or repeats one, a template path is absolute, leaves the repository
   or does not exist, a count is not an integer of at least 1, or nothing is declared.
3. **The run.** For each declaring case with a committed package, the stage runs
   `python -m neptune_deploy map <package> -p … -t … -o work/packages/<case>.deploy` (Deploy's
   published interface, with the harness's own interpreter). It then reads the mapped package back
   and verifies it, and validates its manifest and receipt against package-schema. It counts the
   package's lifecycle records by kind and by declaration. Each record is attributed through its
   transform record's `mapping_sha256` or `template_sha256`, compared with the sha256 of the
   declared preset or template.
   The case is red when the map exits non-zero, a declared preset is not shipped, the package
   does not verify or validate, it holds no lifecycle record, any declaration mapped no record, or
   an `at_least` count is not reached. Deploy's own findings (`table_unmapped`, `row_unmatched`
   and the others) are reported by code and never fail the stage. A case without a declaration
   (the worked examples, `--corpus DIR`) is passed over.
4. **Downstream.** The ledger stage registers every compiled package and then every mapped one
   (`<case>.deploy`, with a `stage` field), under the same checks (ADR 0006). The stubs count
   both. The harness posts to Deploy's gate issue, as for every stage owner.
5. **CI.** `harness.yml`'s pull-request paths and `.github/scripts/ci_plan.py`'s
   `DEPLOY_STAGE_INPUTS` cover Deploy's `lifecycle/` package (mapper, presets, templates), its
   `__main__.py` and its `pyproject.toml`. A change to any of them runs the harness and the
   `neptune-platform` job, whose tests map the corpus. A template that `deploy.json` declares
   outside those paths fails a workflow test until the filters cover it.
6. **The `assertion` selector** (extends ADR 0007 §5 and §6). `{kind: "assertion", path, id}`
   selects the `assertion` records of the path whose declared `identifier` is `id`
   (`{namespace, value}`). The citation locator is `{"pointer": "/assertions/<i>"}`, the entry's
   JSON pointer. `gold_format` stays 1: the addition is additive, and `resolve.supports` already
   compares locators whole.
7. **Corpus 2.0.0, not 1.1.0.** The rewrite moves four cited items:
   - `cal.0818.z` and `cal.0911.z` now point at `/transformation/z`;
   - `cal.0818.error` and `cal.0911.error` now cite rows of the vision team's
     `calibration/handeye_calibration_log.csv`.

   ADR 0007 §3 makes moved evidence a major version. Every evidence id, claim and answer keeps its
   meaning. The additions are minor-shaped: the PLANT-2 envelopes, the downtime log, the
   syslog export, the assertion, the calibration log and the questions Q7 (the 32 s stop conflict)
   and Q8 (authorisation).

## Alternatives considered

- **Presets hard-coded in `stages.py`.** Lost: every corpus would get the acceptance corpus's
  mappings, and each new Deploy template would be a harness change.
- **The declaration in `neptune.yaml`.** Lost: the compiler refuses unknown keys, and Deploy's
  settings are not the compiler's to read.
- **A declaration file inside the corpus folder.** Lost: it would be ingested as one more source
  and change the package it configures.
- **Calling `map_package` in process.** Lost: it ties the harness to Deploy's internals; the
  command line is Deploy's published interface, and running it as a user does catches packaging
  and entry-point breaks.
- **Failing on every Deploy finding.** Lost: an unmapped PDF table is expected coverage, not a
  break; zero records and a silent declaration are the breaks.
- **Keeping 1.1.0 by leaving the old keys in the calibration files.** Lost: the files would no
  longer be what easy_handeye writes, which is the point of the rewrite.
- **The `source` selector for the assertion.** Lost: it cites the file rather than the person's
  statement, and cannot tell two assertions of one file apart.

## Consequences

- Memory's acceptance snapshot can be built from the harness's two packages, compiled and mapped,
  instead of its own ingest chain. Its generator must add the mapped package to its Ledger export.
- A Deploy PR that renames or drops a preset the corpus declares turns the platform job red until
  `deploy.json` changes. A new template or preset is a `deploy.json` edit plus, when it maps new
  kinds, an `at_least` entry.
- When the compiler reads easy_handeye (MVL-207), the calibration files stop yielding
  `configuration_value` records. That PR moves `cal.*.z` to the `calibration` selector, which is
  a corpus major (ADR 0007 consequences), or ships a gold change with a new corpus version.
- Revisit when Deploy publishes a contract of its own, or when a stage after Memory needs the
  mapped package by a name other than `<case>.deploy`.
