# 0015 — Time and spatial indexes: intervals per clock, extents per declared frame or CRS

- Status: Accepted
- Date: 2026-10-05
- Issue: MVL-97

## Context

Layers above the Ledger ask two kinds of question across packages. "What exists on clock C
between t1 and t2": runs, streams, calibrations and series rows of every registered package.
"What lies in this box of frame F": poses, frames, parts and sites. Today `query` (MVL-98) could
answer the first only for records, by scanning `record.world_*`, and nothing answers the second.

Three earlier decisions bound the answer:

- Clocks are per source. Two clocks are never equated without a named `ClockMapping`, and ticks
  are never converted (ADR 0003 §3, ADR 0013 §5).
- A frame is a `FrameRef`: a frame id inside one declared frame graph. A position states its
  CRS verbatim (`OGC:CRS84` is not `EPSG:4326`). There is no world frame to fall back on.
- Everything the Ledger derives is rebuildable from packages, and a rebuild is byte-identical
  (ADR 0012).

If this is wrong one way, a window on a drone's boot clock returns GPS-time intervals whose ticks
happen to fall in it, or a box near the origin returns every robot's base frame. If it is wrong
the other way, every lookup scans every interval or extent of the tenant.

## Decision

1. **Two derived tables in the catalog, written by registration.** Migration 0010 adds
   `time_interval` and `spatial_extent` to the tenant schema. Registration computes every row
   from the verified package and writes it in the registration transaction, after the record
   and thread rows. The tables are append-only like every registration table (ADR 0002 §6).
   A replay writes them again, so `ledger rebuild` reproduces them and `ledger dump` holds them.
   A catalog with packages from before 0010 refuses the migration and is rebuilt, as for 0007.
   They live in PostgreSQL, not under ADR 0013 §2's object-store layout, because a lookup must
   read them at the same catalog point (`as_of`) as the records they name.
2. **`time_interval`: one row per interval on one clock.**
   - `subject = 'record'`: a record with world time, exactly as `record.world_*` holds it: start
     ticks, and the end on the same clock or NULL when it is open (ADR 0003 §3).
   - `subject = 'series'`: per clock of a stream's series file (`time/<i>`), the least and
     greatest known tick, and how many rows have a known tick there (`rows_known`) and how many
     do not (`rows_unknown`). A clock with no known tick in the file has no row; its rows still
     count, and a reader sees them through the lake (ADR 0013). The ticks are read from the
     file's own int64 column at registration, never from Parquet statistics, which the writer
     computed and nothing verified. A column that is not int64 refuses the package as
     `record_invalid`.
3. **A window is one clock.** `IndexCatalog.window(TimeWindow(C, t1, t2))` lists every row on
   C whose stated extent `[first, last or first]` meets `[t1, t2]`, both ends inclusive. Rows on
   another clock are never compared with it, whatever their ticks. A request may name other
   clocks only together with `ClockMapping` record ids. Each named clock must then reach C
   through the named usable mappings. Otherwise the request is refused as `invalid_request`,
   `unknown_clock` or `unknown_mapping`, and nothing is compared. A named clock's intervals are
   carried onto C exactly as a thread merge carries an entry (ADR 0010 §9). The path is the
   best-ranked usable path whose every hop's validity window holds the whole interval, and each
   hop's bound widens it. An interval no path holds is reported as `mapping_out_of_range` and
   not placed. Stored ticks are never rewritten. Each carried entry keeps its own clock and
   ticks and adds `mapped`: its interval on C and the mapping path. The answer is ordered by the
   interval on C, then clock id bytes, then a native key (extent, registration key, subject,
   record id, package id).
4. **Spatial references are declared, never assumed.** `spatial_extent` has one row per spatial
   reference a record states as Known, at a JSON pointer of the record:
   - `frame` and `frame_binding`: their `FrameRef`s;
   - `hardware_component` and `spatial_artifact`: their Known `frame`; a `spatial_artifact`'s
     Known `crs` too;
   - `frame_transform`: `/parent` and `/child`. With a Known `direction`, the target frame's row
     also holds the source frame's origin, the declared translation, in the translation's unit.
     That is a pose's `translation`, or a 4×4 matrix's translation column when its `layout` is
     Known;
   - `site`, `asset`, `image` and `video`: a declared geodetic point in its Known CRS. Longitude
     is x and latitude y, named fields as the record states them whatever the CRS's axis order;
     the unit is the stated angle unit. Height is not indexed: what it is measured from varies.

   A reference that is Unknown, Ambiguous or NotCovered gives no row. A transform whose
   direction is not Known holds no point in either frame. Coordinates are stored as declared:
   no unit conversion, reprojection or carrying between frames. Geometry stays in its file, so a
   mesh or vector map is a member of its frame or CRS with no extent.
5. **A box names its reference and unit.** `IndexCatalog.within(reference, unit, box)` takes a
   `FrameReference(frame_graph_id, frame_id)` or a `CrsReference(authority, code)`, a canonical
   unit symbol and a 2- or 3-axis box. There is no default reference. It returns:
   - `placed`: the members whose extent in that unit meets the box, ends inclusive. A 2-axis box
     holds any z.
   - `unplaced`: the members it cannot compare, each with a reason: `no_extent`, `unit` (another
     unit, or none Known) or `dimensions` (a 3-axis box and a 2-axis extent). `unplaced=False`
     skips this list.

   Members whose comparable extent misses the box are in neither list. A box does not wrap at
   ±180°.
6. **R-trees per clock and per reference, in core PostgreSQL.** Each table has one GiST index
   over `box` keys. One axis holds `index_key(text)`, the first 52 bits of the text's sha256,
   which a float8 holds exactly. The text is the clock id, or the reference kind, reference and
   unit. `time_interval.span` is `(index_key(clock), first)–(index_key(clock), last)`.
   `spatial_extent` indexes `(scope, xy)`, where `scope` is the reference's key as a point and
   `xy` the extent's box. A search therefore descends only into one clock's or one reference's
   entries. Ticks become float8 in the key only. Rounding never reverses an order, so the key
   keeps every row the exact test can keep, and the bigint ticks and the stored text decide. A
   B-tree on `(reference_kind, reference, unit)` serves `unplaced`.
7. **Bounded work.** A request names at most 64 clocks and 256 mappings. An answer holds at most
   `max_entries` entries per lookup: 10 000 by default, at most 100 000. A lookup asks for one
   row more. If it gets it, the request is refused as `invalid_request` with the clock or `box`
   it exceeded, so an answer is all of its rows or none, never a prefix that depends on the
   engine's scan order. The path search shares ADR 0010 §6's step budgets. A malformed request
   (a window that is not a `TimeWindow` of int64 ticks, ids that are not record ids, a box that
   is not finite, `low <= high` on two or three axes) is a refusal, never an exception. A store
   failure is `CatalogUnavailable`.
8. **Not a catalog-API call yet.** `IndexCatalog` is the Ledger-internal surface. `query`
   (MVL-98) and `access/` (MVL-99) decide what the catalog API exposes, so `contracts/` is
   unchanged.

## Alternatives considered

- **One GiST over ticks (`int8range`) and a B-tree on the clock.** Rejected. Boot clocks of
  different packages all start near zero, so the range search returns every package's intervals
  and filters them. The B-tree alone scans every interval of a clock that starts before the
  window's end. The hashed axis keeps the search inside one clock.
- **`btree_gist` or `cube` for a composite `(clock, range)` or 3-D key.** Rejected. Each is a
  server extension a deployment must install and the Ledger role must be allowed to create. ADR
  0013 rejected server extensions for the same reason. The hashed key needs none.
- **Read series intervals from Parquet statistics.** Rejected. Statistics are optional and
  writer-computed. The columns are int64 and read in batches in linear time.
- **Index series rows themselves.** Rejected. The lake reads rows in place with the window
  pushed down (ADR 0013). The index says which files a window needs, not which rows.
- **A default world frame, or treating a frame id as global.** Rejected. Two robots' `base`
  frames are two frames. A query that names none is refused.
- **Treat `EPSG:4326` and `OGC:CRS84` as one CRS, or convert units.** Rejected. That is
  interpretation (root ADR 0020). A layer above can ask both and decide.
- **Truncate an answer at `max_entries` and return a cursor.** Rejected for now. A merged
  cross-clock order has no stable cursor until `query` (MVL-98) defines paging.

## Consequences

- "What exists on clock C" and "what lies in this box of frame F" are one index search each,
  for records and series files alike, at any catalog point. Cross-clock answers exist only with
  named mappings and carry their path.
- Registration reads each series file's clock columns once more after verifying it: linear in
  its rows. The spatial rows come from the record lines registration already reads.
- Two more append-only tables, rebuilt and dumped with the rest. Migration 0010 refuses a
  catalog that already holds packages.
- ADR 0013 left two things to this issue. The file-level time index is `subject = 'series'`: it
  names the series files a window needs, on any clock, without opening a footer. Merging rows of
  two clocks onto one reference clock is not done here. The index carries intervals, and a row
  merge is a read plan, so it moves to the query engine (MVL-98), which plans lake reads.
- Bounding volumes are points today, because no record kind states a box. A kind that states one
  adds rows with `min_*` < `max_*` without a schema change.
- Revisit when `query` exposes these lookups and pages them (MVL-98), when a record kind states
  geometry bounds, or when a clock's intervals outgrow one R-tree per tenant.
