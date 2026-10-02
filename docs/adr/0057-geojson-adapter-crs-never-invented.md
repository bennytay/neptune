# 0057 — The GeoJSON adapter: features, bounds and a CRS that is stated, defaulted by the RFC or Unknown

- Status: Accepted
- Date: 2026-10-02
- Issue: MVL-31
- Extends: ADR 0015 §6 (positions), ADR 0020 (world records), ADR 0024 (adapter ABI), ADR 0037 §7
  (the configuration adapter leaves GeoJSON to a dialect adapter), ADR 0042 (tabular)

## Context

Site files, route files, survey areas, field boundaries and geofences arrive as GeoJSON. Flattened
to text or settings they lose their geometry, their identity hints and, above all, their coordinate
reference system: GeoJSON 2008 let a file name one (`crs`), RFC 7946 removed it and defined a
default, and real files from both eras, and from tools that follow neither, carry projected
numbers with no CRS at all. The acceptance test is that spatial records can be joined later
without Neptune inventing a coordinate system. The model already has the records (`SpatialArtifact`,
`Site`, `Asset`, `StructuredTable`, `StructuredRecord`); no new kind is needed.

## Decision

One adapter, `geojson`, emitting existing kinds only (no schema change).

1. **Records.** One `SpatialArtifact` per file (category `vector_map`, `unit` `NotApplicable` where
   the CRS is known and `Unknown` otherwise, `frame` `NotCovered`, `name` the root `name` member if
   it is a string). Two tables: `features` (one `StructuredRecord` per feature: `id`,
   `geometry_type`, `positions`, `min_x`, `min_y`, `max_x`, `max_y`, `min_z`, `max_z`) and
   `properties` (one row per leaf of a feature's `properties`: `feature` index, `key` a JSON pointer
   inside `properties`, `value`). The `properties` table is named by the declared locator step
   `geojson:properties` after the features array's byte range; its `row` is the leaf's position
   within its feature (chunks cannot know a global count; the table is keyed by feature and row). A
   bare Feature or geometry root is one feature, row 0. A feature with an `asset_id` property is an
   `Asset`, else with a `site_id` a `Site`: no other feature is promoted, since a feature `id` or a
   `name` alone does not say what the feature is.
2. **Assertion kinds.** Bounds, position counts and the artifact's own structure are `observed`
   (read from the geometry's bytes, which stay where they are). The CRS, a geometry's declared type,
   feature ids and properties are `stated`. Every cell and every identifier cites exact bytes
   (`ByteRange`s, no `Row` step, so each cell has its own provenance, ADR 0020 §5).
3. **The CRS is never invented.**
   - A root `crs` member is `stated` as written: an OGC URN (`urn:ogc:def:crs:AUTH:ver:CODE`), an OGC
     HTTP URI or `AUTH:CODE`, authority and code verbatim, nothing case-folded or looked up (the
     URN's version stays in the cited bytes). `{"type": "EPSG", "properties": {"code": n}}` is
     `EPSG:n`. Always with `crs_legacy` (info): RFC 7946 removed the member.
   - Two different CRSs (a repeated member) are `Ambiguous`, each candidate cited
     (`crs_ambiguous`). A `null` (GeoJSON 2008: no CRS can be assumed), a `link` (never followed)
     or a name no rule reads is `Unknown` with `crs_unknown` saying which.
   - With no `crs` member the file is RFC 7946's and its CRS is that RFC's default, `OGC:CRS84`
     (WGS 84, longitude then latitude), `stated` by the specification and cited at the root `type`
     (ADR 0017 §6), with `crs_defaulted` (info), **only when nothing contradicts it**: a position
     outside longitude and latitude's range anywhere in the file, a `crs` member on a feature or a
     geometry, or a file that breaks off, or stops at a limit, before a `crs` member could be ruled
     out. Then the CRS is `Unknown` (`crs_unknown`). `plan` reads the whole file once to know.
   - Nothing is reprojected, wrapped, closed or repaired; the numbers stay as written.
4. **Which CRSs are geographic.** Without a registry (the model's `CrsCode` is verbatim), the
   adapter knows a short fixed list: the RFC 7946 default, `OGC:CRS84`, `CRS83`, `CRS27` and
   `EPSG:4326`, `4269`, `4258`. Positions are longitude then latitude whatever the authority's
   axis order, because GeoJSON says so (RFC 7946 §3.1.1, GeoJSON 2008 §2.1.1). Range, winding and a
   `Site` or `Asset` location are decided only for these.
5. **Validation, as findings.** Longitude within [-180, 180] and latitude within [-90, 90]
   (`coordinate_out_of_range`) only where the CRS is geographic; rings of four or more positions
   (`ring_too_short`), closed (`ring_not_closed`), and where geographic exterior counterclockwise
   and holes clockwise (`ring_winding`, info: RFC 7946 advises it, earlier files did not). A
   position that is not two or more numbers, an unknown geometry type, or coordinates missing or of
   the wrong depth is `geometry_invalid`; NaN, infinities and numbers too large for a double are
   `non_finite_coordinate` and are left out of the bounds. Bounds are plain minima and maxima per
   axis in the CRS's own numbers, `Unknown` for an invalid, empty or over-budget geometry and `z`
   `NotCovered` when no position has a third number. A bound never wraps at the antimeridian.
6. **Identity hints.** A feature's `id`, and `site_id` and `asset_id` properties, are kept as
   strings exactly as the source writes them (an integer as written) in the `Asset` or `Site`
   identifiers under the namespaces `geojson.feature_id`, `site_id` and `asset_id`, each citing its
   member. An `Asset` with a `site_id` names it in `site` as the declared `LogicalId("site_id", v)`.
   Nothing is resolved or merged: two features with one `asset_id` are two records (MVL-35 owns
   identity). `name` and `category` are the properties of those names. The location is the position
   of a Point, only in a geographic CRS and only if the geometry has no problem; a polygon's
   location is `Unknown`, never a centroid.
7. **Claiming.** A root object whose `type` is a GeoJSON type, and which has in the probe head the
   member that type needs (`features`, `geometry`, `coordinates` or `geometries`), is `VERIFIED`
   (ADR 0024 §7): it beats the configuration adapter (`STRUCTURE`/`NAME_ONLY`, which declines
   GeoJSON as `config.shape_not_configuration`), the text adapter (`GENERIC`) and tabular (which
   declines objects). It reads content, not the name, so `site.json`, `site.geojson` and an
   extensionless copy are all this adapter's. A `type` after a `features` array longer than the
   64 KiB head is not seen: the file is declined and falls to text, or a manifest picks `geojson`.
8. **Streaming and hostile input.** `plan` walks the root through a window of the source read as
   Latin-1 (a character is a byte, so every offset is exact) that grows to fit a value up to
   `max_feature_bytes` (8 MiB); a feature or root member over it, and what follows, is not read
   (`feature_too_large`). `json`'s C scanner decodes each value and says where it ends; a value
   nested beyond the interpreter's limit is skipped by an iterative bracket count and reported
   `too_deep`, never recursed into. Geometries are walked with a stack. Settings: `max_source_bytes`
   (1 GiB), `max_features` (10 M), `max_positions` (500,000 per geometry; over it the geometry is
   not walked and its bounds are `Unknown`: `position_budget`), `max_properties` (4,096 per
   feature), `max_depth` (32, for GeometryCollections and properties). Blocks of features are cut by
   constants of this version (2,048 features or 1 MiB), so chunk ids and the per-chunk, one per
   code findings are deterministic. A feature that is not UTF-8 has no record (`invalid_utf8`), a
   repeated member name is `Unknown` (`duplicate_member`), truncated or malformed JSON keeps every
   whole feature before it (`json_truncated`, `json_syntax`).

## Alternatives considered

- **Always default to WGS 84 when no `crs` member.** Wrong for the many files with projected
  numbers and no CRS; the join downstream would silently misplace them. RFC 7946 licenses the
  default only for a file that is RFC 7946's, and the file's own positions can falsify that.
- **A new `GeoFeature` record kind.** The existing tables hold features and properties, and the
  schema version is contended (ADR 0037 §1); a kind can be added when a consumer needs more than
  tables of cited cells.
- **Read the file with `json.load` and a geometry library.** No byte offsets to cite, a whole-file
  tree in memory, and a library's CRS handling and repair decide for us.
- **Bounds on the artifact.** `SpatialArtifact` has no bounds field (record kinds are frozen); the
  per-feature bounds are cells of a table citing their geometry.
- **One chunk for the file.** A site file with a million features would redo everything after a
  crash; blocks of features resume.
- **Look EPSG codes up to learn which are geographic.** A registry is a dependency and an
  interpretation; the short fixed list errs toward `Unknown`.
- **KML and lat/lon CSV in this adapter.** Deferred: CSV with latitude and longitude columns is a
  table and `tabular` reads it (the columns are named by the source and nothing says their CRS);
  KML needs an XML reader with entities and DTDs refused, a separate adapter.

## Consequences

- Spatial records join later on cited, declared CRS facts or on an explicit `Unknown`, `Ambiguous`
  or default marked as the specification's; a consumer decides what to do with each.
- A legacy file with a `crs` member that this list does not read as geographic (EPSG:3857, UTM)
  has no location on its sites and assets, and no range check: its features are in the tables.
- A `site.json` that was text, or configuration at `NAME_ONLY`, is now this adapter's: old lineage
  is untouched, new runs differ.
- Properties rows are keyed by feature and row, not by a global row; a consumer joins on
  `feature`.
- Changing a constant in 6 or 8, the geographic list or any rule changes chunk or record ids, so
  it creates new lineage (non-negotiable 6).
