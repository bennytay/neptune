# 0052 — The geometry adapter: meshes and scenes as referenced objects, nothing copied, nothing guessed

- Status: Accepted
- Date: 2026-10-02
- Issue: MVL-32
- Refines: ADR 0020 §2 (`SpatialArtifact`), ADR 0041 §2 (declarations as cited tables) and ADR 0045 §
  "several files, one recording" (a reference is text, never opened)

## Context

Robots bring geometry of every kind: a manipulator's link mesh, a quadruped's foot, a mobile base's
chassis, an AGV's fork, a vehicle's lidar sweep, a marine hull, a humanoid's torso, an AMR's USD
scene. MVL-32 asks for baseline inspection of OBJ, STL, PLY, glTF and USD: dimensions, bounds and
units where declared, resource identity, dependency resolution, coordinate metadata and lazy raw
geometry handles, without turning ingestion into real-to-sim reconstruction.

`SpatialArtifact` (ADR 0020 §2) holds category, name, unit, CRS and frame. It has no field for
bounds, counts, an up axis or dependencies, and record kinds are frozen while schema changes wait on
the Ledger. ADR 0041 met the same gap for images with `StructuredTable` and `StructuredRecord`:
cited rows, no new kind. Geometry files are hostile input like any other (non-negotiable 9): lying
counts, truncated binaries, nesting bombs, and references to `/etc/passwd`.

## Decision

1. **One adapter, `geometry`, standard library only.** It reads OBJ, STL (ASCII and binary), PLY
   (ASCII, binary little and big endian), glTF and GLB (the JSON only), USD ASCII (the layer
   metadata block only) and identifies a binary USD crate. It plans one chunk per source. No
   vertex, face or texel becomes a record: the geometry stays in the source bytes.
2. **Records, all existing kinds.**
   - One `SpatialArtifact` per file citing `ByteRange(0, size)`. That citation is the **lazy
     geometry handle**: it resolves to the raw bytes in the content-addressed source, whose
     sha256 is the **resource identity**; nothing is copied or converted. `category` is `mesh`,
     `point_cloud` (OBJ or PLY with vertices and no faces) or `scene` (USD).
   - A `geometry properties` table (`StructuredTable`, header `property, v0, v1, v2`), one
     `StructuredRecord` per property, each citing the exact bytes it comes from (the facet array,
     the vertex section, the header line, a JSON pointer). Vocabulary: `encoding`,
     `format_version`, `vertex_count`, `face_count`, `facet_count`, `object_count`, glTF array
     counts, `declared_vertex_count`, `declared_face_count`, `declared_facet_count`,
     `bounds_min`, `bounds_max`, `up_axis`, `meters_per_unit`, `embedded_resource_count`.
   - A `geometry dependencies` table (header `kind, target, scope`), one row per named file,
     citing the exact bytes that name it. Kinds: `material_library` (OBJ `mtllib`, the rest of the
     line as one reference), `texture` (glTF images, PLY `comment TextureFile`), `buffer` (glTF),
     `sublayer` (USD `subLayers`).
   - Two adapter locator steps keep ids distinct where one citation serves several records:
     `geometry:table` (`name`) and `geometry:property` (`name`).
3. **Stated and observed** (non-negotiable 2). `stated` is what the file or its format's
   specification declares: header counts, USD `upAxis`, `metersPerUnit` and `defaultPrim`, glTF's
   metres and +Y (the specification, citing the file's `asset`), glTF `POSITION` accessor `min` and
   `max`, every reference. `observed` is what the adapter measured: counts of statements, facets and
   array lengths, bounds taken in one bounded pass over the vertices, format and encoding, a
   reference's scope. A header's count and the measured one are separate properties, so a header
   that lies is visible beside the truth.
4. **References are classified, never opened.** The adapter sees one source and no directory
   (law 4, ADR 0045), so it judges a reference by its text: `relative`, `parent` (climbs out of the
   file's directory), `absolute` (a root-anchored or drive path, `file:`), `uri` (any other
   scheme) or `embedded` (`data:`, counted not listed). Absolute paths and URIs can never be inside
   the source root: `geometry.reference_outside_root` (warning). A `..` that escapes the file's own
   directory is `geometry.reference_leaves_directory` (info): it stays in the root only if the file
   sits deep enough, which only a check across sources can know. Control characters and non-text
   are `geometry.reference_unsafe` and get no row. glTF URIs are percent-decoded before they are
   judged and stored verbatim. Whether a relative reference names a file that is present, and is
   not reached through a symlink (the walk already reports every symlink,
   `symlink_not_followed`), is a check across sources and belongs to `validate/` with the
   `SourceArtifact`s' locations; it is not built yet and nothing here pretends otherwise.
5. **Units and axes are never guessed.** glTF: metres by specification, up `Y`. USD:
   `metersPerUnit` maps to a unit only for 1, 0.01, 0.001, 0.0254, 0.3048, 1000 and 1e-6;
   another scale leaves the unit `Unknown` (`geometry.unit_unmapped`) with the number kept; no
   `metersPerUnit` is `Unknown`, not USD's fallback; `upAxis` absent is `Unknown`. OBJ, STL and PLY
   have no unit: `Unknown`; their up axis row is `NotCovered`. A CRS and a frame have no place in
   any of these formats: `NotCovered`. A binary USD crate is identified by signature and version
   and everything inside it is `NotCovered`. Bounds are in the file's own units, unconverted;
   a file with no finite vertex has `NotApplicable` bounds, a scan stopped by a limit
   `NotCovered`, never zero.
6. **Hostile input is bounded and reported.** `max_scan_bytes` (256 MiB), `max_vertices`
   (2,000,000), `max_header_bytes` (1 MiB), `max_json_bytes` (16 MiB), `max_json_depth` (64),
   `max_entries` and `max_value_bytes`, all config and so part of the transform. A line over 64 KiB
   is skipped, never buffered. Binary STL and PLY compare their declared counts with the file's size
   before reading a row, read only the rows that fit, and report `geometry.truncated` and
   `geometry.count_mismatch` with both numbers; a count of 10^18 costs nothing. A scan cut short by a
   limit makes counts and bounds `NotCovered` and is one `geometry.limit_exceeded`. glTF nesting is
   counted outside strings before any parse; NaN, Infinity and a BOM are refused. Non-finite vertices
   are left out of the bounds and counted once. Unknown OBJ statements and bad face indices are
   one `geometry.malformed` per reason. One damaged structure is a finding and the rest is read
   (non-negotiable 7); a file that is no readable geometry is `geometry.unreadable` and has no record.
   Names (an OBJ object, an STL solid, a glTF scene, a USD default prim) are copied only as text a
   record can hold: a control character, a lone surrogate, bytes that are not UTF-8 or more than
   `max_value_bytes` make the name `Unknown` with a finding. A byte order mark is skipped in OBJ and
   ASCII STL and refused in glTF JSON (RFC 8259). Numbers past int64 or float range are
   unreadable (a PLY count) or `Unknown` (a glTF accessor), never an exception.
7. **Probing is by bytes.** Signatures (GLB, PLY, USD) are `SIGNATURE` or `VERIFIED`; binary STL by
   its exact size; ASCII STL, OBJ and glTF JSON by grammar (glTF is `SIGNATURE`, so JSON readers
   never take it). A file that starts `solid` and whose size fits a binary STL is binary. The name
   is the last resort (`NAME_ONLY`, non-empty files only); `ingest` then reads a damaged binary
   STL, ASCII STL or OBJ if its first bytes are shaped like one.
8. **Out of scope, filed separately.** Decoding vertices or faces, triangulation, unit conversion,
   mesh repair, semantic reconstruction, glTF binary buffers and node transforms, USD prims,
   references and variants, the crate's table of contents, `.mtl` files (a source of their own), STEP,
   IFC, PCD and LAS. Anything derived from geometry lives in `derived/`.

## Alternatives considered

- **trimesh, pygltflib, usd-core.** They decode geometry, normalise units and axes, repair meshes,
  and hide offsets: no value could cite its bytes and the trust base would hold native decoders.
  They are the fixtures' oracles only (`uv run --no-project --with trimesh --with usd-core`).
- **A `GeometryExtent` record kind or new `SpatialArtifact` fields** for bounds, counts, up axis and
  dependencies. Typed, but schema changes wait on the Ledger and ADR 0041 showed tables carry cited
  declarations exactly. If many consumers parse the properties table, that is the trigger.
- **Resolving references inside the adapter.** An adapter has one `SourceReader` and no directory;
  giving it the filesystem breaks purity (law 1) and the sandbox. Resolution is a pass over the store.
- **Failing the file on one bad reference or vertex.** A corrupt artifact is findings, not a failed
  job (non-negotiable 7).
- **Assuming metres, Y-up or UTF-8 names.** Silent assumptions (non-negotiable 4).

## Consequences

- Any mesh, point cloud or scene becomes cited evidence of its category, unit, scale, up axis,
  counts, bounds and dependencies, with a handle to its raw bytes and no decoder in the trust base.
- A consumer reading properties parses a small table; a typed view can replace it without touching
  the cited bytes if schema v2 adds one.
- References that cannot be safe are findings from day one; presence and symlink checks need
  `validate/`.
- A parser upgrade changes the transform's version and so every id (non-negotiable 6).
- Revisit if schema v2 adds typed extent and dependency kinds, or when a decoder (a derivative
  layer) needs per-object bounds through `ObjectLocator`.
