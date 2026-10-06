# 0011 — Demo v1: real memory and context stages, pinned cited answers, `make demo` and a quickstart CI runs

- Status: Accepted
- Date: 2026-10-07
- Issue: MVL-191
- Amends: [0004](0004-integration-harness.md) §3, §4 and §6 (memory and context are real, in process;
  the smoke query); [0006](0006-real-ledger-stage-on-embedded-postgres.md) §1 (the catalog outlives the
  stage); [0007](0007-acceptance-corpus-layout-versioning-and-gold-answers.md) §6 (a consumer's scorer)

## Context

The Demo v1 gate (MVL-191) asks for one command that runs the acceptance corpus through every layer and
renders the MVL-161 incident timeline, gold questions answered through Context's agent surface with every
claim cited and checked in CI by structure, and a README quickstart a new user can follow. Forces:

- **The memory and context stages were stubs.** They served goldens, so nothing proved that the graph
  Memory builds from the harness's packages is the one Context answers over, or that an agent gets cited
  answers to the gold questions.
- **Memory already builds the acceptance snapshot.** Its generator
  (`packages/neptune-memory/tests/fixtures/acceptance_corpus_snapshot.py`) runs the harness's compiler and
  deploy stages, registers both packages in a throwaway Ledger, asks `threads_of` for every record and runs
  `memory rebuild --with-estimates --config`. A second consolidation path would drift from it.
- **Answers are text.** An LLM's or a renderer's wording must never decide a gate. What an answer cites is
  structure: Context's renderer ends each statement with `[I…][E…]` keys whose footers give claim ids and
  evidence refs, and `render.agent.parse_answer` reads them back.
- **Gold answers cite evidence by path and selector, not record ids** (ADR 0007 §5): a claim id moves with
  every compiler or Memory change, a path and row do not.
- **Not every gold claim is answerable today.** Bag diagnostics are not records (MVL-204), document text is
  not consolidated, the envelope register names no configuration. A gate that hides that is worse than one
  that names it.
- **A quickstart in a README rots** unless something runs it.

## Decision

1. **Memory, real, in process** (`harness/consolidate.py`). After the ledger stage, every registered
   package becomes Memory's Ledger export (`neptune_memory.ledger.LedgerExport`): its records read back
   verified in file order, at the transaction the catalog registered it at, a compiled package's
   `derived/clock_mapping` lines, and the catalog's own `threads_of` answer for every record. Then
   `python -m neptune_memory.cli rebuild --snapshot <head> --with-estimates [--config <declaration>]` and
   `memory verify`. Only the export's assembly is the harness's; consolidation is Memory's command line.
   The case's Memory declaration (`Case.memory`) and committed snapshot (`Case.snapshot`) are read where
   Memory keeps them (`packages/neptune-memory/tests/fixtures/acceptance_corpus.{memory_config.json,
   graph.json.gz}`), never copied: Memory owns its consolidator configs (Memory ADR 0013 §5). For the
   acceptance corpus the graph must equal the decompressed snapshot **byte for byte**, or the stage is red:
   that is the pipeline's determinism check. The graph is `work/memory/graph.json`.
2. **The catalog outlives the ledger stage.** The ledger stage keeps its embedded server and database
   (`Context.ledger_uri`) for the memory stage's `threads_of` and the context stage's hydration; the run
   stops it at the end (`Context.close`, also on error). `needs_services` is false for every stage: CI
   still needs no Docker. The memory stage's `entry` is `neptune_memory.cli`, the context stage's
   `neptune_context.mcp`.
3. **Context, real, through the MCP tools** (`harness/agent.py`). The engine is `LocalEngine` over the
   graph with the ledger's catalog attached; the server is `neptune_context.mcp.build_server`, called
   through an in-memory MCP session: the tools, arguments and text Claude Code gets. The smoke query is the
   first gold question's first query (the graph's first declared identity for a corpus without answers),
   and the report keeps the packet's id and counts. One stdio round trip proves the Claude Code path:
   `python -m neptune_context.mcp --memory <graph>` is spawned, lists its six tools and answers that query
   with the same text as the in-process server without a catalog (as the sample `.mcp.json` runs it).
4. **Pinned answers, checked by structure** (`harness/acceptance/answers.json`, `answers_format: 1`,
   naming its corpus version and the graph `generation` it was pinned against). Per gold question: the
   phrasing it is `asked_as`, the tool `calls` an agent makes (subjects by declared id only, never a
   content-addressed id; an argument `"$support:<gold claim>"` is the first claim id that supports that gold
   claim in the question's earlier answers, as an agent copies one from an Items footer), and per gold
   claim either `supported` (the sorted claim ids whose statements support it) or a `gap`
   (`{reason, in_graph}`). A statement's citations, in ADR 0007 §6 terms, are the record ids its claims
   rest on (their `provenance.records` and record objects) and, per evidence ref it cites, the source's
   corpus path with its row (`row`, `row_cell`), page or JSON pointer; support is
   `harness.acceptance.resolve.supports`. The context stage is red when an answer is an error, a statement
   cites no item or no evidence, supports differ from the pins, a gap has no reason, its `in_graph` (does
   any claim of the graph support it, cited or not) is wrong, an answer supports a `must_not_cite` item, a
   question's first cited source does not hydrate (`neptune_hydrate`, status `resolved`), or the pinned
   generation is not the graph's. A separate file, not a selector in `gold.json`: gold stays
   compiler-version-proof (ADR 0007), the pins are graph-specific and regenerated by `make demo-pin`
   (which keeps gap reasons and blanks a new gap's, so the check refuses it until a person writes one).
   Corpus 2.1.0 pins 28 of 40 gold claims supported and 12 gaps.
5. **`make demo`** (`python -m harness.demo`): the harness over the acceptance corpus with every stage
   real (owner contract tests skipped; `make harness` runs them), then Deploy's published `pack` command
   over the graph just built: `incident-timeline@2` for INC-C3-0011 (Memory's event node for the incident
   record Deploy mapped from the report, on the clock its timeline entries state, first entry to last) and
   `configuration-traceability@2` for `manifest:ARM-3A` (on its latest run's clock). Outputs in `demo/`
   (git-ignored): the two PDFs, `graph.json`, `answers.json` and `answers.md` (every call and cited text),
   `report.json`/`report.md`, `demo.md`, `packs/<name>/` and `work/`. No clock, host or path in any of
   them: a second run gives the same bytes (tested).
6. **The quickstart is a script CI runs.** `scripts/quickstart.sh` holds the README's quickstart between
   markers; `test_quickstart_readme.py` fails when the README's block differs, or when the README's status
   counts differ from the pins. `harness.yml`'s `quickstart` job runs it on a runner with nothing set up
   (it installs uv, runs `make setup` and `make demo`) with a 15-minute timeout, the README's promise. No
   LeRobot step until Learn (Demo v2).
7. **CI paths.** `harness.yml` and `ci_plan.py`'s `AGENT_STAGE_INPUTS` cover Memory's and Context's source
   and project files, Memory's acceptance declaration and snapshot, and Context's `claude/` config and
   skill; `QUICKSTART_INPUTS` (the README and the script) and `AGENT_STAGE_INPUTS` run the
   `neptune-platform` job, whose slow tests run the demo. A PR that regenerates Memory's acceptance snapshot
   therefore runs `make demo-pin` and commits `answers.json` in the same PR, and its diff shows which gold
   claims gained or lost support.

## Alternatives considered

- **Import Memory's snapshot generator.** Lost: it is a test fixture of another package, and it registers
  in its own Ledger; the stage must consolidate what the harness's ledger stage registered.
- **Copy Memory's config into `harness/acceptance/`.** Lost: two copies of one declaration drift, and a
  Memory change would turn the harness red on `main` instead of in Memory's PR.
- **Score answer text, or ask an LLM to grade it.** Lost: not deterministic; the citation keys are.
- **Pin exact cited claim sets per question.** Lost: hundreds of ids per question that say nothing about the
  gold answers; pinning the supporting ids per gold claim is the load-bearing part.
- **A claim-id selector in `gold.json`.** Lost: gold answers would change with every compiler and Memory
  regeneration (ADR 0007's reason to cite paths).
- **Render the PDFs as a sixth stage.** Lost: packs are a Deploy product over a graph, not a contract seam;
  the demo runs them after the harness.
- **Run the quickstart with a cached uv.** Lost: it would not prove the clean-laptop path the README promises.

## Consequences

- The harness's every stage is real; the stubs remain for a package that falls behind its contract.
  Report: memory `output.{claims, generation, sha256, snapshot.verdict, verify, packages}`; context
  `output.{answers.<Q>, stdio}`; `smoke.packet` is the real packet's summary.
- A run is about 25 s for the acceptance corpus; the platform job's slow tests run the demo twice
  (determinism) and the harness a few more times.
- Memory regenerating its snapshot, or the compiler or Deploy moving it, now also moves the pins: that PR
  runs `make demo-pin`. A gold claim losing support is visible in review, not silent.
- Gaps name their owners (MVL-204 for bag diagnostics, document text, the envelope register); when a gap
  closes, the pin moves it to `supported` and the README's counts change with it.
- Revisit when Context gains a lexical channel over document text (more gaps close), when the pins churn
  more than reviews can follow (pin by claim descriptor instead of id), or when the harness becomes a
  required status.
