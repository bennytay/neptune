# 0016 — Query engine: a rule-based planner, budgets that cut only prefixes, and a sealed SQL passthrough

- Status: Accepted
- Date: 2026-10-05
- Issue: MVL-98

## Context

`query(spec)` is the last catalog-API call without an implementation. Context (MVL-144) needs it for
traversal under `as_of`, `during`, frame and zone windows. Memory and Learn need it to pull the
records of a thread in a window, and the series rows of its streams.

The pieces exist. The catalog holds record rows, the thread index (ADR 0010) and the time and
spatial indexes (ADR 0015) in PostgreSQL. The lake reads series Parquet in place with DuckDB or
DataFusion (ADR 0013). catalog-api 1.6.0 fixed the filter contract: kinds, a `TimeWindow` on one
clock, a thread, packages, `as_of`, keyset paging. It has no frame window, lineage preference,
projection, series join, budget or explain.

Four things break if this is wrong:

- **Unbounded reads.** A query over 10⁵ packages can return 10⁷ rows. If it is cut silently, a
  caller treats a prefix as the whole answer.
- **Nondeterminism.** If a time limit decides which rows come back, the same call gives different
  bytes, which breaks the replayable-reads rule (ADR 0004 §1).
- **Hidden lineage choices.** A "current" filter that resolves lineage only over the rows a window
  kept would let the window pick the transform.
- **SQL as an escape hatch.** Hostile SQL could read files, reach the network, load code, or read
  another tenant's rows.

## Decision

1. **catalog-api 1.7.0 adds optional `QuerySpec` fields, and `QueryMeta` fields.** Each one is
   optional, so every 1.6.0 document is still valid. A 1.6.0 spec gives exactly the 1.6.0 table.
   - `frame: FrameWindow(reference, unit, low, high)`. `reference` is a `FrameReference`
     (`frame_graph_id`, `frame_id`) or a `CrsReference` (`authority`, `code`). `low` and `high`
     are 2 or 3 coordinates in `unit`, inclusive. A frame window keeps a record that has a
     `spatial_extent` row in that reference and that exact unit whose extent meets the box. This
     is ADR 0015 §5's `placed` rule. A record that states the reference but has no comparable
     extent never matches, just as a record without world time never matches a `TimeWindow`.
     `lake.space_index` now uses these two reference types.
   - `lineage: LatestTransform | Pinned | AsRegisteredBy` (§3).
   - `columns`: a projection over `QueryRow`'s columns. `kind`, `record_id` and `package_id`
     are always kept. Columns come out in `QUERY_RESULT_SCHEMA` order.
   - `series: SeriesJoin(columns)` (§4).
   - `budget: QueryBudget(max_rows, max_bytes, max_millis)` (§6).
   - `explain: true`: the result's metadata carries the plan (§2).
   - `QueryMeta.budget: BudgetReport`, on every answered query: the limits that applied, the rows
     and bytes returned, which limits cut the answer, and whether it is reproducible.
   - `QueryMeta.plan: tuple[PlanStep, ...]`, when `explain` is set.
   - Finding codes: `budget_exceeded` and `ambiguous_lineage`.
   - The codec gains `float` (JSON number, finite) and a `max_items` constraint for box corners.

   A rejected spec returns an empty `QUERY_RESULT_SCHEMA` table, `invalid_request` findings and
   no budget report, as in ADR 0004 §2. Rejected specs include: a spec that fails its JSON
   Schema; an undeclared kind; `first > last`; a box that is not finite with `low <= high`; a
   unit that is not a canonical symbol; a budget above the Ledger's ceiling; `series` without a
   window, with `kinds` other than exactly `stream`, or with `after` or `limit`.
2. **The planner is rule-based. PostgreSQL filters the catalog, DuckDB or DataFusion scans
   series, and Arrow joins them.** `neptune_ledger.query` plans a spec in up to four stages.
   Each stage is a `PlanStep(engine, operation, detail)`:
   - **Candidates (PostgreSQL).** One statement over the most selective index the spec names, in
     a fixed order. The rule is deliberate: a fixed rule gives a deterministic plan to explain.
     PostgreSQL still chooses join methods inside each statement.
     1. `thread_id`: `thread_member` by `(thread_id, registration_key)`, which already carries
        each member's world time.
     2. Otherwise `window`: the `time_interval` R-tree (ADR 0015 §6), on the window's clock
        only: record intervals, or series-file intervals for a series join (§4).
     3. Otherwise `frame`: the `spatial_extent` R-tree for the reference and unit.
     4. Otherwise `packages`: `record_by_package`.
     5. Otherwise `kinds`: the record primary key, in key order.

     The driving set is a `MATERIALIZED` CTE. Every other filter is applied exactly against the
     `record` row: kinds, `as_of`, packages, the window rule of `QuerySpec`, an `EXISTS` on the
     frame box, and the cursor. Rows come back in `(kind, record_id, package_id)` order under
     `LIMIT`. The statement runs in a read-only transaction. It reads in batches of at most
     10 000 rows, by keyset on that order, so a lineage filter or a budget can stop between
     batches.
   - **Lineage (PostgreSQL then Arrow-side Python).** Optional; see §3.
   - **Series scan (DuckDB by default, or DataFusion).** Optional; see §4.
   - **Join and budget (Arrow).** The series rows meet their stream rows, the projection is
     applied, the budget cuts a prefix (§6), and the table is combined into one chunk. One chunk
     makes the IPC bytes independent of how an engine chunked its output.

   `PlanStep.detail` is text the planner wrote: the SQL it ran, with parameter names and no
   values, the driving rule, and the series statement. It is never an engine's `EXPLAIN` output,
   because costs and timings vary between runs. So a spec with `explain` stays byte-identical.
   `QueryEngine.explain(spec, analyze=...)` returns PostgreSQL's and DuckDB's own plans. It is
   for operators, not part of the contract.
3. **A lineage preference resolves over whole lineage sets, never over what the filters kept.**
   - Without a thread, a row's lineage set is `(kind, source_content_id)` over every record
     registered by `as_of`: ADR 0003 §4.1 with the catalog as the thread. With `thread_id`, it is
     the thread's own lineage set, so the answer equals `thread(key, preference)`'s current view.
   - For each set that the candidate rows touch, all its members are read: `record_by_evidence`,
     or the thread's members. The set is resolved with the same resolver as `thread`
     (`threads.order.resolve`, using `transform_graph` chains for `LatestTransform`). A window,
     a frame or a package filter therefore never changes which transform wins.
   - A row is kept when its set is `Known` and the row has that transform. A row with no source
     or no transform has no lineage set and passes unchanged.
   - An `Ambiguous` set keeps no rows, and gives one `ambiguous_lineage` finding (subject: the
     source; detail: the kind and the candidate transforms) when it would have contributed rows
     to this page. Dropping its rows is reported, never silent. A `NotCovered` set keeps no rows
     and gives no finding, because `Pinned` and `AsRegisteredBy` mean exactly that (ADR 0003 §4.4).
   - Rows stay `(record, package)` pairs. A record that several packages registered is one row
     per package, all of the winning transform.
4. **A series join is a window read of the selected streams, joined back to their record rows.**
   - `series` requires a `window` and `kinds` exactly `stream`. The record stage selects streams
     with every filter of the spec, with one difference: the window selects a stream by its
     series file's interval on the window's clock (`time_interval` subject `series`, ADR 0015
     §2), the ticks the file actually holds there, not by the stream record's stated world time.
     At most 10 000 streams may be selected. More is refused as `invalid_request`, never cut.
   - `SeriesCatalog` resolves their files (ADR 0013 §4). `plan_series` reads them on the
     window's clock only, with the window pushed down to each scan and the row budget pushed down
     as `LIMIT`. Files the lake cannot serve keep their ADR 0013 findings.
   - Each series row gets its stream's record columns: string columns dictionary-encoded, since
     they repeat on every row of a stream. Then come `clock`, `ticks`, `ticks_state`, `seq` and
     the series' value, state and locator columns (by default those all files share).
   - Rows are in ADR 0013 order on one clock: `(ticks, package_id, stream_id, seq)`. That is
     total, so the join is deterministic.
   - A stream registered in several packages is read once, from its first registration
     (ADR 0013 §4), and joins that package's row.
   - Rows on other clocks are never placed on the window's clock. A thread whose streams sit on
     several clocks (a run split across two files) is read one clock per window. Carrying series
     rows through `ClockMapping`s is deferred (§9).
5. **Results.** `query` returns a `pyarrow.Table` with `QueryMeta` in the schema metadata, as in
   1.6.0. `QueryEngine.stream(spec)` returns the same rows as a `RecordBatchReader` in batches of
   65 536 rows. The answer is computed under the budget first, so streaming bounds a consumer's
   batch size, not the Ledger's memory; the budget bounds that. Record rows keep 1.6.0's order and
   paging. A projection without `world_*` still pages, because the cursor is the key columns.
6. **Budgets cut a prefix of the deterministic answer, and say so.**
   - **Limits.** `max_rows` and `max_bytes` count the returned table: its rows, and its Arrow
     `Table.nbytes` after the projection, as one chunk. `max_millis` is wall time from the start
     of the call. An absent limit takes the Ledger's default: 1 000 000 rows, 512 MiB, no time
     limit for `query`, and 30 s for SQL passthrough. A limit above the ceiling (10⁷ rows,
     4 GiB, 600 000 ms) is refused. `limit` is a page size, not a budget: it cuts silently, as
     in 1.1.0.
   - **What exceeding does.** The answer is the longest prefix, in the result order, that fits.
     Each limit that cut it gives one `budget_exceeded` finding, with subject `rows`, `bytes` or
     `time`, and is listed in `BudgetReport.exceeded`. The rows and bytes found are fetched with
     `LIMIT max_rows + 1`, so a cut is detected, never assumed. The byte cut is a binary search
     over prefix lengths, since a prefix's bytes grow with its length. Row and byte cuts depend
     only on the catalog and the spec, so the same call gives byte-identical output.
   - **Time.** The deadline is checked between record batches and before each stage. Each
     PostgreSQL statement runs under `statement_timeout` set to the time left. A DuckDB series
     scan is interrupted when the time runs out (`connection.interrupt()`). The rows returned
     are still a prefix: whole batches of the key-ordered record scan, or, for a series join,
     either the complete series answer or none of it. Which prefix depends on the wall clock, so
     a time-cut answer has `reproducible: false`. Every other answer has `reproducible: true`.
     The prefix property makes a time-cut answer recoverable. The same spec with
     `max_rows = BudgetReport.rows` and no time limit returns the same rows, and for record rows
     the last row is the cursor to continue from. DataFusion offers no interrupt from Python at
     the pinned 54.0.0, so a DataFusion scan is checked only before it starts. DuckDB is the
     default engine.
7. **SQL passthrough runs inside a sealed, per-call DuckDB over views of one scoped answer.**
   `QueryEngine.sql(statement, scope)` (and `PostgresCatalog.sql`) is for power users and
   `access/` (MVL-99). It is not a catalog-API call: `query` still never accepts SQL. Its rules:
   - **Scope.** `scope` is a `QuerySpec`. It is answered by the typed path under its own budget.
     Its rows become the view `records`, and with `series` the joined rows become `series`.
     Nothing else is loaded, so there is no other tenant's, package's or point's data in the
     process to reach. "Escaping the views" has nothing to escape to.
   - **Statement.** One statement, at most 64 KiB, with no NUL. DuckDB's own parser must classify
     it as a `SELECT`. Two statements, DDL, DML, `SET`, `PRAGMA` writes, `ATTACH`, `COPY`,
     `INSTALL`, `LOAD`, `CALL`, `EXPORT` and `EXPLAIN` are refused as `invalid_request` before
     anything runs.
   - **Engine.** A new in-memory DuckDB per call, configured before any user text runs:
     - `enable_external_access=false`: no file, glob, `ATTACH`, `COPY` or HTTP;
     - extension autoinstall and autoload off, and unsigned and community extensions refused;
     - `python_enable_replacements=false`, so a table name never resolves to a Python object in
       the caller's frames;
     - `threads=1`, a memory limit (1 GiB by default) and `max_temp_directory_size=0B`;
     - `lock_configuration=true`, so the statement cannot undo any of this.

     The views are registered Arrow tables, which need no file access. The connection is closed
     after the call, so nothing survives into the next one.
   - **Budgets.** The budget of §6 applies to the statement's result. Batches are fetched until
     the row or byte budget is exceeded. A watchdog interrupts the statement at the deadline,
     with a `budget_exceeded` finding for `time` and `reproducible: false`. Engine errors
     (parser, binder, permission, out of memory) are `invalid_request` findings with DuckDB's
     first message line, never exceptions.
   - **Determinism.** Output order is the statement's. With one thread and insertion order
     preserved, the pinned DuckDB gives the same bytes for the same input. Only a total
     `ORDER BY` makes that a guarantee rather than an observation, and the documentation says so.
8. **Where it lives.** `neptune_ledger.query`:
   - `spec.py`: validation;
   - `plan.py`: candidates, the rule and the SQL;
   - `records.py`: batches and lineage;
   - `join.py`: the series stage;
   - `budget.py`;
   - `sql.py`: the passthrough;
   - `engine.py`: `QueryEngine`.

   `PostgresCatalog.query` and `.sql` delegate to one lazily opened `QueryEngine` on the
   catalog's conninfo. The engine reads through its own read-only connection, like
   `IndexCatalog` and `SeriesCatalog`. No migration is needed: every driving index exists
   (0001, 0007, 0009).
9. **Deferred, with reasons.**
   - **Moving series rows from other clocks onto a window's clock through named mappings.**
     MVL-97 moved this here. Each row needs exact rational arithmetic through a path chosen per
     tick (ADR 0010 §9), and int64 overflows on nanosecond ticks. A per-row Python path misses
     ADR 0013's 200 ms budget by an order of magnitude. A `series` join therefore reads only the
     window's own clock and maps nothing. The thread merge (`thread(…, merge=…)`) and
     `IndexCatalog.window(clocks=…, mappings=…)` remain the cross-clock tools. This goes to a
     follow-up issue, together with an exact vectorised mapping (DuckDB `HUGEINT`).
   - **Paging of `IndexCatalog` answers.** Its single-clock record lookups are now paged through
     `query` (window and frame filters, keyset cursor). Cross-clock carried answers stay
     all-or-none (ADR 0015 §7) until the row merge above defines an order to page.

## Alternatives considered

- **A cost-based planner over PostgreSQL statistics.** Rejected. Its choice, and so the explain
  output, would change as statistics change. PostgreSQL already chooses within each statement,
  and the fixed rule always drives from the narrowest declared filter.
- **Engine `EXPLAIN` text in `QueryMeta`.** Rejected: it holds estimated costs and timings, so
  the same call would stop being byte-identical. It stays available outside the contract.
- **Truncate at the time limit and call it complete, or fail the call.** Rejected. The first is a
  silent truncation. The second throws away rows the caller can use. A flagged prefix is both
  honest and recoverable.
- **Make time-cut answers reproducible by cutting at fixed work units.** Rejected. Which unit was
  reached still depends on the clock. Only a row count reproduces a prefix, and the report gives
  it.
- **Resolve lineage over the filtered rows only.** Rejected. A window would then decide which
  transform is "latest" (Context).
- **SQL passthrough straight over PostgreSQL or over package Parquet.** Rejected. A PostgreSQL
  role sees the tenant schema and its functions, and only an allow-list of every function could
  stop file or network functions. DuckDB over package paths needs file access, which is exactly
  what a hostile statement wants. Loading the scope first and sealing the engine leaves nothing
  outside the views.
- **Parse SQL ourselves to allow-list constructs.** Rejected. DuckDB's parser decides what runs,
  so its own classification is the check. File, network and extension access are closed by
  configuration, not by matching text.
- **Return series rows as a second table.** Rejected. `query` returns one table (ADR 0004 §2).
  Dictionary-encoded record columns cost one integer per row.

## Consequences

- `query` is implemented. Every query contract test passes against `PostgresCatalog`, and the
  `PENDING` list is empty. Context can filter by thread, clock window, frame box, packages and
  lineage preference at any `as_of`, and page the answer.
- catalog-api moves to 1.7.0 (minor, stable). Consumers' pins move in this PR (the platform
  harness tests). The goldens add a full spec, a frame spec, a series spec and a partial-result
  meta.
- Every answer states the budget it ran under. A cut answer is always a flagged prefix. Only a
  time cut is not reproducible, and it says so.
- Measured on a synthetic catalog of 10⁴ packages (the L1 harness), a thread + window query
  through `PostgresCatalog.query` has p50 **0.8 ms** and p95 2.1 ms over 41 windows on a
  20-thread workstation, far inside the 300 ms acceptance: the thread index narrows the
  candidates to the workhorse machine's 2 500 members before the window is applied. The `@slow`
  test (`test_ledger_query_scale.py`, about 2 minutes to build the catalog) asserts p50 < 300 ms.
- Revisit:
  - when a consumer needs series rows across clocks (§9);
  - when DataFusion exposes cancellation;
  - when a consumer needs SQL over more than one scope;
  - when the 10 000-stream join cap binds.
