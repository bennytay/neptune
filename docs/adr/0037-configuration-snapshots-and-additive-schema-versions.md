# 0037 — Configuration snapshots, the config adapter, and schema versions that add without rewriting

- Status: Accepted
- Date: 2026-10-02
- Issue: MVL-23
- Amends: ADR 0017 §2 and §7 (what `schema_version` says; regenerating goldens on a bump), ADR
  0022 §1 and ADR 0023 §1 (which tables a package holds), ADR 0006 §3 (`JsonPointer` also
  addresses a TOML document as parsed)

## Context

MVL-23 asks for JSON, YAML and TOML machine configuration as typed snapshots, so that two robot
runs can be bound to the exact configuration they ran (MVL-38) and compared field by field later.
ADR 0019 §7 left configuration snapshots to this issue, and the M1 review named them a new kind.
Forces:

- **The model is frozen and grows by addition** (ADR 0023 §1): new kinds through an ADR and a
  version bump. As ADR 0017 §2 was written, every record carries the writer's `SCHEMA_VERSION`,
  so this first addition would rewrite every record and golden file in the repository, and every
  package an unchanged adapter writes, for no change in content. ADR 0023 also promised that
  older packages always load, but the package reader required a table for every kind it knew, so
  the first new kind would have made every existing package unreadable.
- **Configuration formats type values differently.** JSON and TOML fix a scalar's type in their
  grammar. YAML types a plain scalar by its text, and the rules changed between YAML 1.1 and 1.2:
  `on`, `yes`, `0755`, `1:30`, `1_000`, `2026-09-01` and `1e3` read differently. ROS 2 parameter
  files declare no version; PyYAML and ROS 2's own parser read 1.1-like rules, yaml-cpp reads 1.2.
- **Configs are hostile input** like any source: nesting bombs, alias bombs (billion laughs),
  huge scalars, invalid UTF-8, repeated keys, tags that make a YAML loader run code.
- Values must carry provenance down to the value, missingness must be explicit, and nothing may
  be inferred: a key named `wheel_radius` is a declared number, not a radius in metres.

## Decision

1. **Schema version 2, added without rewriting anything.** `SCHEMA_VERSION` is 2.
   - A record's `schema_version` is the lowest version whose readers read it: the version that
     added its kind (`since`, 1 unless a kind says otherwise). Kinds are frozen, so a kind's
     records are written at one version for ever, and adding a kind changes no byte of a record
     that does not use it. A later addition that changes what an existing kind may hold (an enum
     member, a locator step) writes the records that use it at its own version; the ADR adding
     it says so.
   - A package is written at the lowest version that holds its records (`package_version`): its
     manifest and receipt carry that version, it has a table for each kind of that version
     (`kinds_at`) and no other, and its receipt counts those kinds. A package with no
     configuration records is a version 1 package, byte for byte what a version 1 writer wrote.
   - Readers read every version from 1 to their own: a record must be at least its kind's `since`
     (`record_object(..., since)`), a package's tables are those of its manifest's version, that
     version is exactly the one its non-empty tables need (a version 2 manifest over version 1
     records is refused: the same records have one package), and its receipt is recomputed at
     the version it says. A version 1 reader refuses a version 2
     package with "schema version 2 is newer", never a key error.
   - ADR 0017 §7's note that a bump regenerates every golden file no longer holds: an addition
     regenerates nothing that does not use it. The schema's `schema_version` is a constant per
     kind, and `1..2` for the package's documents.
2. **`ConfigurationSnapshot`** (kind `configuration_snapshot`, family `machine`, since 2): one
   configuration document as its bytes declare it. A JSON or TOML file is one; a YAML stream is
   one per document.
   - `format` (`json`, `toml`, `yaml`) and `format_version`: the version the document declares
     (`%YAML 1.2`, citing the directive), `Unknown` when it could and does not, `NotCovered` in
     JSON and TOML, which have no place for one.
   - `encoding`, `byte_order_mark`, `line_endings`: how the bytes are written, as found.
   - `comments`: every comment, `#` included, `Known` and citing its own span, in source order.
     Recorded and never attached to a value: which value a comment is about is interpretation.
   - `values`: how many `ConfigurationValue` records it has, so a package that lost some to a
     failed chunk shows it. `digest`: the identity of its values (§5).
3. **`ConfigurationValue`** (kind `configuration_value`, family `machine`, since 2): one node of a
   document (a mapping, a sequence, a YAML alias or a scalar), naming its `snapshot`.
   - `path`: keys verbatim (a string's decoded content; a YAML key's scalar text) and sequence
     positions as integers, `()` for the root. `order`: its position among its parent's entries,
     so key order survives. Two values share a path only where a key repeats, so `occurrence`
     holds one rank per step of the path: which of its parent's entries with that key the step
     passes through (0 for the first and for every sequence position). `(path, occurrence)` is
     unique in a snapshot: values sort, compare and hash in one order, however they are listed
     (a package's tables are sorted by id).
   - `tag`: a YAML node's tag, the explicit one expanded or YAML's non-specific `?` (plain
     scalars, collections) or `!` (quoted and block scalars); `NotCovered` in JSON and TOML, and
     for an alias, which has none of its own.
   - `text`: a scalar as written (a string's content; any other scalar's token verbatim, so
     `0x1F`, `1.0` and `1_000` keep their spelling). `NotApplicable` for collections and aliases.
   - `value`: the reading the format's own schema gives the node: `ConfigCollection(type,
     length)`, `ConfigAlias(anchor, target path)` or `ConfigScalar(type, value)` (bool, exact int,
     binary64 float or `NonFinite`, string, YAML binary, and TOML's four date-time kinds as ISO
     8601). A null the format defines (JSON `null`, YAML `null`, `~`, empty) is `KnownAbsent`
     citing the document. Readings that differ by YAML version are `Ambiguous`, YAML 1.1's first.
     A value no record can hold (beyond binary64, over 14,000 bits, an unpaired surrogate, an
     application's tag, a text not of its tag's type) is `Unknown` with a finding.
4. **Citations.** A value's record-level locator is an RFC 6901 `JsonPointer` into the document
   as parsed, after a `config:document {index}` step in YAML. Where a key's text repeats in a
   mapping, each of its entries is addressed by position instead (in YAML, `1` and `"1"` are two
   keys of one text, so one path: addressed by position, but no `duplicate_key`, which needs the
   same text and type: a plain key's type is what the document's YAML versions read it as, a
   quoted or `!!str` key a string, any other tag its own): the mapping's pointer, then
   `config:entry {order}`, then a pointer into that entry's value, so no two values share a
   citation and none is a counter. A value's state cites the `Span` it is written at, in code
   points of the decoded text (the text adapter's convention); a TOML table cites its `[header]`,
   and a table with no one place (an array of tables, a dotted-key table) its pointer alone.
5. **Snapshot identity and comparison.** A snapshot's bytes are identified by their content id; a
   run is bound to them, and through them to its snapshot. Its values are identified by `digest`:
   the sha256 of every value's path, occurrence and `comparison_key`, in path then occurrence
   order (`neptune.identity.configuration`). `compare_configurations` joins two snapshots' values
   by path, compares a repeated key's entries by occurrence, and reports each added, removed or changed path with the records on each side. They
   agree: equal digests exactly when no path changed. What a value declares counts: its reading,
   or for one with no reading its text and tag; a collection's type, not its length (its entries
   count themselves); an alias's target. Spelling, quoting, comments, key order, anchors' names,
   encoding and format do not: run A's parameters in UTF-16, with CR LF, or as JSON, have run A's
   digest.
6. **Read as declared, schema-aware and schema-less at once.** `text` is every scalar as written,
   read by no schema; `value` is the reading of the format's own schema.
   - JSON: the standard library's `json` with hooks that keep members in order with repeats and
     every number's token. A number with neither fraction nor exponent is an int (RFC 8259's
     grammar). `NaN` and `Infinity`, which `json` accepts and RFC 8259 does not, are read as
     declared and reported.
   - TOML: the standard library's `tomllib`, which decides validity and every value. A repeated
     key makes a TOML document invalid, as every TOML reader refuses it: a syntax error, not a
     snapshot. A scanner over the accepted text finds spans and comments, which `tomllib` drops.
   - YAML: PyYAML's pure-Python parser, events only. Nothing is ever constructed, so no tag runs
     code; aliases are references, never expanded, so a billion-laughs document is 91 values;
     an anchor on a key, which is no node and has no path, makes an alias to it read as that
     key's scalar; merge keys (`<<`) stay keys. Plain scalars are typed by the version the document declares;
     without one, by option `yaml_version`: `declared` (default) reads both 1.1 (the type
     repository) and 1.2 (the core schema) and keeps both readings where they differ, with one
     `config.yaml_version_undeclared` finding per document; `1.1` or `1.2` assume that version.
     A 1.1 timestamp keeps its declared fields; the repository's "zone-less means UTC" is a
     default, not a declaration, so it stays a local date-time.
   - A schema the document points at (`$schema`, an editor's modeline) is a community convention,
     not a declaration; validating values against another source's schema is `validate/`'s.
7. **Probing and format detection.** The bytes decide, never the name. The first line that is not
   blank or a comment picks the grammars to try (`{`: JSON then YAML; a TOML header: JSON, TOML,
   YAML, since `["base_link"]` is both; `key =`: TOML; otherwise YAML), and the first that
   accepts the whole text reads it. If none does, the error reported is that of the reader that
   got furthest by lines. The probe claims `STRUCTURE` (0.7) only for a document whose grammar
   holds to its end (or to its cut, for a truncated file or a 64 KiB head), whose every
   document root is a mapping or sequence, and whose shape is settings, not data:
   - A sequence at the root (JSON's `[{"t": 0.0}, ...]`, YAML's `- 1`) is rows of data, never
     named settings: 0.0 with `config.shape_not_configuration`.
   - A JSON object is data when a root key is not a setting's name (`[$@]?[A-Za-z_]` then
     letters, digits and `_-.:/`: keys such as `2024-01-01` or `max speed` are content), when it
     is GeoJSON (a root `"type"` naming an RFC 7946 type), or when every root member is a list
     of objects (`{"rows": [...]}`). Those score `NAME_ONLY` (0.1) with the same reason: the
     text adapter reads them, the probe engine reports a `.json` read as text as
     `name_mismatch`, and a dialect adapter (GeoJSON, tables) claims them when it lands. A
     manifest naming this adapter still ingests them. One table among settings
     (`{"joints": [{...}], "base": "base_link"}`, PX4's `parameters`) is configuration.
   YAML's grammar also holds for many notes
   (`Robot: spot-12` lines, a Markdown list), so a YAML head must also show configuration: a
   `%YAML` directive or explicit `---`, a tag, a closed nested or flow collection, or a plain
   scalar both versions read as a boolean or a number. A flat file of text values is left to the
   text adapter, as are a scalar document, a blank or comment-only file, and a file whose grammar
   breaks mid-way (the probe engine reports a `.toml` read as text as `name_mismatch`).
8. **Hostile input costs findings** (`config.*`, all documented in the descriptor): `too_large`
   (`max_bytes`, 8 MiB), `too_deep` (`max_depth`, 200), `scalar_too_large`
   (`max_scalar_length`, 1 Mi code points), `invalid_encoding`, `syntax_error`,
   `duplicate_key`, `unsupported_key` (a collection or alias to one as a key), `undefined_alias`,
   `unrepresentable_value`, `unresolved_tag`, `invalid_value`, `nonstandard_json`,
   `byte_order_mark`, `mixed_line_endings`, `no_document`, `yaml_version_unsupported`. A
   document that does not parse yields no snapshot: a partial configuration is not the
   configuration. In a YAML stream the documents before it are kept.
9. **PyYAML is the YAML library** (ADR 0001 §4, imported only in `adapters/config/`). It is the
   library ROS tooling and most robotics code use, it is stable and widely audited, its event API
   gives exact positions and leaves typing and alias handling to us, and its pure-Python parser
   behaves the same on every install (libyaml's C parser is not used). `json` and `tomllib` are
   the interpreter's, so `python` (its minor version) and `pyyaml` are the adapter's libraries.
10. **Chunks.** One chunk per `chunk_values` values of a document (a constructor argument), in
    document order; the first also emits the snapshot and the document's value findings. Every
    chunk parses the whole file again, so its output depends on nothing but the bytes.

## Alternatives considered

- **Keep ADR 0017 §2 as written: every record carries the writer's version.** It rewrites every
  record and golden file now and at every addition, changes the bytes of packages no addition
  touched, and makes old readers refuse records whose shape never changed. It also leaves the
  package reader unable to read packages written before the addition, against ADR 0023.
- **`StructuredTable` and `StructuredRecord` for configs.** A configuration is a tree, not a
  table, and those are `world` records; the M1 review named configuration snapshots a new kind.
- **One record per snapshot holding the whole tree.** Lines of megabytes that cannot be chunked,
  with every value's citation nested inside, and nothing a binding or a diff can name by itself.
- **The path as a JSON Pointer string only.** `/0` is a key or a position; a typed path is not.
- **Expanding aliases and merge keys.** Exactly the billion-laughs bomb, and merging is an
  interpretation of YAML 1.1 that YAML 1.2 dropped.
- **Typing undeclared YAML as 1.2**, which the 1.2 specification makes the default for its own
  processors. The robots' own readers (PyYAML, ROS 2's rcl) read 1.1-like rules; choosing either
  silently is the assumption non-negotiable 4 forbids. ROS 2's rcl rules are a third reading; a
  `yaml_version` choice for them can be added as a new option value (a new transform).
- **ruamel.yaml**, which reads YAML 1.2 and keeps comments: one maintainer, an API that changes
  between minor releases, and comments come out attached to nodes, which is interpretation we
  would have to undo. **strictyaml** refuses flow style and anchors, which robot configs use. **An
  own YAML parser**: a large grammar to get right for little gain over PyYAML's events.
- **An own TOML parser for spans.** `tomllib` is the reference reader; a second parser deciding
  validity could disagree with it. The scanner only locates what `tomllib` accepted, and if it
  ever cannot, values cite their pointers alone.
- **Byte ranges instead of code-point spans.** UTF-16 files and the text adapter's convention
  favour spans over the decoded text; the encoding and BOM are on the snapshot.
- **Claiming broken files weakly (`NAME_ONLY`)** so the config adapter reports their syntax
  errors. It would tie with the text adapter on damaged text and claim prose that starts like a
  mapping; a broken config is read as text and the name mismatch reported.

## Consequences

- MVL-38 binds a run to a configuration by the source it ran with: the content id names the
  exact snapshot, and the digest says when two runs ran the same values. Comparing two runs needs
  only the package (`compare_configurations`), as `tests/integration/test_config_job.py` shows.
- Adapters for formats carried in JSON, YAML or TOML (rosbag2 `metadata.yaml`, a calibration
  file, GeoJSON) must check their structure and claim `VERIFIED` to win over this generic reader,
  which claims `STRUCTURE`; until they do, those files become configuration snapshots.
- Every later kind gives itself `since`; a package that uses no later kind keeps its bytes.
- Large files cost one parse per chunk: at 8 MiB of YAML (pure-Python PyYAML, about 1 MB/s)
  each call parses for several seconds. Revisit if configs that large are common, or if PyYAML's
  1.1 grammar refuses 1.2-only syntax users rely on, or if consumers need keys' own spans.
