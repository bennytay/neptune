# 0062 — Assertion records: human assertions, acceptances and retractions as stated evidence

- Status: Accepted
- Date: 2026-10-02
- Issue: MVL-183
- Amends: ADR 0017 §4 (a new family, `assertion`)

## Context

Memory's identity policy (G1), spatial baselines (G3) and incident reconstruction all rest on "an
operator assertion recorded as stated evidence": a person confirming that two ids name one robot,
accepting a manipulator cell's commissioning baseline, annotating an incident, or withdrawing an
earlier statement. Nothing in the model can hold one. Forces:

- **Evidence ≠ interpretation.** What a person asserted is evidence (`stated`); what it does to a
  claim (merge two threads, adopt a baseline, discount a retracted link) is Memory's decision.
  A memory a human can silently edit is worse than one a human cannot correct.
- **Assertions arrive as sources** (a JSON file, a console export, a ticket comment), so they need
  content ids, provenance and findings like any other evidence: the normal ingest path.
- **No silent assumptions** about time and identity: an author is a declared id, never resolved to
  a person; an authored time is civil time with whatever zone the source states, never converted.
- **Determinism.** Same bytes, adapter version and config give the same records. When the Ledger
  registers a package is not in the bytes.
- **The model grows by addition** (ADR 0023 §1, ADR 0037 §1): a new kind with `since`, the package
  schema contract versioned by `SCHEMA_VERSION` (platform ADR 0002).
- MVL-202 (ADR 0061, in review) adds a `CivilTimeZone` companion kind for a clock's declared zone.
  It is not on `main`, and this issue must not wait for it.

## Decision

1. **One record kind, `assertion`, in a new family `assertion`** (`model/assertion.py`, `since` 4).
   It is an evidence record (ADR 0017 §5): `id` from its record-level evidence, one `provenance`,
   which must be `stated` (observed or inferred provenance is refused). Fields, each `Knowledge`:
   - `identifier: LogicalId`: the id the source gives the assertion, which a retraction names.
   - `assertion_type: AssertionType`: `same_identity`, `distinct_identity`, `accept_baseline`,
     `reject_baseline`, `annotate` or `retract`. A new type is a new enum member through an ADR.
   - `author: LogicalId`: a declared identity (`{namespace, value}`), never resolved here.
   - `authored_at: Timestamp` and `authored_zone: str` (§4).
   - `scope: tuple[RecordId | LogicalId, ...]`: the records (by id) and real-world things (by
     logical id) the assertion is about, in declared order, one state for the whole list:
     `Known(())` declares nothing in scope; `Unknown` means the list was not read. A record id is
     a string and a logical id an object in JSON. Neither is resolved or checked to exist.
   - `retracts: LogicalId`: the declared id of the assertion a `retract` withdraws (§5).
   - `payload: str`: the declared payload's JSON text exactly as written (its span), never
     re-typed, so numbers keep their spelling and repeated keys survive; consumers parse it.
   - `rationale: str`: free text, citing the `Span` it is written at. The issue's "rationale as a
     `DocumentSpan`" is exactly this: a text value whose state cites its span in the decoded
     source, as `DocumentBlock` text does; no new span type is needed.
   - `signature: str` (as declared: a detached signature or its reference) and `ticket: LogicalId`
     (a tracker's key in its namespace): optional references.

   Required fields (`identifier`, `assertion_type`, `author`, `authored_at`, `authored_zone`,
   `scope`) are `Known`, `Ambiguous`, `Unknown` or `NotCovered`; optional parts (`payload`,
   `rationale`, `signature`, `ticket`) may also be `KnownAbsent`; `retracts` is §5's.
2. **The compiler stores; it applies nothing.** No assertion changes, merges, hides or re-times any
   other record. Identity links stay ADR 0050's; an assertion of `same_identity` is a person's
   statement Memory may use to create or confirm one, not an `IdentityLink`.
3. **The `neptune.assertions` file, version 1**, read by the `assertion` adapter:
   ```json
   {"format": "neptune.assertions", "version": 1, "assertions": [
     {"id": {"namespace": "dc-north.fleet-console", "value": "ASR-2026-0107"},
      "assertion_type": "same_identity",
      "author": {"namespace": "dc-north.staff", "value": "m.okafor"},
      "authored_at": "2026-09-14T10:32:05+02:00", "authored_zone": "Europe/Berlin",
      "scope": [{"namespace": "dc-north.asset_tag", "value": "AMR-07"}, "rec:sha256:…"],
      "payload": {"shift": "B"}, "rationale": "…", "signature": "…",
      "ticket": {"namespace": "jira.dc-north", "value": "FLEET-412"}}]}
   ```
   - The root holds `format`, `version` and `assertions` exactly once each; otherwise nothing is
     read (`assertion.not_assertions`). Only `version` 1 is read: a later version may change what
     a key means (`assertion.version_unsupported`).
   - Each entry requires `id`, `assertion_type`, `author`, `authored_at` and `scope`; `retracts`
     is required for a `retract` and refused for any other type. `authored_zone`, `payload`,
     `rationale`, `signature` and `ticket` are optional. Ids are exactly `{namespace, value}`
     (namespace a lowercase token, value non-empty text).
   - Missingness: a required key missing, `null`, repeated or unreadable is `Unknown` with a
     finding. Version 1 defines a left-out or `null` optional part as none: `KnownAbsent`, citing
     the entry (left out) or the `null` (written). A blank text is `Unknown`. A left-out
     `authored_zone` is `Unknown`: the format has a place for it and the entry does not state it.
     Keys the version does not define are reported (`assertion.unknown_key`) and not read.
   - Citations: an assertion cites `JsonPointer("/assertions/<i>")`; each value its `Span` in
     the decoded text (code points, the shared structured readers' convention, ADR 0055).
   - Hostile input: the shared JSON reader (standard library `json` with hooks, nothing
     evaluated) under `max_bytes` (16 MiB), `max_depth` (64), `max_path_ratio` (64),
     `max_scalar_length` (1 MiB) and `max_assertions` (100,000); everything past a limit is a
     finding, never an exception. An entry that is not an object is skipped with a finding.
   - Probing reads content only: `VERIFIED` where the whole file parses with this format at its
     root, `SIGNATURE` where the head starts an object naming it (a long or broken file). This
     beats the config adapter's `STRUCTURE` claim on the same bytes.
4. **Time and zone, as declared.** `authored_at` is RFC 3339 (`T`, `Z` upper-case; fraction up to
   nine digits) or a date alone, counted by ADR 0023 §2: with `Z` or an offset, POSIX ticks
   (timescale `posix`); without, ticks of its own civil clock (timescale `Unknown`); a date alone,
   days. Each assertion's time is on a `TimestampDomain` of its own (role `document`, field
   `authored_at`, scope the entry's pointer), as an image's capture time is (ADR 0041).
   `authored_zone` is the IANA zone name exactly as written, checked by spelling only and never
   looked up or applied: whether a tz release has the name depends on the release, and record
   bytes may not. Overlap with ADR 0061: its `CivilTimeZone` companion states a *clock's* zone.
   When it lands, a later adapter version may also emit one for each `authored_at` domain;
   `authored_zone` stays, since fields never change (ADR 0023 §1), and holds the same declared text.
   The Ledger's transaction time (when the package holding the assertion was registered) is the
   Ledger's, added on registration; it is not a field of this record, and a record carrying one
   is refused.
5. **Retraction is a new record, never a deletion.** A `retract` names the assertion it withdraws
   by that assertion's declared `identifier` (a `LogicalId`), not by record id: record ids are
   lineage-scoped and change with a parser upgrade, the declared id does not. `retracts` is
   `NotApplicable` for every other type (an adapter that meets one reports
   `assertion.retracts_not_applicable` and the value stays in the cited bytes), and may be
   either where the type was not read. A retraction of a retraction is allowed. Whether and when
   a retraction takes effect, and what a retraction of an unknown id means, is Memory's.
6. **Version.** The kind is `since` 4, so `SCHEMA_VERSION` is 4 and the package-schema contract
   publishes **4.0.0** (an integer owner constant is the registry major, platform ADR 0002 §3);
   every earlier golden still validates, and a package without assertions keeps its bytes (ADR
   0037 §1). The Ledger's catalog API, which embeds the compiler's kinds, takes **1.4.0**. A new
   planned contract, `assertion-records`, is part of package-schema and rides on its version.
   Number 4 is provisional: kind-adding PRs are numbered in merge order, and a renumber touches
   only the version constants, `since`, contract directories and regenerated outputs.
7. **Goldens.** `tests/golden/assertion/` holds three packages ingested from
   `tests/fixtures/assertion/`: an identity confirmation between two robots of a warehouse fleet
   (with the distinct-identity assertion beside it), a baseline acceptance for a manipulator cell
   (with an earlier rejection), and a retraction of the fleet confirmation (with an annotation).
   They are not worked examples: Memory's G1 golden graph consolidates every worked-example
   record, so an assertion there would change Memory's contract (as ADR 0050 §10 found).

## Alternatives considered

- **Assertions as `derived/` annotations.** They are what a person stated, not what a procedure
  inferred; Memory's policies need them as evidence they can cite.
- **Applying assertions in the compiler** (merging ids, flagging a retracted record). That is
  interpretation, and it would mutate records a package already holds.
- **A kind per assertion type.** Six kinds would share every field; one kind with an enum grows by
  an enum member through an ADR.
- **Retracting by record id.** A parser upgrade changes record ids (ADR 0003); a retraction would
  silently stop naming its target.
- **The Ledger's transaction time in the record.** The same bytes would give different records
  each registration, breaking determinism; the Ledger owns that time and adds it.
- **Payload as a typed value tree** (configuration values). Heavy for an opaque, author-defined
  part; the verbatim text loses nothing and a JSON parser reads it.
- **Waiting for ADR 0061's `CivilTimeZone`.** It is not on `main`; a declared-text field now and a
  companion record later lose nothing.
- **Family `world` or `alignment`.** An assertion is neither record context nor a relation a source
  states about other sources; Memory reads it as a person's statement.

## Consequences

- Memory (G1 identity policy, G3 baselines) and Deploy's console authoring (MVL-184) pin
  package-schema 4.0.0 and write or read the `neptune.assertions` format; the console writes
  version 1 files through the normal ingest path.
- The Ledger indexes `assertion` rows in its default partition until it adds one.
- A field consumers need that is not here (a second author, a signature scheme, an expiry) is a
  new ADR and a companion kind or a format version 2 read by a new adapter version, never an edit.
- Revisit if assertions arrive in another container (a ticket system's export): that is another
  adapter emitting this kind, not a change to it.
