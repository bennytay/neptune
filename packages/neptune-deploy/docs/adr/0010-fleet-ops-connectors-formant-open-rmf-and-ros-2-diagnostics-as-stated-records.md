# 0010 — Fleet-ops connectors: Formant and Open-RMF as read-only Sources, ROS 2 diagnostics as a declared mapping

- Status: Accepted
- Date: 2026-10-03
- Issue: MVL-156
- Builds on: ADR 0001 §1 and §4, ADR 0002, ADR 0006; root ADRs 0009 (source revisions), 0020 §5 (structured records), 0026 §6 (local-only), 0051 (stated evidence), 0058 (plugin loader)

## Context

A fleet is run through systems the robots never see: Formant holds a fleet's devices, events, annotations and
remote-assist interventions; Open-RMF holds the tasks it dispatched, the states its fleet adapters reported and
the lane and zone map it plans over; ROS 2 nodes report their own health as `diagnostic_msgs/DiagnosticArray`.
All three say what happened around a deployment. None of them is the robot's own evidence, and none may become
more than it says.

- **What they say is `stated`.** An operator's intervention, a task's assigned robot, a node's `WARN` are claims
  by a person or a system (root ADR 0051). Values stay as written; a blank is `Unknown`; a field the API does
  not offer is `NotCovered`. Nothing ranks, converts or joins.
- **The clocks are not known to be one clock.** Formant's times are ISO text with an offset, Open-RMF's are
  integers a log names `unix_millis_*`, a diagnostic's is `header.stamp` seconds and nanoseconds. Each has its own
  clock record, whose meaning is `Unknown` unless the text or the operator states it.
- **Two of the three need no network.** Open-RMF's history is files (JSON, JSON Lines, a SQLite database the
  api-server wrote) and diagnostics are a compiler package. Only Formant is a service.
- **The compiler has gaps this issue meets.** It has no `Task` kind (a task declaration is a `Run` at best), it
  does not decode message payloads into packages (so a bag's `/diagnostics` is not in any package), and it does
  not yet ingest plugin Sources (ADR 0006, compiler gap 2). Memory G3 (MVL-137) has registered no event
  vocabulary, so there is nothing to map diagnostics onto that is registered.
- **Input is hostile.** A response is untrusted JSON, a log is a file someone else wrote, and a SQLite database
  can be a hostile program (views with unbounded recursion, triggers, `ATTACH`).

## Decision

1. **Plugin surface.** Deploy registers two more `neptune.sources` entry points, each with ADR 0006 §1's factory
   signature `factory(url, *, network, ledger=None, options=None, credentials=None, environ=None)`:
   - `deploy_formant` (`neptune_deploy.sources.fleet_ops:formant_source`): `formant://<organization id>`. The
     workspace is asked before the source is built and before every request (`LocalOnlyError` is policy, never a
     finding).
   - `deploy_open_rmf` (`...:open_rmf_source`): a local directory. `network` is accepted for one signature and is
     never asked, because nothing here uses the network. Credentials are refused: there are none.

   Both return a `FleetOpsSource` with the shape of the compiler's `Source` (`walk`, `open`) over their own entry
   types (`DocumentEntry`, `DocumentReadError`), plus `listing()`, `discover(ledger)`, `catalog()` and `findings()`.
   ROS 2 diagnostics are not a Source: they are a mapper over a compiler package (§7), the way ADR 0002's
   lifecycle mapper is, and register no entry point. Importing any of this touches nothing.
2. **Formant's API, read only.** Five `POST /v1/admin/<route>/query` requests, one per part: `devices`, `events`,
   `annotations`, `interventions` (`intervention-requests`) and `recordings` (`files`). Formant's lists are
   queries with a JSON filter, so they are `POST`; they change nothing. `QueryTransport.post_query` refuses any
   other path, and the transport has no other method. A page is `{"items": [...], "continuationToken": ...}`.
   - The bearer token is the declared `formant_access_token`, else `NEPTUNE_FORMANT_ACCESS_TOKEN`; nothing
     ambient is read. It is printable ASCII with no space, goes to the declared endpoint only, and is in no repr,
     finding or transform. The default endpoint is `https://api.formant.io`; a declared endpoint (an https
     proxy, a loopback emulator) requires `instance`, a declared name `@<name>` that is part of every identity,
     as ADR 0006 §3 requires `store`.
   - Options are declared and closed: `endpoint`, `instance`, one boolean per part, `from`, `to`, `device_ids`,
     `page_size` (1 to 1,000), `max_records`, `max_listing_bytes`, `timeout`, `time_formats`, `clock`. An unknown
     option is refused. A filter is sent as declared and nothing more.
   - Recordings are referenced only: the `files` query's records (id, name, size) are a table, and no bytes are
     fetched. One info finding (`recording_not_fetched`) says so.
   - Limits as ADR 0006 and 0009: a page is at most 8 MiB of strict UTF-8 JSON, a continuation token at most 4 KiB
     and a repeated one stops the part (`pagination_loop`), at most 100,000 pages, `max_records` records and
     `max_listing_bytes` bytes. No redirect is followed. Every request has a deadline.
   - **The deadline holds against HTTP/1.0 and `Connection: close`.** `http.client` forgets a connection's socket
     as soon as the response says it will close it, so ADR 0006's `Transport.abort()` finds nothing to shut down,
     and a server that answers that way and trickles its body outlives the deadline. `QueryTransport` keeps its
     own reference to the socket of the request in flight. The same flaw is in `Transport` itself; see
     Consequences.
3. **Documents and stated records.** Each part's objects become one **document**, `{"items": [...]}` in a fixed
   byte form (sorted keys, ASCII, items sorted by their bytes and de-duplicated), so it is a function of what was
   returned and not of page size, page order or row order. Its revision token is `records:<sha256>`: a changed
   record is a new revision, an unchanged one the same. `walk()` yields the documents and `open()` serves their
   bytes, so the compiler stores what every record cites. Over each document:
   - A `StructuredTable` cites `/items`, a row `/items/<i>` and a cell `/items/<i>/<key>`, all `stated`. A cell is
     the value as given: text is text, a number or boolean keeps its type, an object or array is its sorted JSON
     as text. `null`, an absent key and `""` are `Unknown`. A key the table cannot name (empty, or a lone
     surrogate) and a value it cannot store are `Unknown` with a `value_unrepresentable` finding; they stay in
     the document's bytes.
   - An integer time field (`time`, `startTime`, `endTime`; Open-RMF's `unix_millis_*`) is a `TimestampDomain`
     named by the field, whose role, epoch, timescale and resolution are `Unknown` unless the operator declared
     them (`clock`). The declaration is in the transform, so it is in every id. The row carries a
     `@clock:<field>` cell citing that clock.
   - Strict JSON everywhere: UTF-8, no duplicate keys, no `NaN` or `Infinity`, no number that is not finite
     (`1e999`), bounded nesting and digits.
   - A part read to its end that holds no records is an info finding (`part_empty`); one that failed or stopped
     at a limit is a finding (`part_failed`, `part_limit`, `part_invalid`) and is absent or partial, never
     invented. Other parts are read as if it were not there.

   The shape is the one MVL-155's `sources/stated_records.py` has. It is kept here as
   `sources/fleet_ops/documents.py`, under another name, so that two branches do not collide; unifying them is a
   follow-up once both are on `main`.
4. **Formant interventions are `Intervention` records.** Each item of `interventions` that states a usable `id`
   becomes one record whose provenance is the item and whose every value cites its own key, `stated`
   (`sources/fleet_ops/interventions.py`):
   - `mode` is `interventionType`, `authority` is `authority`, `reason` is `message`, `outcome` is `outcome`,
     `commands` the strings of `commands` in order. `identifiers` is `id` (`formant.intervention`), `machines` is
     `deviceId` (`formant.device`): Formant's own id, never matched to another system's name for the robot.
   - `start` and `end` are `time` and `endTime`, text read by a declared format (root ADR 0023 §2, the reader of
     ADR 0002 §5). The defaults, `%Y-%m-%dT%H:%M:%S.%f%z` and `%Y-%m-%dT%H:%M:%S%z`, are declared in the transform
     and replaceable (`time_formats`). An offset names an instant, so the clock is POSIX from the Unix epoch; text
     no declared format reads is `Unknown` with a `value_unreadable` finding, and stays in the table. Each field
     is its own clock record, and two fields are never merged into one clock.
   - `site` and `configuration` are `NotCovered` (the API does not offer them) and `related` is empty. An item
     with no usable id builds no record (`record_skipped`) and stays in the table.
   Device metadata, events, annotations and recording records are tables, not lifecycle kinds: ADR 0001 §4 admits
   no Deploy type, and none of them is a lifecycle record the compiler's model has.
5. **Open-RMF.** Parts are declared (`files`), each a file below the directory: `tasks` (task states and logs),
   `fleet_states`, `dispatches` and `map`. A file is a JSON array of objects, JSON Lines, or a SQLite table.
   - **Files.** A path is relative, with no `..`, empty, absolute or backslash part and no symlink on the way
     (`path_invalid`, `symlink_refused`); only a regular file is opened (`not_regular_file`, so a FIFO is never
     waited on), no larger than `max_file_bytes` (`file_too_large`), by an `O_NOFOLLOW` descriptor whose type and
     size are checked. A refused file is a `file_refused` finding and the other parts are read.
   - **SQLite is hostile input.** It is read with the standard library, read-only through a URI (`mode=ro`),
     with `PRAGMA query_only` and an authoriser that allows `SELECT`, reads, functions and recursive queries and
     denies everything else (no `ATTACH`, `PRAGMA`, write or DDL). A progress handler bounds a hostile view or
     recursive query by VM instruction count and not by the clock (`work_limit`), so a run is the same on a slow
     machine. The table must exist in `sqlite_master` under the declared name, which is then quoted: it is never
     spliced into a statement as given (`table_missing`). At most `max_rows` rows are read (`row_limit`), in the
     file's scan order, so a cut depends on the file. The database is never written or changed, and a test
     checks its bytes and its directory afterwards.
   - **Rows.** A row is an object of column to value as stored. A column the operator declares as JSON
     (`json_columns`; the api-server stores a task or fleet state as JSON text in `data`) is parsed strictly where
     it parses and stays text where it does not. A blob and an infinite float have no JSON form: the cell is
     absent (`Unknown`) and the count is a `cells_not_recorded` finding.
   - **Tasks are `Run` declarations.** Each task item that states an id becomes one `Run`, `stated` by that item:
     `logical_id` is `rmf.task` over `/booking/id`, `machine` is `rmf.robot` over `<group>/<name>` of
     `/assigned_to` (the fleet and the robot, as stated), `first` and `last` are `unix_millis_start_time` and
     `unix_millis_finish_time`, each on the clock of its own field. The pointers are the api-server's and are
     declarable (`task_fields`; for a database row they are `/data/booking/id` and so on). The compiler has no
     `Task` kind, and a `Run` is a session one piece of evidence declares, which a task record does; a `Task` kind
     is a compiler PR (Consequences), never a Deploy type.
   - **The map is spatial records in the frame the file names.** A map object states levels with vertices, lanes
     and zones. Each level is one item (`{"level", "data", "map", "coordinate_system"}`, `data` the level exactly
     as stated), and becomes one `SpatialArtifact` (`vector_map`) citing the item, in a `Frame` named by the level
     as the file names it, in one `FrameGraph` for the document. The geometry stays in the document's bytes: no
     coordinate is moved, converted or projected; `unit` and `crs` are `NotCovered`; the frame's axes and
     handedness are `Unknown`. Keys of the map beyond its name, coordinate system and levels (lifts, doors) are
     not recorded and say so (`map_keys_not_recorded`).
   - Identity: `ExternalObjectRef("deploy_open_rmf", "<site>/<part>", "records:<sha256>")`. `site` is declared and
     is the operator's name for the deployment. The directory is where bytes were read from, not what they are:
     a copy elsewhere has the same identity and the same bytes.
6. **Overlap is kept, not found.** A Formant intervention and an Open-RMF task that overlap in real time are two
   records of two systems, each citing only its own documents and made by its own transform. Their machines are
   in two namespaces (`formant.device`, `rmf.robot`) and are not matched by name; their times are on two clock
   records, which the model refuses to compare (`DomainMismatchError`); `related` is empty; no link record
   (`identity_link`, `clock_mapping`, `run_assembly`) is made; no finding mentions an overlap. A test builds the
   overlapping fixtures and asserts exactly this, with the overlap computed only by the test's own arithmetic.
   "This intervention happened during that task" is an interpretation, so it belongs to `derived/` or a later
   layer (non-negotiables 2 and 8), never to a connector.
7. **ROS 2 diagnostics are a declared vendor mapping over a package.** `neptune_deploy.diagnostics` maps the
   statuses of a compiler package (a JSON export read by the tabular adapter) to event rows. It never opens a bag
   or a source's bytes.
   - **The mapping file** (`neptune-deploy.diagnostics-mapping/1`, `diagnostics/mapping.py`) is operator data:
     where a row's statuses are (`array`, a JSON pointer), the keys inside one status, the stamp's `sec` and
     `nanosec` pointers, an optional declared clock, `levels` (status code as declared text to event-kind string)
     and `names` (status name to event-kind string, which wins over `levels`). The shipped preset
     `ros2_diagnostics` maps `diagnostic_msgs/DiagnosticStatus`' own codes, `0 OK`, `1 WARN`, `2 ERROR`,
     `3 STALE`, to `diagnostic.ok`, `diagnostic.warn`, `diagnostic.error` and `diagnostic.stale`. The field names
     and codes were checked against ROS 2 Humble's own message definitions with `rosbags`
     (`make_fleet_ops_fixtures.py --oracle`). A vendor copies the file and adds its names. A wrong file is a
     `MappingError` before any record is read.
   - **The strings are the file's declaration, not a vocabulary.** Memory G3 (MVL-137) has not registered an
     event vocabulary, and there is none on `main`. Neptune invents none and registers none: the target strings
     are values the mapping declares, kept as declared, and recorded whole in the transform's config. Validating
     them against the registered vocabulary is deferred: **D3 follow-up, tied to MVL-137**, to run when the
     vocabulary exists (a check over the transform's `levels` and `names` values, no change to records).
   - **Output.** A `diagnostic events` table, one row per status: `event_kind`, then `level`, `name`, `message`
     and `hardware_id` exactly as the export states them (a level stays the integer it was), the stamp's `sec` and
     `nanosec` as stated (never combined), and a `@clock:stamp` cell citing one `TimestampDomain` for
     `header.stamp` whose role, resolution, epoch and timescale are `Unknown` unless the mapping's `clock`
     declares them. A `diagnostic values` table has one row per key and value pair, naming its event row. Every
     cell cites its source cell (a JSON pointer in the export, exactly), `stated`, under the mapper's transform,
     whose upstream is the tabular adapter's.
   - **A status outside the mapping is a finding and stays as declared.** The code is looked up as the level's
     declared text. A level not in `levels` is `status_unmapped` (the code, a count, the first rows, the records),
     the row keeps the level as written, and its `event_kind` is `Unknown` unless its name is in `names`. A status
     with no level is `level_missing`. Nothing is guessed.
   - **Bags.** A bag's `/diagnostics` is a `Stream` of its package. The compiler does not decode message payloads
     into packages, so there is nothing to map: each diagnostics stream (a topic in the mapping's `topics`, or
     schema `diagnostic_msgs/msg/DiagnosticArray`) is a `bag_payload_not_decoded` finding and the bag is not
     opened (ADR 0002 precedent: Deploy consumes packages, never raw sources). When the compiler decodes
     `DiagnosticArray` into a package's series, this mapper reads it there; until then bag diagnostics reach
     Deploy only through an export. A package with no diagnostics is `nothing_to_map`.
   - The output is a new package, as ADR 0002's: the base's source ledger and lineage are carried, the base is
     never changed, and the same package and mapping give byte-identical files.
8. **Findings** are `deploy_formant.*` and `deploy_open_rmf.*`: `part_failed`, `part_limit`, `part_invalid`,
   `part_empty`, `value_unrepresentable`, `value_unreadable`, `record_skipped`, plus `recording_not_fetched`
   (Formant) and `file_refused`, `cells_not_recorded`, `map_keys_not_recorded` (Open-RMF); and
   `deploy_diagnostics_map.*`: `status_unmapped`, `level_missing`, `bag_payload_not_decoded`, `nothing_to_map`.
   They carry codes, counts, statuses, field names and pointers, never an error text, URL, path, header or token.
9. **Fixtures and oracles.** One fixture per source, each spread across embodiments: Formant records are an AMR,
   a manipulator cell and a legged robot (`tests/fixtures/fleet_ops/formant`); Open-RMF is a warehouse AMR fleet
   (`.../rmf`, with a SQLite database generated by `make_fleet_ops_fixtures.py`); diagnostics are a legged
   robot's export and a manipulator cell's rosbag2, both as the compiler's own packages. No test reaches a
   network: an in-process server (`tests/deploy_formant_fake.py`) serves Formant over real HTTP with knobs for
   redirects, statuses, malformed and looping pages and a trickling body. **The Formant and Open-RMF documents
   are recorded-shape fixtures written from the vendors' public API documentation as understood here. They are
   not captures of a live service and are not validated against the vendors' own client models** (neither is
   installable here), and this ADR does not claim the services behave as the fixtures do. A live run is the D2
   gate's, as for ADR 0006. The diagnostics shape is checked against ROS 2's message definitions; the SQLite
   reader is checked with the standard library's.
10. **No new dependencies.** `http.client`, `sqlite3`, `json`, `hashlib` and the compiler's own modules; nothing
    is added to `uv.lock`.

## Alternatives considered

- **Build a `Task` record kind in Deploy.** ADR 0001 §4 forbids a Deploy type; a missing kind is a compiler PR.
  `Run` is the nearest declared kind, and the ADR says why it fits.
- **Link the intervention to the task it overlaps.** It is the most useful join and an interpretation: it needs
  the two clocks to be one clock and the two device names to be one robot, and neither is stated. Linking is
  `derived/`'s, with its own provenance and confidence.
- **Register a diagnostics vocabulary here.** Memory G3 owns the registry; a Deploy-local vocabulary would be
  a second one that the real one then has to replace. The declared mapping carries the strings without claiming
  them.
- **Read the bag's `/diagnostics` here.** That is parsing a raw source a second time, and an adapter's job over a
  byte-level format (ADR 0002 precedent); the finding says so and the gap is the compiler's.
- **Parse the api-server's `data` column by default.** That is a guess about what a column holds; the operator
  declares it.
- **Open SQLite with `immutable=1` or through `sqlite3.deserialize`.** Both skip the file's journal and locking,
  which is right for a read-only medium and wrong for a database the api-server may be writing; `mode=ro` with a
  query-only authoriser is the declared default.

## Consequences

- A Formant organisation and an Open-RMF site are Sources the compiler can ingest by document, with their
  metadata as `stated` tables, interventions as lifecycle records and tasks as runs, each citing the document it
  came from; ROS 2 statuses are event rows with the strings a mapping declares.
- **Compiler gaps, for the compiler's backlog:** a `Task` record kind (Open-RMF tasks are `Run`s until then);
  decoding `diagnostic_msgs/DiagnosticArray` (and message payloads generally) into a package's series, so a
  bag's diagnostics can be mapped without an export; ingesting plugin Sources (ADR 0006, gap 2), which makes
  `walk()` and `open()` reachable from `neptune ingest`.
- **A flaw in ADR 0006's `Transport`:** `abort()` shuts down `connection.sock`, which `http.client` sets to
  `None` once a response says it will close the connection (HTTP/1.0 or `Connection: close`), so an object store
  that answers that way can hold a read open past its deadline. Fixing it is a change to `object_store/transport.py`
  (keep a reference to the socket in `_send`, as `QueryTransport` does), which MVL-154's branch also edits, so it
  is left for a dedicated PR or the D2 gate.
- `sources/fleet_ops/documents.py` duplicates `sources/stated_records.py` (MVL-155) and `QueryTransport`
  duplicates `RobotoTransport`. Both are to be unified once the branches are on `main`.
- D3 follow-up (MVL-137): check the transform's `levels` and `names` values against the registered event
  vocabulary, and file findings for the strings it does not hold. The records are unchanged by it.
- Revisit when a live Formant tenant or an Open-RMF api-server disagrees with the recorded shapes, when the
  compiler gains the kinds above, or when Memory G3 registers the vocabulary.
