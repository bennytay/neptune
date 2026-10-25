# Site model v3: the M1 spec

The design every M1 lane (#182–#187) builds against. Demo-only rules apply (`docs/site-model/README.md`).
Where this spec is silent, choose the simplest thing that makes a gold question pass, and say so in the PR.

## 1. What it is

- A live, cited model of one site: every robot, machine, person (by role), shift, task, zone, document and
  rule, plus what feeds or depends on what.
- It is a **pure fold over an append-only fact log**. Folding the same log gives the same model, whether
  live (one entry at a time) or from scratch.
- Claude queries it through MCP tools. Every answer carries citations and observed/stated/inferred labels,
  and says `stale`, `unknown` or `ambiguous` instead of guessing.

```
corpus files ──(compiler ingest)──► packages ──(mappers)──┐
feed messages ──(replay)─────────────────────(mappers)────┼──► fact log ──fold──► SiteModel ──► query ──► MCP
neptune confirm (yes/no) ─────────────────────────────────┘
```

## 2. Where the code lives

- v3 lives in `packages/neptune-memory/src/neptune_memory/site/` (v2 and Demo v1 were deleted in #213).
- Tests go in `packages/neptune-memory/tests/site/`. There is no `__init__.py`, so basenames must be unique
  repo-wide: prefix them `test_site_` (helpers: `site_`).
- `site/facts.py` holds the shared types and is **frozen by this PR**. Lanes import it; changing it needs a
  one-line note in the PR.
- No new dependencies. SQL uses stdlib `sqlite3`.
- CLI: `python -m neptune_memory.site <command>`, with `make site-demo` wrapping the demo path (§9).

## 3. The fact (`site/facts.py`, frozen)

| Field | Type | Meaning |
|---|---|---|
| `subject` | `str` | entity ref `"<type>:<id>"`, e.g. `machine:cv-2`, `person:okafor` |
| `predicate` | `str` | from the vocabulary (§4) |
| `object` | `Ref \| Value` | `Ref(ref)` makes an edge; `Value(value, unit)` is a literal (unit verbatim or `None`) |
| `valid_from` | `str \| None` | site-clock time `YYYY-MM-DDTHH:MM:SS`; `None` = not stated |
| `valid_to` | `str \| None` | stated end only. Views close an interval at the next change; facts never do |
| `kind` | `"observed" \| "stated" \| "inferred"` | observed = a machine reported it; stated = a person or document says so; inferred = proposed by Neptune or a model |
| `source` | `str` | source id from `layout.yaml` (`plc.cv2`, `fleet`, `cmms`, `roster`, `docs`, `photos`, `confirm`) |
| `citations` | `tuple[Citation, ...]` | ≥1 |
| `model` | `str \| None` | for model-proposed facts: `"<model id>@<prompt sha256[:12]>"` |

- `Citation(file, locator, quote)`:
  - `file` is the corpus-relative path.
  - `locator` is `line 812`, `row 7 col "Problem"`, `page 2 block 3` or `region 120,40,380,90`.
  - `quote` is the cited text or value as written (`None` for an image region).
- `fact_id = "fact:" + sha256(canonical JSON of every field)[:16]`. Canonical JSON = sorted keys, no spaces,
  UTF-8.
- **Times:** one declared site clock (`layout.yaml: clock`), ISO local wall time with no zone. Times are
  compared as strings and never converted.

## 4. Vocabulary (`site/vocab.py`, #184)

**Entity types:** `site`, `area`, `cell`, `zone`, `robot`, `machine`, `person`, `role`, `qualification`,
`shift`, `task`, `work_order`, `change`, `document`, `rule`, `source`.

| Predicate | Subject → object | Card. | Kind in corpus |
|---|---|---|---|
| `part_of` | area/cell/zone → site/area | one | stated (layout) |
| `located_in` | robot/machine → cell/zone | one | stated (layout) |
| `feeds` | machine/cell → cell/machine | many | stated (layout) |
| `depends_on` | anything → anything | many | stated (layout) |
| `covers_zone` | rule/role → zone | many | stated |
| `assigned_to` | task → robot | one | observed (fleet) |
| `name` | any → text | many | stated |
| `serial` | robot/machine → text | one | stated (CMMS) / observed (fleet) |
| `mode` | robot/machine/cell → text | one | observed (state interval) |
| `health` | robot/machine → text (`ok`, `fault:<code>`) | one | observed (state interval) |
| `current_task` | robot → task | one | observed (state interval) |
| `in_zone` | robot → zone | one | observed (state interval) |
| `battery` | robot → value % | one | observed (state interval) |
| `setpoint` | machine → value (unit) | one | observed |
| `firmware` | robot/machine → text | one | observed |
| `parts_out` | cell → value (count, cumulative) | one | observed |
| `task_state` | task → text | one | observed |
| `has_role` | person → role | many | stated (roster) |
| `holds_qualification` | person → qualification | many | stated (roster) |
| `on_shift` | person → shift (interval = shift times) | many | stated (roster) |
| `on_break` | person → text `break` (interval) | many | stated (roster) |
| `requires_qualification` | rule/document → qualification | many | stated (doc span) |
| `states_limit` | document → value, e.g. `45–55 N` | many | stated (doc span) |
| `rule_text` | rule → text (verbatim clause) | one | stated (doc span) |
| `work_order` | machine/robot → work_order | many | stated (CMMS) |
| `wo_action`, `wo_cause`, `wo_by`, `wo_loto` | work_order → text/person | one | stated (CMMS) |
| `change` | machine/robot → change | many | stated (change log) |
| `note` | any → text | many | inferred (photo) until confirmed |
| `finding` | any → text | many | inferred (insight rules, §8a) |
| `same_as` | any → same type | many | inferred proposal → stated on confirm |
| `report_interval` | source → value seconds | one | stated (layout) |

- **Cardinality `one`:** if two current facts disagree, the one with the higher kind wins (observed = stated
  > inferred). If they tie, the view reads `ambiguous`, citing both. `many` never conflicts.
- **State intervals:** a feed mapper emits a fact **only when the value changes** for
  (subject, predicate, source). `valid_from` is the message time. Every message still logs a `seen` entry
  (§5), which is what freshness reads.

## 5. Fact log (`site/log.py`, `site/build.py`, #183)

- **Location:** `<site_dir>/.neptune/log/segment-000001.jsonl` …, rolling every 10,000 entries. Append-only;
  nothing is rewritten.
- **One JSON line per entry:**
  `{"seq": 17, "type": "fact"|"seen"|"confirm", "fact": {...}|null, "seen": {"source": "...", "t": "..."}|null, "confirm": {"proposal": "fact:…", "answer": "yes"|"no", "by": "..."}|null}`
- `seq` starts at 1 and means "when we knew it" (transaction time). It is never a wall clock.
- **Static files**, in `build(site_dir)`:
  1. `neptune.sdk.ingest(site_dir / "static", site_dir / ".neptune/package", isolation="in_process")`;
     then `neptune.store.package.read_package(...)`.
  2. Hand each record to `mappers.map_record(record, package, mapping) -> list[Fact]`, where `mapping` is the
     site's declared mapping file (§12). Records arrive in package order
     (stable), and citations come from each record's `provenance` evidence locator.
  3. `layout.yaml` is read directly by `mappers.map_layout(path)`. It is the declared site, so its facts are
     stated.
- **Feed messages:** `replay.messages(site_dir)` (#182) yields `FeedMessage` in arrival order. For each one,
  append `seen`, then the facts from `mappers.map_message(msg)`.
- **Entry points:**
  - `build(site_dir, *, until=None) -> Path` does static first, then the feed up to `until` (site time).
  - `replay(site_dir, *, speed=60.0, until=None)` does the same live: it sleeps `Δt/speed` between messages
    and appends as it goes.
- **Identity proposals:** after a batch of facts holding a new `serial`, the build appends the `same_as`
  proposals it now implies (§7), so a live replay shows a proposal as soon as its second serial lands.
- **Recorded model output:** the whiteboard extraction is a file in the corpus (§8), so the log never calls a
  model.

## 6. Views (`site/views.py`, #185)

- `SiteModel.empty().apply(entry) -> SiteModel` is incremental. `fold(entries) = reduce(apply)`.
- `load(site_dir, *, as_of_seq=None) -> SiteModel` folds the log prefix up to `as_of_seq`.
- **now / `state_at(t)`:** for each (subject, predicate), the latest fact with `valid_from ≤ t`. Its state is:
  - `known`;
  - `stale`, when the source's last `seen ≤ t` is older than 3 × `report_interval`. The answer also carries the
    last value and `since`;
  - `unknown`, when no source has stated it but the vocabulary expects it for this entity type, e.g. a robot's
    `battery`;
  - `ambiguous` (§4).
- **history:** intervals per (subject, predicate), each `[valid_from, next change)` on the site clock.
- **topology:** a graph over `feeds`, `depends_on`, `located_in`, `part_of`, `assigned_to` and `covers_zone`.
- **identity:** stated `same_as` facts are joined by union-find. Inferred proposals stay apart until confirmed.

## 7. Proposals and `confirm` (`site/identity.py`, `site/confirm.py`, #186)

- **Identity proposals:**
  - Two entities with the same `serial` from different sources get an inferred `same_as` fact, citing both.
    In the corpus that is fleet `robot:amr-02` and CMMS `robot:MR-1142`.
  - No auto-merge.
- **Photo facts:** each region in the recorded extraction becomes an inferred `note`, with `model` set and a
  region citation.
- **Document facts:** a value the mapper finds verbatim in a PDF block is **stated** directly, citing page,
  block and quote. Nothing goes through a model.
- **`python -m neptune_memory.site confirm <site_dir>`:**
  - It lists pending proposals; `--yes ID` / `--no ID` appends a `confirm` entry.
  - The fold turns a yes into a **stated** copy of the fact, with `source="confirm"` and the proposal's
    citations plus `confirm` line N.
  - Python: `confirm(site_dir, proposal_id, answer, by="operator")`.

## 8. Query API (`site/query.py`, `site/render.py`, #187)

- All functions take a `SiteModel` and return an `Answer`. Times are site-clock strings; `at=None` means the
  latest `seen` time in the log.

```python
def resolve(model, name: str) -> Answer                      # names/ids → entities (same_as joined, proposals listed)
def state_at(model, entity: str, at: str | None = None) -> Answer        # every one-predicate's state at t
def find(model, predicate: str, obj: str, at: str | None = None) -> Answer  # who/what has predicate=obj at t (obj: a ref, or the value as text)
def timeline(model, entity: str, start: str, end: str, predicates: list[str] | None = None) -> Answer
def why(model, fact_id: str) -> Answer                       # the fact, its citations, what it beat or ties with
def diff(model, start: str, end: str, entities: list[str] | None = None) -> Answer  # facts that began in (start, end]
def gaps(model, at: str | None = None) -> Answer             # stale, unknown, ambiguous, pending proposals
def impact(model, entity: str, at: str | None = None, depth: int = 3) -> Answer  # downstream via topology, with states
def search_evidence(model, text: str, limit: int = 20) -> Answer  # keyword over citation quotes + document text
def sql(model, query: str) -> Answer                         # sqlite3 over tables: facts, intervals, seen, entities
```

- `Answer(tool, at, as_of_seq, items, gaps, sources)`:
  - `items` are `Item(fact_id, subject, predicate, object, valid_from, valid_to, kind, state, cite)`. `state` is
    one of `known|stale|unknown|ambiguous|pending`; `cite` is a list of keys into `sources`.
  - `sources` maps `S1…` to a `Citation`, numbered in order of first use.
- `render(answer) -> str` produces compact lines: `- machine:cv-2 mode = "stopped" since 10:05:02 · observed
  [S1]`, followed by a `Sources:` footer.
- **MCP** (`packages/neptune-context/src/neptune_context/mcp/site.py`):
  - The server gains tools `neptune_site_<name>` for each function above, each with a JSON object input that
    mirrors the Python arguments and returns `render(answer)`.
  - They are enabled by `python -m neptune_context.mcp --site <site_dir>`.

## 8a. Insights (`site/insights.py`, #203)

- **Rules, not a model.** Each rule reads the views through the generic vocabulary and topology, never a
  site's ids, and writes **inferred** facts: predicate `finding`, `source="neptune.insights"`, `model=None`.
  A finding's text opens with its rule, and it cites the citations of the facts that support it.
  - **starvation chain:** a `fault:` health, then something downstream (topology, robots in a reached zone,
    their tasks) goes idle, starved, waiting or blocked within 120 s.
  - **qualification gap:** during a fault, no on-shift, not-on-break person holds a qualification that a
    document or rule requires. The document or rule must name the entity (one of its stated names) or cover
    its zone.
  - **stale source:** a source, or one fleet device, is silent for more than 3 × `report_interval` while
    other sources keep reporting.
  - **change before fault:** a `change`, `work_order`, `setpoint` or `firmware` change on the same entity in
    the 24 h before a fault. This is a correlation, never a cause.
  - **regulation check:** a work order against a rule clause that shares its words. It checks lockout (when
    the clause names it) and the qualification of the person who did the work.
- `findings(model, at)` reads only what was true by `at`. `insights(model, at)` and
  `shift_report(model, shift_id | start, end)` return `Answer`s. The report sets `Answer.sections`, and
  `render` prints those as headings.
- `python -m neptune_memory.site insights <site_dir>` appends the findings the log does not hold yet. It is
  idempotent.
- `sql` also has `downtime(entity, state, start, "end", seconds, cause, work_order, fact_id)`,
  `utilisation(robot, mode, seconds)` (only time the source was reporting) and
  `fault_clear_time(entity, fault, start, "end", seconds, fact_id)`.
- **MCP:** `neptune_site_insights {at}` and `neptune_site_shift_report {shift_id | start, end}`. Gold:
  `tests/site/gold_insights.json`.

## 9. The live-site corpus (`site/corpus/`, `site/replay.py`, #182)

- `python -m neptune_memory.site generate <out>` (Python: `neptune_memory.site.corpus.generate(out: Path)`) writes the corpus. It is deterministic: same code, same
  bytes. PDFs reuse the writer in `tests/fixtures/pdf/make_pdfs.py`, loaded by path, as Deploy's document
  fixtures do.
- `make site-demo` = generate into `site-demo/` → `build` → `confirm --yes-all` (the amr-02/MR-1142 proposal, as
  the operator would) → print the MCP command.
  `make site-replay SPEED=60` = generate, then build live (the proposal stays pending).

**Site `site:p7`, "Plant 7 assembly hall"**
- **Clock:** `P7 local` (zone `Europe/Berlin`, stated, never applied).
- **Two shift-A windows:** 2026-10-19 06:00–14:00 (last good shift) and 2026-10-20 06:00–14:00.
- **Places:** areas `area:inbound`, `area:assembly`; `cell:c3` (arm cell); zones `zone:aisle-a`,
  `zone:c3-fence`, `zone:charging`.
- **Machines:** conveyor `machine:cv-2` (PLC, Sparkplug-like); arm `robot:arm-c3` (controller log); AMRs
  `robot:amr-01..03` (fleet manager, VDA 5050-like state).
- **Topology:** `machine:cv-2 feeds cell:c3`; `cell:c3 feeds zone:aisle-a` (AMR pickup);
  `robot:arm-c3 located_in cell:c3`.
- **People** (roster):
  - Okafor: maintenance tech, holds `arm-reset-c3` and `conveyor-lockout`, on shift, break 10:30–11:00.
  - Lindqvist: maintenance tech, holds `conveyor-lockout` only, on shift.
  - Patel: line lead.

| Path | What | Becomes |
|---|---|---|
| `static/layout.yaml` | site, areas, zones, machines, topology, sources and `report_interval`, clock | stated facts (`map_layout`) |
| `static/neptune.mappings.yaml` | how every file below becomes facts (§12) | nothing itself |
| `static/cmms/assets.csv` | asset register: `MR-1141..1143` with serials (`SN-88412` = MR-1142) | `serial`, `name` (stated) |
| `static/cmms/work_orders.csv` | WO-5521 (10-19 15:20, CV-2 belt replaced, "tension per SOP-CV-04 rev B"); WO-5530 (10-20 10:08–10:41, cleared jam at PE3, by Lindqvist, LOTO yes, cause "belt tracking off") | work-order facts (stated) |
| `static/changes/changes.csv` | CHG-311: CV-2 speed setpoint 0.80 → 0.95 m/s, 10-20 05:40 | `change` (stated) |
| `static/roster/shift_roster.csv`, `qualifications.csv` | the people above | roster facts (stated) |
| `static/docs/SOP-CV-04_rev-B.pdf` | belt tension "45–55 N"; jam clearing needs `conveyor-lockout` | `states_limit`, `requires_qualification` (stated, verbatim) |
| `static/docs/SOP-C3-02.pdf` | arm restart after a starve or stop needs `arm-reset-c3` | `requires_qualification` (stated) |
| `static/docs/PSR-12_excerpt.pdf` | synthetic plant regulation §4.2: "Jams on powered conveyors are cleared only under lockout by a person qualified for conveyor lockout." | `rule:psr-12-4.2` `rule_text`, `requires_qualification` (stated) |
| `static/photos/whiteboard_2026-10-20.png` + `.extraction.json` | recorded model output: region "CV-2: PE3 dirty — clean every shift", model id, prompt hash | `note` on `machine:cv-2` (inferred) |
| `feed/plc_cv2.jsonl` | Sparkplug-like DDATA every 5 s: `state`, `alarm`, `speed_setpoint`, `parts_out` (cell counter) | mode, health, setpoint, parts_out |
| `feed/fleet.jsonl` | VDA 5050-like `state` every 5 s per AMR: `operatingMode`, `batteryState.batteryCharge`, `errors`, `orderId`, `zone`, `serialNumber`, `version` | mode, battery, current_task, in_zone, serial, firmware, task_state |
| `feed/arm_c3.jsonl` | controller log lines: mode changes, protective stop, reset | mode, health |

- **Feed envelope** (`FeedMessage`): one JSON line per message:
  `{"t": "2026-10-20T10:05:02", "source": "plc.cv2", "topic": "spBv1.0/P7/DDATA/cv2-edge/cv2", "payload": {...}}`.
  Arrival order is `(t, source, line)` across files.

**2026-10-20 timeline** (2026-10-19 runs clean at about 1 part per 40 s)
- **05:40:** setpoint 0.80 → 0.95 (CHG-311). **05:50:** amr-03 firmware 4.1.0 → 4.2.0.
- **06:00–10:05:** normal. The cell's `parts_out` rises about 90 an hour.
- **09:40:05:** amr-03's last message, battery 22% at `zone:charging`. After this it is **stale**.
- **10:05:02:** CV-2 alarm `JAM_PE3`, mode `stopped`, health `fault:JAM_PE3`.
- **10:05:30:** arm-c3 mode `idle_starved`. **10:06:** amr-01 and amr-02 sit `waiting` on order T-8812 in
  `zone:aisle-a`.
- **10:08:** WO-5530 opened. **10:30–11:00:** Okafor on break.
- **10:41:** CV-2 running again. **10:41:20:** arm-c3 `protective_stop`, awaiting reset.
- **11:04:** arm-c3 reset (controller log names user `okafor`), mode `running`. **11:10:** `parts_out` growing
  again.
- No source ever reports CV-2 belt tension, so it is **unknown**.

## 10. Gold questions (`tests/site/gold_questions.json`, `tests/site/test_site_gold.py`)

- There are eight, each with an expected item set (subject, predicate, object, kind, state) and the expected
  cited files.
- They pass since #188 (no xfail). Tests call the §8 API. The answers must cite the files
  listed; wording is never checked.
- Harbor DC (`generate --site harbor`, #201) has five more (`tests/site/harbor_gold_questions.json`), and
  insights have three for Plant 7 (`gold_insights.json`) and two for Harbor (`harbor_gold_insights.json`:
  the sorter's starvation chain, Reyes on break). All pass since #204, through the same code: the two sites
  differ only in their `static/neptune.mappings.yaml` and `layout.yaml`.

## 11. Lane ownership

| Issue | Owns | Needs |
|---|---|---|
| #182 corpus + replay | `site/corpus/`, `site/replay.py` (`FeedMessage`, `messages`, `pace`), `site/__main__.py` (`generate`), `Makefile` targets `site-demo`/`site-replay` | spec |
| #183 fact log | `site/log.py`, `site/build.py`, `__main__` `build`/`replay` | `facts.py`; stub mappers until #184 |
| #184 site data model | `site/vocab.py`, `site/mappers/` (`map_layout`, `map_record`, `map_message`) | `facts.py`, corpus |
| #185 views | `site/views.py` | #183, #184 |
| #186 identity + confirm | `site/identity.py`, `site/confirm.py`, `__main__` `confirm` | #184 (and #185 to show merges) |
| #187 query + MCP | `site/query.py`, `site/render.py`, `neptune_context/mcp/site.py`, `--site` flag, the skill text | #185 |
| #188 gate | flips the gold xfails, adds one replay test, `make site-demo` end to end; v2 stays (Context and Demo v1 still import it) | all |

- Shared files (`site/__main__.py`, `Makefile`): add your subcommand or target only; don't reorder the others.

## 12. Mappings: config, not code (`static/neptune.mappings.yaml`, #200)

- Every site-specific id, path, column and topic lives in **one file per site**,
  `<site_dir>/static/neptune.mappings.yaml`. `site/mappers/` holds only the generic machinery: a new site with
  new exports needs a mapping file, never code. `layout.yaml` stays the declared site; it is not a mapping.
- The file sits in `static/`, so it is ingested and citable like everything else, but it maps to no facts.
  A site with no mapping file maps only `layout.yaml`; everything else is unmapped (below).
- **Templates** (`"robot:{topic[-2]}"`) fill each `{name}`, then apply `|filters` left to right:
  - `lower`; `slug` (spaces → `-`); `until:<c>` (the text before the first `<c>`);
  - `type`: free text → entity type, through `types`;
  - `tag`: an equipment tag (`CV-2`) → `<type>:<tag lowercased>`, through `tags`.
- Names: in a table, `{Column}` is a cell as written (column names match case-blind, spaces = `_`). In a feed,
  `{source}`, `{topic}`, `{topic[N]}` (the `/`-split segment, negative from the end) and `{payload.a.b}`. In a
  document, `{stem}` (file name without extension), `{id}` (the document id without its type) and `{1}`, `{2}`
  … (the rule pattern's groups).

```text
mappings: 1                              # format version

types:                                   # `|type`: the first rule whose word the text contains
  - {contains: robot, type: robot}
  - {type: machine}                      # no `contains`: the fallback
tags:                                    # `|tag`: the register row whose `name` cell holds the tag as a word
  table: cmms/assets.csv                 #   gives the type (its `type` cell, through `types`);
  name: Name                             #   no such row: machine
  type: Type

tables:                                  # CSV/XLSX rows (header = row 1), JSONL lines, JSON arrays
  - file: cmms/work_orders.csv           # glob under static/; the first matching entry wins
    source: cmms                         # the facts' source id (layout.yaml `sources`)
    kind: stated                         # stated (default) | observed
    subject: "work_order:{WO}"           # the default subject of every fact below
    time: Opened                         # valid_from: a column, or a template; optional
    facts:                               # in order; a fact whose `column` cell is blank is skipped
      - {subject: "{Equipment|tag}", predicate: work_order, column: Equipment, ref: "work_order:{WO}"}
      - {predicate: wo_problem, column: Problem}          # object = the cell as written
      - {predicate: wo_by, column: Done by, ref: "person:{Done by|lower}"}
  - file: cmms/work_orders.json          # a JSON table: `rows` is the dotted path of its array
    rows: workOrders                     #   (none: a top-level array, or the object as one row)
    source: cmms
    subject: "work_order:{id}"           # columns are dotted paths (`asset.tag`), cited `json /workOrders/1/cause`
    time: opened
  - file: roster/shift_roster.csv
    source: roster
    subject: "person:{Person|lower}"
    facts:
      - {predicate: has_role, column: Role, ref: "role:{Role|slug}", once: Person}  # first row per Person
      - {predicate: holds_qualification, column: Certs, split: "|", ref: "qualification:{item}"}  # one per item
      - {predicate: on_break, column: Break start, value: break,
         time: "{Date}T{Break start}", until: "{Date}T{Break end}"}  # a template; None if a cell is blank
  - file: changes/changes.csv
    source: cmms
    subject: "change:{Change}"
    time: When
    facts:
      - {predicate: change_to, column: To, unit: "{Unit}"}  # a unit template; blank = none

documents:                               # PDF blocks, cited `page P block B`
  - file: docs/*.pdf
    source: docs
    id: "document:{stem|until:_}"        # the first block's text is the document's `name`
    rule:                                # a block matching `pattern` is a clause: `id` gets rule_text = group `text`
      pattern: '^§?\s*(\d+(?:\.\d+)+)\s+(.+)$'
      id: "rule:{id|lower}-{1}"
      text: 2
    lift:                                # values stated verbatim in a block
      - {pattern: '\d+\s?[–-]\s?\d+\s?N\b', predicate: states_limit, about: document}  # group 1 if any, else the match
      - {terms: {table: roster/qualifications.csv, column: Qualification},          # each term named in the block
                                                                    # (terms may `split: "|"` a cell)
         predicate: requires_qualification, ref: qualification, about: rule}         # (case-blind, `-` or space)
                                         # about: document | rule (the block's clause, else the document)
extractions:                             # recorded model output (§7)
  - file: photos/*.extraction.json
    source: photos
    predicate: note                      # subject: a `<tag>:` text prefix (through `tags`), else the image

feeds:                                   # one entry per envelope `source`
  - source: plc.cv2
    decoder: sparkplug                   # payload.metrics[]: {name, value, unit}; the metric's unit is kept
    subject: machine:cv-2
    fields:                              # name (glob) -> predicate, or {predicate, subject, unit, ref, state}
      state: mode
      alarm: health
      "*/parts_out": {predicate: parts_out, subject: cell:c3}
  - source: fleet
    decoder: vda5050                     # payload fields by dotted path (same as `json`)
    subject: "robot:{topic[-2]}"
    fields:
      batteryState.batteryCharge: {predicate: battery, unit: "%"}
      zone: {predicate: in_zone, ref: zone}                    # ref: <type> prefixes a bare id
      orderId: {predicate: current_task, state: orderState}
  - source: arm.c3
    decoder: controller_log              # as `json`; the quote is the line's `text`, except on `event: heartbeat`
    subject: robot:arm-c3
    fields: {mode: mode, fault: health}
  - source: ros.insp1
    decoder: ros2_diagnostics            # status[] as `<status name>/<key>` and `<status name>/level`
    subject: robot:insp-1
    fields:
      "insp1/thermal_scan */max_temp_c": {predicate: temperature, subject: "machine:{hardware_id}", unit: "°C"}
```

- **Decoders:** `sparkplug` (`metrics[]`), `opcua` (`nodes[]`: `name`, `value`, `eu` = unit), `json` (any JSON
  object payload), `vda5050` (= `json`), `controller_log`, `ros2_diagnostics` (a `DiagnosticArray`; level 0 =
  no fault code, else `WARN`/`ERROR`/`STALE`, quoting the status message; `{hardware_id}` names the status's
  device). A new one is a function registered by name in `DECODERS`, `site/mappers/feeds.py`.
- **Feed fields** are emitted when present and not null, and only on change (§4). `health` reads a fault code:
  `""` or `[]` = `ok`, else `fault:<code>` (a list's first `errorType`). `current_task` makes `task:<value>`,
  its `assigned_to` back, and `task_state` from the `state` field; a blank value is `none`.
- **Freshness per device:** when a feed's `subject` is a template (`robot:{topic[-2]}`), each message also logs
  `seen` for `<source>/<id>`, so one silent robot turns stale while its fleet still reports.
- **`time` / `until`** state when a fact holds. Leave `until` off for facts that stay true after the row's end
  (a work order's cause does not stop being its cause when it closes): views read a fact past its `until` as over.
- **YAML 1.1 reads a bare `on` key as `true`**; write `about:` (`on:` is accepted as an alias).
- **Unmapped files** (a CSV/TSV/JSONL/XLSX table or a document no entry matches) are still logged:
  `document:<path under static/>` gets `unmapped = "not mapped"` citing the file, plus one stated `text` fact
  per row or block, verbatim. So `search_evidence` finds them and `gaps` lists them as not mapped.
- **`python -m neptune_memory.site draft-mapping <site_dir> <file>`** prints a draft `tables:` entry (CSV or
  JSONL rows) or `feeds:` entry (feed envelopes) from the file's header and first rows, by deterministic
  heuristics. The skill says: draft → show the user → save into the mapping file → `build`.
- Plant 7's full file is `MAPPINGS` in `site/corpus/static.py`.

## 13. Live (#202)

- `python -m neptune_memory.site publish <site_dir> --broker mqtt://127.0.0.1:1883 --speed 60 [--start T]`
  replays the corpus feed to MQTT on each message's real topic; the payload is the feed envelope.
- `python -m neptune_memory.site live <site_dir> --broker ...` subscribes to `#`, starts a fresh log
  (`static/`, then any earlier recording), and per message records the envelope to
  `feed/live/segment-NNNNNN.jsonl` first, then appends `seen` + facts through `build.Appender` (the
  same `Seam` as `build`). Facts cite the recorded line, so `build` with `messages=live.captured`
  writes the same bytes. A new or changed file under `static/` re-ingests `static/` and appends only
  facts the log lacks.
- The MCP server tails the log (`log.Tail`) and applies new entries before each call;
  `neptune_site_changes(since_seq)` (`query.changes`) lists facts appended after a seq.
- `make site-live` runs `broker` (amqtt, anonymous), `live` and `publish` (`START=2026-10-20T09:55:00`,
  earlier messages at once). No auth, TLS or reconnect handling.

## 14. Web view (#210)

- `python -m neptune_memory.site web <site_dir> [--port 8765] [--open]` serves one page (`site/web.html`:
  vanilla JS and SVG, no build step) over stdlib `http.server`. `make site-web` opens it for `SITE_DIR`;
  `make site-live` serves it beside `live` and prints its URL.
- **Panels:** a site map, the live state table, a timeline of mode/health/zone changes, the findings
  (inferred), the pending-confirm queue (yes/no appends a `confirm` entry), and a source viewer.
- **Map:** layouts state no coordinates, so it is laid out from `part_of` (areas hold cells and zones),
  `located_in` and the live `in_zone`; a machine with neither sits on its area's floor beside what it
  `feeds`. Colour reads mode, health and the current task's state; stale is grey and dashed. A stated
  `same_as` group is one glyph. `feeds` edges out of a faulted or waiting node flow in the fault colour.
- **API** (JSON, read through `query.py` / `insights.py`): `/api/snapshot?at=T|seq=N` (the time slider
  sets `at` or `as_of_seq`), `/api/why?fact=ID`, `/api/source?file=&locator=` (lines of a text file, a
  CSV/XLSX row, a PDF page's blocks from the compiler's package, a JSON object, an image region),
  `POST /api/confirm`, and `/events` (server-sent events each time the log grows; the model tails it
  with `log.Tail`). `#fact=ID` or `#entity=REF` in the URL opens a fact or an entity.
- `FactLog` appends under a lock (`.neptune/log.lock`) and numbers on from any entries another writer
  added, so a confirm from the page while `live` runs never reuses a `seq`.
- An actions panel calls `neptune_memory.site.actions` (`pending`, `approve`, `reject`, #212) when that
  module exists, and is hidden otherwise.
