# The integration harness

One command that checks the contracts, flows a corpus through compiler, deploy, ledger, memory and context,
issues a smoke query and writes a report. Decision records: [ADR 0004](adr/0004-integration-harness.md) and, for
the deploy stage, [ADR 0008](adr/0008-harness-deploy-stage-corpus-declared-mappings-and-the-assertion-selector.md).
Code: `harness/`.
The default corpus is the versioned [acceptance corpus](acceptance-corpus.md) (ADR 0007).

## Run it

```bash
make setup                      # once
make harness                    # = uv run python -m harness; report in harness/.run/
uv run --all-packages python -m harness --run-dir /tmp/h --no-owner-tests   # faster: skip owners' pytest
uv run --all-packages python -m harness --corpus-name worked-examples       # the four small worked examples
uv run --all-packages python -m harness --corpus path/to/cases              # a folder of case folders
uv run --all-packages python -m harness --compose                           # start/stop the compose stack
```

No Docker is needed while every real stage runs in process: the compiler, Deploy's mapper (a subprocess of the
harness's interpreter), and the ledger on an embedded PostgreSQL from the `pgserver` wheel
([ADR 0006](adr/0006-real-ledger-stage-on-embedded-postgres.md)); memory and context are stubs. Exit 0 means: `check --all` passed, every stage is `ok`, and the smoke query returned a
packet. The summary line goes to stdout; the report
is always written.

CI is `.github/workflows/harness.yml`: merge queue, nightly, on demand, and pull requests that touch
`contracts/**`, `harness/**`, an exported schema or the code a real stage runs (the SDK, the Ledger, Deploy's
source). The pull request gets one comment (edited in place); the
nightly run comments on the Linear gate issues only when red; dispatch it by hand for a green record.

## Read the report

`harness/.run/report.json` (and `report.md`, the PR comment). Keys are sorted and nothing in it is a time, a host
or an absolute path, so a changed byte means changed behaviour.

| Key | Meaning |
|---|---|
| `ok` | contracts ok, every stage `ok`, smoke ok |
| `contracts` | `check --all`: `ok`, `exit_code`, the tool's `notes`, `problems` (and `output_tail` when red) |
| `corpus` | `name` (`acceptance <version>`, `worked-examples` or `custom`) and the case ids; for the acceptance corpus also `version`, `tree` (its id), `locked` and `problems` (a build that does not match `corpus.lock.json` makes the run red) |
| `stages[]` | in order: `mode` **real** or **stub**, `reason` (what decided it), `contract` and `contract_version`, `needs_services`, `status` (`ok`, `failed` = the stage's own checks, `error` = it raised or needs services that are down, `skipped` = an upstream stage did not pass), `problems`, `output` |
| `smoke` | `query`, `packet_source` (`golden ...` or `canned: ...`), `packet`, `ok` |

The compiler's `output.cases[]` has, per case: `state`, the `package` and `receipt` ids, `sources`, `findings` by
code (partial success: an unsupported or corrupt source is a finding, not a failure), and whether the package
verified and its manifest and receipt validate against the registry's package-schema. A case with gold
answers (the acceptance corpus) also has `gold`: how many evidence items resolved against its package and which
did not (each one missing fails the stage; [acceptance corpus](acceptance-corpus.md)).

<a id="deploy-stage"></a>The deploy's `output.cases[]` has, per case: `declared` (whether the case names a Deploy declaration;
nothing more for one that does not), and for one that does the `presets` and `templates`, `state`, the mapped
`package` id, `package_verified`, `manifest_valid` and `receipt_valid`, `records` (lifecycle records by kind),
`by_declaration` (records per `preset:<name>` and `template:<path>`) and `findings` by code. Deploy's findings
(`table_unmapped`, `row_unmatched`, `column_unmapped`, ...) are what no mapping read: they never fail the stage.
It fails when the map exits non-zero, a declared preset is not shipped, the package does not verify or validate,
it holds no lifecycle record, a declaration mapped none, or an `at_least` count is not met. The mapped package
is `work/packages/<case>.deploy`.

The ledger's `output.cases[]` has, per registered package (each compiled one, then each mapped one, labelled
`<case>.deploy`; `stage` says which): `registration` (must be `registered`), `reregistration` (must be
`already_registered`), `verify` (must be `intact`), `tx_seq`, the package's `schema_version` (at most the major
`neptune-ledger` locks), `records`, `files_checked`, `registration_findings` by code, and `responses_valid`
(every response validates against the registry's catalog-api schema). `locked_package_schema` is the lock it
checked against.

When red: `contracts.problems` is a registry or owner-test failure (fix it with `scripts/contracts.py`, ADR 0002);
a stage `failed` names the case; a stage `error` with "needs ..." means start the compose stack.

## Map a corpus with Deploy

A case maps with Deploy when it carries a declaration (`Case.deploy`). The acceptance corpus's is
`harness/acceptance/deploy.json`:

```json
{"deploy_format": 1, "corpus": "acceptance", "corpus_version": "2.0.0",
 "presets": ["cmms_generic", "jira_json", "register_zone", "servicenow_csv"],
 "templates": [], "at_least": {"maintenance_event": 15}}
```

Add a shipped preset by name, or a template file or directory by its repository path, and raise `at_least` for
the kinds it yields. A template outside Deploy's `src/` also needs a path in `harness.yml` and in `ci_plan.py`'s
`DEPLOY_STAGE_INPUTS`: `test_the_deploy_stages_code_and_declarations_run_the_harness_and_the_platform_job` fails
until both cover it. A red deploy stage stops the run before the ledger; its report still names the mapped
package and its counts.

## Add a stage

1. Write its driver in `harness/stages.py`: `def driver(ctx: Context) -> Outcome`. It reads `ctx.registry`,
   `ctx.work` (scratch), `ctx.cases` and earlier outputs (`ctx.upstream[stage_id]`), and returns
   `Outcome(output, problems)`: output is JSON-able, deterministic and path-free; any problem fails the stage.
2. Add `Stage(id, package, contract, needs_services, real, stub)` to `STAGES`, in flow order. `contract` is
   the contract the package owns (or, for a package that owns none, the one it writes); its `[owner]` entry
   point and version constant decide real versus stub. For a package that owns none, `entry` names a module a
   real run also needs and `built_against` its constant for the contract version (the deploy stage's are
   `neptune_deploy.lifecycle` and `neptune_deploy:PACKAGE_SCHEMA_VERSION`).
3. Give it a stub: `_golden_stub(contract_id, consumes)` serves the contract's goldens (or a canned marker).
4. Add the package's exported-schema path to the `pull_request` filter in the workflow if it is new (the
   workflow test lists any contract module that is not covered).

## Swap a stub for a real stage

1. The package ships the module named in its `contract.toml` (`[owner].module`), its version constant, and the
   contract is published in `contracts/` at that version; `contracts/lock.toml` has the package's entries.
2. Write the real driver and pass it as the stage's `real`. Until then the stage stays a stub even if the
   package is installed, with the reason "the harness has no real driver for ...".
3. If the real stage needs plain PostgreSQL 16 only, start an embedded one as the ledger does (ADR 0006) and
   set `needs_services=False`. If it needs AGE, pgvector or MinIO, keep `needs_services=True` and, in the
   same PR, start the stack in `harness.yml` (`docker compose -f harness/compose.yaml up -d --wait` before
   `make harness`, and `down` after); `test_ci_needs_no_docker_while_no_stage_that_needs_services_is_real` fails until you do.
4. Run `make harness`: the report must say `real` for that stage, with the contract version it matched.

## Gates

Every gate issue states "harness green at <sha>, corpus acceptance <version> (tree <id>)" in its acceptance:
run `make harness` (or dispatch the workflow), and quote the report's corpus line and stage modes
([ADR 0007](adr/0007-acceptance-corpus-layout-versioning-and-gold-answers.md) §4).
