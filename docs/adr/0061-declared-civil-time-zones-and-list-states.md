# 0061 — Declared civil time zones, and lists that can be blank

- Status: Accepted
- Date: 2026-10-02
- Issue: MVL-202
- Amends: ADR 0005 §2 and ADR 0012 §3 (a clock's declared civil zone is the companion kind
  `civil_time_zone`, not a domain property); ADR 0023 §2 (a zone-less civil date-time may have a
  zone the source declares elsewhere); ADR 0037 §1 and ADR 0023 §1 (a field may gain states, and a
  record that uses one is written at the version that added it); ADR 0051 §4 (lifecycle lists are
  states, and a blank list is `Unknown`, not `()`); enforces ADR 0051 §1 on every nested value

## Context

Neptune Deploy's lifecycle mapper (Deploy ADR 0002, MVL-113, PR #80) hit two gaps in the model.
It works around both today:

1. **Declared civil zones.** CMMS, ticketing and incident exports write local civil times
   (`2026-03-04 14:10`). The zone (`Europe/Berlin`) is stated by the export or by the mapping that
   reads it. ADR 0023 §2 counts a zone-less date-time on its own civil clock, with timescale
   `Unknown`. The model has no place for the zone, so Deploy keeps it in the clock's scope and in
   its transform config. Non-negotiable 4 requires the zone to be stored as declared and never
   converted at parse time.
2. **Blank lists.** The lifecycle kinds' lists (an incident's machines, a work order's replaced-part
   serials) are bare tuples. A blank cell therefore becomes `()`, which says "the declaration
   states none". Deploy writes `()` plus a `list_cell_blank` finding. Non-negotiable 3 says a
   blank never becomes a fact.

The model is frozen and grows only by addition (ADR 0023 §1). A kind's fields never change, and
optional fields were rejected because a missing key would come back as a meaning. ADR 0023 left
one door open for a declared offset or zone: "a companion kind can carry it". The lifecycle kinds
are on package-schema 4 (ADR 0051), and Deploy is about to pin them.

## Decision

1. **`civil_time_zone` (family `reference`, since 5)** is a companion of `timestamp_domain`. It
   follows the extension rule of ADR 0023 §1 and ADR 0019 §4.
   - Its fields are `domain` (the `TimestampDomain` record id) and `zone: Knowledge[str]`.
   - `zone` is the IANA tz database name exactly as declared, such as `Europe/Berlin`, `UTC` or
     `Etc/GMT-5`.
   - It is checked by syntax only: `/`-joined components of letters, digits and `._+-`, no `.` or
     `..` component, at most 255 characters. It is never looked up in a tz database. Whether a
     name exists depends on the database release, and a record's bytes may not.
   - `Known` (or `Ambiguous` when declarations disagree) is a stated zone. `Unknown` means the
     source could state one and does not. `NotCovered` means its format has no place for one.
     `KnownAbsent` and `NotApplicable` are refused: a civil clock always has a zone, and the
     question is only whether the source says it.
   - A text that is not an IANA name (`W. Europe Standard Time`, `+01:00`) gives `Unknown` and a
     finding. The text stays in the cited bytes. A fixed offset is not a zone. ADR 0023 §2 already
     makes a date-time with an offset an instant.
2. **Nothing converts.** With a declared zone, the domain stays exactly as ADR 0023 §2 builds it:
   ticks count from 1970-01-01T00:00:00 of the civil clock, epoch `unix`, timescale `Unknown`.
   - Reading the ticks as instants needs a tz database release, and must resolve DST folds and
     gaps. That reading is a derived transform, with the release as a library version.
   - No `civil` timescale member is added. It would repeat the companion and still not say which
     zone.
3. **Where the zone lives, and why there.**
   - A zone describes the clock that the ticks count, so it attaches to the domain and not to each
     `Timestamp`. `Timestamp` is `(ticks, domain_id)` and has no room for more (ADR 0005 §1).
   - `TimestampDomain` cannot gain a field (ADR 0023 §1). The companion adds the zone without
     changing a byte of any existing domain or package, and a package that does not use it stays
     at its version.
   - There is no per-timestamp override. A source that declares a zone per row (a `timezone`
     column) gives each row's time cells their own domain, with `scope` naming the row, and one
     companion each.
   - `provenance` cites what declares the zone.
     - If the source's own bytes state it (a header, a `timezone` key, a column), the companion
       cites those bytes and is `stated` or `observed` as the adapter reads it.
     - If the reading transform's configuration states it (a mapping file that says "this
       export's times are Europe/Berlin"), the companion cites the evidence it applies to (the
       table or column). Its `transform` is the one whose configuration holds the zone, and it is
       `stated`. The same pattern applies when an adapter reads YAML under a configured version
       (ADR 0037 §6).
   - An adapter writes at most one companion per domain from what it reads. Disagreeing
     declarations are `Ambiguous`.
4. **Lists that can be blank: `Listed[T] = Knowledge[tuple[T, ...]]`** (`neptune.model.lists`).
   This applies to every list on the lifecycle kinds and their parts:
   - `identifiers`, `machines`, `related`, `calibrations` and `assets`;
   - the `hardware` and `software` inventories, and each item's `identifiers`;
   - `tests`, `constraints`, `missions`, `zones`, `dependencies`, `commands`, `actions` and
     `parts`;
   - a part's `removed` and `installed`;
   - `corrective_actions`, `timeline`, `changes` and `hazards`;
   - a hazard's `scores` and `mitigations`.

   The states mean:
   - `Known(())` is a list the declaration states is empty. `Known((a, b))` holds its items, under
     ADR 0051 §4's rules for items (ids sorted with no repeats; statements in source order).
   - `Unknown` is a blank list.
   - `NotCovered` is a list the format has no place for, and `NotApplicable` one that does not
     apply.
   - `KnownAbsent` is refused. "Declared empty" is `Known(())`: one fact with one encoding.
   - `Ambiguous` is refused for a whole list. An item in doubt is an `Ambiguous` item of a `Known`
     list, as before. Otherwise a consumer that indexes stated ids would find them inside a
     rejected reading of the whole list.
5. **The JSON keeps every version 4 byte.**
   - A `Known` list that inherits the record's provenance is written as the bare array, which is
     the version 4 shape.
   - Any other state, including a `Known` list citing its own evidence, is a `Knowledge` object
     (ADR 0011) whose value is that array.
   - A reader refuses a `Known` object that inherits its provenance, because that is the array
     written another way.
   - In the JSON Schema, each such field is `Listed_<item>`: the array, a `known` object with its
     own provenance, `unknown` or `not_covered`, or `not_applicable`.
6. **A record is written at the lowest version whose readers read it.** This amends ADR 0037 §1:
   a field may gain states, as an enum gains members.
   - A lifecycle record whose lists are all bare arrays is written at 4, as before. One holding
     any other list state is written at 5 (`LIST_STATES_SINCE`), so a version 4 reader refuses it
     by version and never by key.
   - A reader also refuses a line that declares an older version than its content uses.
   - The record's version is its `schema_version` property, and `kinds.record_version` reads it.
   - A package is written at `records_version`: the highest of its records' versions, not only
     its kinds'. The package reader checks the manifest against the records it read.
7. **Schema version 5.** It adds `civil_time_zone` and the list states.
   - Package-schema is published at **5.0.0**, because `SCHEMA_VERSION` is the registry major
     (platform ADR 0002 §3).
   - Catalog-api takes a minor bump: it adds a kind and accepts every earlier document.
   - No worked example or golden package changes, because none uses a zone or a list state.
8. **Every value on a lifecycle record is stated, not only the record** (enforces ADR 0051 §1).
   A state may cite its own evidence, but any explicit provenance anywhere in a lifecycle record
   must be `stated`. This covers a field, a list, each item, an `Ambiguous` candidate and every
   value inside a part. Inherited provenance is the record's, which is already stated. A
   constructor or reader given an `observed` citation refuses it, so such a line never
   round-trips.

## Alternatives considered

- **An optional `civil_zone` field on `TimestampDomain`.** ADR 0023 §1 rejects optional fields: a
  missing key would come back as a meaning, and every reader would handle two shapes of the kind
  forever. It would also rewrite every domain's bytes unless the field were omitted. Lost.
- **The zone on each `Timestamp`.** It repeats one fact on every value and breaks ADR 0005 §1's
  two-part timestamp. Every series column would need it as well. Lost.
- **The zone in the domain's `scope` or `field`** (Deploy's workaround). Those name where the
  ticks are read from, verbatim. A zone there is a fact hidden in a locator. Lost.
- **Validating against a tz database at parse time.** The answer depends on the installed
  release, so the same bytes would give different records on two hosts (non-negotiable 5). Lost.
- **New companion kinds, or new `*_state` fields, for blank lists.** A reader that ignores the
  companion still reads `()` as "none". New fields cannot be added to a frozen kind. Lost.
- **Always writing lists as `Knowledge` objects.** That changes every version 4 lifecycle record's
  bytes and ids' inputs, and a version 4 reader could no longer read packages that have no blank.
  Lost.
- **Allowing `KnownAbsent` for a declared-empty list.** It gives two encodings of one fact, so two
  adapters could write different bytes for the same declaration. Lost.
- **Allowing `Ambiguous` for a whole list** (a cell whose separator is in doubt). No source needs it
  yet. Consumers that walk records for stated ids, such as the Ledger's index, would read every
  candidate's items as stated. It can be added later as a state, by ADR. Lost.
- **Widening every declared list in the model** (`Machine`, `Site` and `Asset` identifiers and
  aliases, calibration parameters). These are the ids and names one declaration gives the record
  itself, or the entries a parsed file holds. For them "none given" is exactly what `()` says, and
  no source has needed more. They can widen the same way later, through an ADR. Deferred.

## Consequences

- Deploy can drop both workarounds in a follow-up on its own track. It writes a
  `civil_time_zone` per civil clock instead of the zone in scope and config. It writes `Unknown`
  instead of `()` plus `list_cell_blank`.
- Consumers that read lifecycle lists must handle a state where they read an array before. A
  version 4 consumer is protected: it refuses a version 5 record by version.
- A package's version can depend on a record's content, not only its kinds. The store and the
  receipt compute it from the records, and `kinds_at(version)` still decides the tables.
- Readers that want instants from civil times must join `civil_time_zone` to the domain and apply
  a tz database in a derived transform.
- Revisit if a source declares zones per value often enough that per-row domains cost more than a
  per-timestamp form, or if another list in the model is found blank in real data.
