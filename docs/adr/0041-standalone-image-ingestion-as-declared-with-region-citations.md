# 0041 — Standalone image ingestion: containers read as declared, no pixel decoded, regions cited

- Status: Accepted
- Date: 2026-10-02
- Issue: MVL-29
- Refines: ADR 0020 §3 (`Image`, `Capture`, `ImageRegion`) and applies ADR 0023 §2 (civil date-times)

## Context

Robots produce stills of every kind: a pipe crawler's inspection JPEG, an AMR's dock PNG, an ROV's
survey TIFF, a field rover's DNG, a humanoid's WebP, a manipulator's 16-bit depth PGM, a floor map
BMP. Neptune keeps them as visual evidence; it does not caption them. MVL-29 asks for
JPEG/PNG/WebP/TIFF support, EXIF time, GPS and orientation, dimensions and colour profile, a region
scheme, derivatives (thumbnails, pyramids, perceptual hash, an embedding hook), and that images stay
addressable at region level so they can later be related to assets, sites and runs.

The model already holds an `Image` with `Capture` and `ImageRegion` (ADR 0020 §3). What it lacks is a
place for everything else a container declares: EXIF and TIFF tags, XMP, ICC profiles, header fields.
`StructuredTable` and `StructuredRecord` (ADR 0020 §5) hold such declarations with exact citations,
so no new kind is needed. Images are hostile input (non-negotiable 9): lying dimensions, tag loops,
offsets past the end, zlib bombs and XML bombs are routine.

## Decision

1. **One adapter, `image`, standard library only, no pixel decoded.** It reads PNG (and APNG's first
   frame), JPEG, TIFF, BigTIFF and DNG, WebP, BMP and Netpbm (PBM, PGM, PPM, PAM) with `struct`,
   `zlib` and `expat`. Pixels are counted, scanned for markers or bounds-checked, never inflated or
   decoded; a decoder is a later derivative (§8). The adapter plans one chunk per source: a
   container's structures are small beside its pixels and are read in one sandboxed call.
2. **Records, all existing kinds.** `Image` per stored raster, `TimestampDomain` per capture clock,
   and `StructuredTable`/`StructuredRecord` for the rest, each table citing the exact bytes it reads.
   - One table per TIFF/EXIF IFD, named for the pointer that declares it (`IFD0`, `IFD1`, `Exif`,
     `GPS`, `Interop`, `SubIFD`): `tag`, `type`, `count`, then the values as stored (rationals as
     numerator and denominator, ASCII one cell per string). Values larger than `max_value_bytes` and
     MakerNotes are cited, not copied (`image.value_not_copied`).
   - One table per XMP packet (`namespace`, `path`, `value`), one per ICC profile (`ICC header`,
     `ICC tags`), and each fixed header structure (`IHDR`, `SOF0`, `VP8X`, `BITMAPV5HEADER`,
     `PNM header`, ...) as a one-row table whose header is the specification's field names.
   - A profile or text inflated from a PNG chunk, or joined from JPEG APP2 segments, is cited through
     the `image:payload` locator step (`decode=inflate` or `decode=join`) after its carrier's bytes.
3. **One `Image` per stored raster, and the region scheme.** An `Image` cites the whole file, except
   in a TIFF or DNG, where each IFD that declares `ImageWidth` and `ImageLength` is an image citing
   that IFD (a DNG's preview and its raw raster are two images). A region of an image is the image's
   citation followed by `ImageRegion(x0, y0, x1, y1)`: pixels of the stored raster, origin top-left,
   x right, y down, EXIF orientation not applied (a bottom-up BMP still counts rows from the top).
   Any later layer (a detector, a site map, a run) relates to an image by its record id or by a region
   citation, without parsing the file again.
4. **Declared is `stated`, measured is `observed`** (non-negotiable 2). Everything a writer or camera
   declares is `stated`: every IFD, XMP, ICC, text, JFIF and Adobe table and row, and the capture
   (time, position, make, model, serials, orientation) with its `TimestampDomain`. Only the measured
   raster structure is `observed`: IHDR, SOFn, VP8*, the BMP file header, the Netpbm header, and the
   `Image`'s size. A BMP DIB header and a WebP ANIM chunk also declare density, colour endpoints,
   intent and background colour, so they are `stated`.
5. **Capture is read as declared.** `Make` and `Model` verbatim; `BodySerialNumber` and DNG
   `CameraSerialNumber` as device identifiers (namespaces `exif.body_serial`, `dng.camera_serial`);
   `Orientation` 1 to 8 kept and never applied.
   - Time follows ADR 0023 §2 and nothing more: `DateTimeOriginal` with no zone is seconds of its own
     civil clock with timescale `Unknown`; with `OffsetTimeOriginal` the file states the offset, so it
     is an exact instant (`posix`); `SubSecTimeOriginal` sets the resolution. No zone is ever assumed
     and `DateTime` (file change time) and GPS time are never read as capture time.
   - GPS degrees, minutes and seconds are read exactly, with hemisphere and altitude reference, in
     degrees and metres above mean sea level as EXIF states; `crs` stays `Unknown` (`GPSMapDatum` is
     text, not a registry code) and the datum stays in its row. A position outside 90/180 degrees or
     with minutes or seconds of 60 or more is not a valid position: `Unknown` plus
     `image.value_unreadable`, the raw rows kept in the GPS table.
   - An absent tag is `Unknown`; a format with no place for metadata (BMP, Netpbm) is `NotCovered`;
     a value that does not parse (orientation 9, month 13) is `Unknown` plus `image.value_unreadable`.
6. **Camera geometry is declared, not interpreted.** Focal length, focal-plane resolution, distortion
   and an XMP camera model stay in their cited rows. Which of them form a calibration is MVL-26's
   reading; this adapter infers no intrinsics, field of view or scale.
7. **Hostile input is bounded and reported.** Offsets and sizes are checked before every read, an IFD
   is read once (`image.ifd_loop`, `image.bad_offset`), a declared raster over `max_pixels` is
   recorded with `image.pixel_limit` and `image.raster_truncated` when the file is too short to hold
   it, zlib payloads inflate to at most `max_metadata_bytes`, XMP is parsed with expat refusing any
   DTD and nesting past 64, and `max_structures` and `max_entries` bound what one source walks and
   emits (`image.limit_exceeded`, once). A limit never loses the image: a JPEG that stops in its
   metadata still looks for its frame header, within another `max_structures` segments, and a PNG
   stopped by a limit does not judge its raster. Work is charged before it is done: an IFD is read
   one entry at a time, strip and tile arrays spend `max_entries` x 256 items, a PNG chunk or metadata
   block over `max_metadata_bytes` is cited and not read, and a text cell, XMP namespace, path or
   value over `max_value_bytes` is not copied: its cell is `NotCovered` (citing the exact cell, like
   an IFD value left out of its row), never a prefix in a `Known` cell, and one
   `image.value_not_copied` per table cites the first 16 such cells with their lengths and counts
   the rest. An XMP path stops growing once it passes the cap, so deep trees of long names cost no
   more than the names. A text chunk inflates only that far; other streams
   inflate to `max_metadata_bytes` each and four times that in all; all rows of a source hold at most
   `max_metadata_bytes` of text. An XMP packet or ICC profile at given bytes is parsed once however many
   tags point at it. Netpbm headers are scanned in one pass, in at most the probe head (64 KiB, one limit for probe,
   detect and read), with numbers of at most ten significant digits.
   A BMP's linked profile is never opened. One damaged
   structure is a finding and the rest is read (non-negotiable 7); a file that is no readable image
   is `image.unreadable` and has no record. The default limits are config, so they are part of the
   transform.
8. **Not in this adapter, filed separately.** Thumbnails and pyramids, perceptual hashes and the
   visual-embedding hook are derivatives over decoded pixels and need a decoder dependency (MVL-80;
   any such output is derived and lives outside `model/`). Standalone video (container and codec
   metadata, frame-to-time mapping, lazy frame handles) is MVL-81. ADR 0020 §3 still names MVL-29
   for a standalone video's frame series; that pointer is MVL-81's, and a dedicated PR amends it.
9. **Leaf test.** `tests/unit/test_package.py` lets a format subpackage import its own modules
   (`neptune.adapters.image._png`), still nothing from another adapter, the runtime or the store.

## Alternatives considered

- **Pillow or `exifread`.** They decode pixels, accept hostile input with
  their own failure modes, normalise (EXIF orientation, time zone) and hide offsets, so no value could
  cite its bytes. Pillow is used only as the fixtures' oracle (`uv run --no-project --with pillow`).
- **A new record kind per metadata family (`ExifBlock`, `IccProfile`, `XmpPacket`).** The model is
  frozen except by addition (ADR 0023), and a table already carries tags, types, counts and exact
  citations. A consumer wanting a typed profile reads the table; a kind can be added when one needs it.
- **Decoding thumbnails in this PR.** It needs a decoder, and the acceptance (addressable at region
  level) does not. Splitting keeps this adapter dependency-free and sandbox-light.
- **Converting capture time to UTC.** A camera clock with no stated zone has none; guessing it is a
  silent assumption (non-negotiable 4).
- **Chunking large TIFFs.** A gigapixel TIFF's IFDs and tags are small; its strips are never read.

## Consequences

- Any still image becomes cited evidence of its size, encoding, orientation, capture clock, position,
  device and colour profile, plus every tag its container holds, with no decoder in the trust base.
- Region citations are stable: `[*image locator, ImageRegion]` is valid whether or not pixels are ever
  decoded.
- Tables are verbose (an EXIF block is tens of rows) but exact; a consumer wanting a typed view builds
  it from tables. If many consumers do, that is the trigger for typed kinds.
- A parser upgrade changes the transform's version and so every id (non-negotiable 6).
- Revisit when MVL-80 adds a decoder (the derivative layer, not this adapter, takes it), or if
  schema v2 (MVL-78) adds typed metadata kinds.
