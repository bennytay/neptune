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

- v3 lives in `packages/neptune-memory/src/neptune_memory/site/`, beside v2. **Don't touch v2** (`schema/`,
  `consolidate/`, `store/` …) or Demo v1. M1's gate (#188) switches over and deletes v2.
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
  2. Hand each record to `mappers.map_record(record, package) -> list[Fact]`. Records arrive in package order
     (stable), and citations come from each record's `provenance` evidence locator.
  3. `layout.yaml` is read directly by `mappers.map_layout(path)`. It is the declared site, so its facts are
     stated.
- **Feed messages:** `replay.messages(site_dir)` (#182) yields `FeedMessage` in arrival order. For each one,
  append `seen`, then the facts from `mappers.map_message(msg)`.
- **Entry points:**
  - `build(site_dir, *, until=None) -> Path` does static first, then the feed up to `until` (site time).
  - `replay(site_dir, *, speed=60.0, until=None)` does the same live: it sleeps `Δt/speed` between messages
    and appends as it goes.
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
  - v2 tools stay as they are.

## 9. The live-site corpus (`site/corpus/`, `site/replay.py`, #182)

- `python -m neptune_memory.site generate <out>` (Python: `neptune_memory.site.corpus.generate(out: Path)`) writes the corpus. It is deterministic: same code, same
  bytes. PDFs reuse the writer in `tests/fixtures/pdf/make_pdfs.py`, loaded by path, as Deploy's document
  fixtures do.
- `make site-demo` = generate into `site-demo/` → `build` → print the MCP command.
  `make site-replay SPEED=60` = the same, live.

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
- They are `xfail(strict=True)` until #188 flips them. Tests call the §8 API. The answers must cite the files
  listed; wording is never checked.

## 11. Lane ownership

| Issue | Owns | Needs |
|---|---|---|
| #182 corpus + replay | `site/corpus/`, `site/replay.py` (`FeedMessage`, `messages`, `pace`), `site/__main__.py` (`generate`), `Makefile` targets `site-demo`/`site-replay` | spec |
| #183 fact log | `site/log.py`, `site/build.py`, `__main__` `build`/`replay` | `facts.py`; stub mappers until #184 |
| #184 site data model | `site/vocab.py`, `site/mappers/` (`map_layout`, `map_record`, `map_message`) | `facts.py`, corpus |
| #185 views | `site/views.py` | #183, #184 |
| #186 identity + confirm | `site/identity.py`, `site/confirm.py`, `__main__` `confirm` | #184 (and #185 to show merges) |
| #187 query + MCP | `site/query.py`, `site/render.py`, `neptune_context/mcp/site.py`, `--site` flag, the skill text | #185 |
| #188 gate | flips the gold xfails, adds one replay test, `make site-demo` end to end, deletes v2 | all |

- Shared files (`site/__main__.py`, `Makefile`): add your subcommand or target only; don't reorder the others.
