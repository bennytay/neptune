# 0020 — World and record context: sites, assets, geometry, media, documents and tables

- Status: Accepted
- Date: 2026-10-01
- Issue: MVL-69 (sub-issue of MVL-1)

## Context

ADR 0017 named the `world` family: the world and record context robots operate in. MVL-69 defines
its records. The sources are the ones most often flattened into text by other systems: a site
register exported to CSV, an asset list, a CAD model or a map, a folder of inspection photos, a
walk-through video, a PDF procedure, a Markdown runbook. Earlier ADRs constrain the design:

- ADR 0003: sites and assets have tier-3 ids that are declared, never inferred.
- ADR 0004 §5: a blank cell is `Unknown`, and it is `KnownAbsent` only where the source defines it
  as "none".
- ADR 0006 and ADR 0016: citations are exact: a `RowCell`, a `Span` of extracted text, a
  `PageRegion`, an `ImageRegion`, a `VideoFrame`, an `ObjectLocator`.
- ADR 0015 §6: positions keep their numbers and wrap their CRS, units and height reference.
- ADR 0017 §5: one record-level `EvidenceRef` per record.
- ADR 0018: timestamped samples are series, including video inside a log.
- ADR 0019 §4: detail that only some records have arrives as new record kinds, not new fields.

MVL-69's acceptance is that images, geometry and tables stay records with their own structure,
never text, and that every value cites its exact cell, span, page region or image region.

## Decision

1. **`Site` and `Asset`: a place and a physical thing that one declaration names.** Kinds `site`
   and `asset`, family `world`.
   - The declaration is a register's row or a manifest entry (`stated`), or a feature of a GeoJSON
     file. What else the row says stays in its `StructuredRecord` (§5); a site or asset record
     holds identity, hierarchy and position, not a copy of the row.
   - `identifiers` lists every id the declaration gives (ADR 0019 §2). `name` is the name it gives,
     and `aliases` the other names (`Names`: stated, cited, unique, sorted by text). An alias split
     out of one cell cites its span inside the cell (`[RowCell, Span]`).
   - A declaration names what it declares: a site or asset has an identifier or a stated name.
   - `parent: Knowledge[LogicalId]` is the declared id of the site or asset it is part of.
     `location: Knowledge[GeodeticPosition]` is the position the declaration gives.
   - An asset also has `category: Knowledge[str]`, its declared type verbatim (`centrifugal pump`),
     and `site: Knowledge[LogicalId]`, the declared id of its site.
   - A value built from several cells (a position from a latitude and a longitude column) cites
     their row; each cell stays citable in the row's `StructuredRecord`.
2. **`SpatialArtifact`: geometry by reference.** Kind `spatial_artifact`.
   - The record-level provenance cites the file, or the part of a container that holds it. The
     geometry stays in those bytes: nothing is converted, re-meshed or re-projected, and objects
     inside it are cited with `ObjectLocator` by the ids the file gives them.
   - `category` is structural: `mesh`, `cad`, `point_cloud`, `raster_map`, `vector_map` or `scene`.
   - `name: Knowledge[str]` is the name the file gives the model, scene or map.
   - `unit: Knowledge[Unit]` is the unit its coordinates are written in, where the file or its
     format states one for all of them (glTF: metres). It is a length or an angle, and
     `NotApplicable` where a CRS gives each axis its own (GeoJSON's degrees and metres).
   - `crs: Knowledge[CrsCode]` is the CRS it declares, `NotCovered` where the format has no place
     for one. `frame: Knowledge[FrameRef]` is the frame its coordinates are in; that `Frame` record
     says what the axes are.
   - Bounds, resolution, origins and resource references (a glTF's buffers, a map's origin) are
     detail for MVL-31 and MVL-32, as new record kinds.
3. **`Image` and `Video`: standalone media.** Kinds `image` and `video`.
   - An `Image` is a still. `width` and `height` are the stored raster's, in pixels, and `encoding`
     the format the adapter decoded (`jpeg`, `png`). These are structural: an image whose header
     cannot be decoded is a finding, not a record. `orientation: Knowledge[int]` is the declared
     EXIF Orientation (1 to 8), kept and never applied.
   - A `Video` is one video track of a standalone video file: `track` (its 0-based position among
     the container's tracks), `width`, `height`, `encoding` (the codec as the sample entry names it,
     such as `avc1`), `clock` (the `TimestampDomain` of its presentation times), and the declared
     `frame_count` and `duration`.
   - Both have `capture: Capture`: what the file declares about how it was made. That is the
     capture `time` on the clock the file states it on, the `position`, and the device's
     `device_manufacturer`, `device_model` and `device_identifiers` (a body serial). A photo is
     related to a machine's camera through declared ids, never by guessing.
   - Regions are cited, never described: `ImageRegion` in the stored raster, after a
     `VideoFrame(track, index, pts)` for video. What a detector or a model sees is derived.
   - A video inside a log is a stream (ADR 0018). A per-frame series for a standalone video (its
     frame-to-time mapping) is MVL-29's and MVL-22's, as a stream, when a consumer needs one.
4. **`DocumentRecord` and `DocumentBlock`: a document and its text, with exact spans.** Kinds
   `document_record` and `document_block`.
   - A `DocumentRecord` has `format` (the format the adapter decoded: `pdf`, `markdown`), `title`
     (as declared) and `pages`, empty for an unpaged document. A `DocumentPage` has `label`
     (declared, never the index), `width` and `height` in the page's own coordinate system (the one
     `PageRegion` uses), and `rotation` (declared `/Rotate`, never applied).
   - A `DocumentBlock` is one unit of text the transform extracts, in reading order: `document`,
     `order`, and `text`, exactly as its record-level citation `[Page(p), Span(s, e)]` (or
     `[Span(s, e)]` in an unpaged document) holds it. `region: Knowledge[EvidenceRef]` is where it is
     drawn, `[Page(p), PageRegion(…)]`. Storing the text means no consumer re-extracts a PDF to read
     a span.
   - How text is split into blocks is the adapter's documented, deterministic rule: CommonMark's
     blocks, or the pinned PDF extractor's text blocks. A block is an address, not a claim.
   - `role: Knowledge[BlockRole]` and `level: Knowledge[int]` are what the format declares a block
     to be: Markdown syntax, a tagged PDF's structure, a word processor's styles. The roles are
     heading, paragraph, list item, table, figure, caption, code, quote, formula, header, footer and
     footnote. An untagged PDF declares none, so its blocks' roles are `Unknown`; a heading guessed
     from font size is a derived annotation.
   - Inline runs (fonts, links, emphasis) are not modelled in v0. They can be added as a new kind.
     A table inside a document is also a `StructuredTable` whose cells cite page regions or spans.
5. **`StructuredTable` and `StructuredRecord`: a table and its rows, cell by cell.** Kinds
   `structured_table` and `structured_record`.
   - A `StructuredTable` is a CSV file, a spreadsheet's sheet or a table in a document. Its `name`
     is declared (a sheet name, a caption). `header: Knowledge[tuple[str, ...]]` is the header row's
     cells verbatim (`""` for a blank header cell), citing that row. It is `NotApplicable` when the
     table has no header row and `Unknown` when the source does not say whether it has one.
   - A `StructuredRecord` is one row: `table`, `row` (0-based, counted as `Row` counts, header rows
     included) and `cells` by column position. A short row keeps its length.
   - Cells keep their source's types: `str | int | bool | Real`. A CSV's cells are text, and `"3.5"`
     stays text; typed readings are derived. A spreadsheet's or typed file's numbers and booleans
     stay numbers and booleans. Non-finite numbers are `Real` (ADR 0017 §8).
   - **Blank is not "none".** A blank cell is `Unknown`. A token the source defines as "none" (a
     register's legend) is `KnownAbsent` citing that definition. The text `none` with no definition
     is `Known("none")`.
   - **Every cell cites its exact cell.** A row cited as `Row(r)` hoists its cells' provenance, as
     a series does (ADR 0018 §5): cell `c` is at `RowCell(r, c, name)`, the row's evidence with its
     last step replaced, where `name` comes from the table's header. States may then inherit, so a
     cell costs no citation in JSON. `StructuredRecord.cell_evidence(table, c)` rebuilds it. A row
     cited another way (a JSON array element, a table on a PDF page) gives every cell its own
     provenance. A `KnownAbsent` cell's provenance cites the definition; its place is its cell.
   - A table whose rows are timestamped samples, such as a telemetry export, is a series
     (ADR 0018), not structured records.

## Alternatives considered

- **Site and asset attributes as fields** (address, operator, time zone, criticality). Every
  register has its own columns, and fixed fields would force a vocabulary or a property bag. The
  row keeps them all, typed and cited, and the site or asset record keeps what identity needs.
- **Aliases as logical ids in an `alias` namespace.** Names are not unique, and treating them as
  ids would give identity resolution false certainty. They stay names.
- **Per-cell provenance in JSON.** Faithful, but a 10,000-row register would carry about 250
  bytes of citation per cell. The cell's place is fully determined by its row, its column and the
  header, so it is hoisted, as for series.
- **Rows as Parquet.** Columnar and compact, but registers are entity data of a few thousand rows
  whose cells are cited individually and joined to sites and assets. JSON Lines keeps them
  greppable. Large timestamped tables are already series.
- **Inferring CSV column types at parse time.** `"007"` would become `7` and `"1e3"` a thousand.
  Types a source does not declare are readings, and readings are derived.
- **Document chunks** (fixed-size windows of text). Chunks cut across structure and cite nothing
  exact. Blocks follow the format's own units and cite exact spans.
- **Document text only through spans**, without storing it. Every consumer would have to re-run
  the pinned extractor to read a citation, which is the re-parsing this layer exists to remove.
- **Block roles from layout heuristics in the canonical record.** A heading guessed from font size
  is an interpretation; it lives in `derived/`, beside the block it annotates.
- **One media record for stills and video.** A still has pixels; a video track has a clock, a frame
  count and a duration. Sharing `Capture` keeps what they have in common in one type.
- **Standalone video as a run with streams only.** Frame timing fits a series, but dimensions,
  codec and capture metadata would become stream metadata text. The `Video` record keeps them
  typed, and a frame series can be added beside it.
- **A geometry summary in the record** (vertex counts, bounds, a thumbnail). Useful, but format
  specific and easy to get wrong. It is detail for later kinds, or derived.

## Consequences

- A mixed folder of registers, photos, maps and procedures becomes records that keep their shapes:
  pixels are cited by region, geometry by object, text by span, and tables by cell.
- `tests/unit/model/test_world.py` resolves every cell of a CSV register, and every block of a PDF
  page and a Markdown file, back to the exact text it cites.
- Sites and assets share the declared-identifier list with machines; MVL-35 links all three the
  same way.
- A `KnownAbsent` records the definition of "none", not where the token appeared. In a table the
  place is still the cell; elsewhere it is the record's own citation.
- Consumers read a position built from two cells at row precision.
- Revisit if registers grow large enough that JSON Lines dominates read time, if consumers need
  inline document runs, or if a standalone video needs typed per-frame data in the canonical layer.
