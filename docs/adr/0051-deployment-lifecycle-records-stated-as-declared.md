# 0051 — Deployment lifecycle records, stated as declared

- Status: Accepted
- Date: 2026-10-02
- Issue: MVL-83
- Amends: ADR 0020 (the `world` family gains eight kinds); ADR 0037 §1 (schema version 3)

## Context

ISO 10218:2025, ISO 3691-4 and ANSI/RIA R15.08 certify a deployment, not a robot. Each requires
records tied to a site and a configuration: the deployment is commissioned, authorised for an
envelope, operated with interventions, maintained, requalified, and changed after incidents. These
records exist as forms, CMMS rows, tickets and PDFs. Neptune Deploy (P-MVL-15) will parse them, and
its first issue (MVL-112) waits on a canonical place to put them. Forces:

- **Non-negotiables 3, 4 and 8.** A severity `S2`, a risk score `12`, a speed limit of `1.5 m/s`, or
  an "approved with conditions" are what the form says. Ranking severities, converting units or
  reading a decision as a boolean is interpretation.
- **The model grows by addition** (ADR 0023 §1, ADR 0037 §1). New kinds take a new schema version
  and change no byte of a package that does not use them.
- **Lifecycle records name other things by the ids people write.** A form writes `AMR-07`,
  `CFG-AMR07-r3`, `DOCK-1` or `INC-0007`. It does not write a record id, and linking the two is
  identity resolution's job (MVL-35).
- **Robots of every kind.** The same records cover an AMR fleet in a warehouse and an arm in a
  fenced cell, so no field can assume one morphology.

## Decision

1. **Eight kinds in the `world` family, at schema version 3 (`since = 3`).** Each is an evidence
   record whose provenance cites the one declaration it comes from: a form, a ticket, a work order
   or a register row. The assertion is `stated`. The kinds live in `model/lifecycle.py`:
   - `commissioning_baseline`: `commissioned`, `hardware` and `software` inventories (name, model,
     ids, version of the kind the source names), `calibrations` (declared ids), acceptance `tests`,
     residual `constraints`, and `sign_off`.
   - `authorisation_envelope`: permitted `missions`, `payload_min` and `payload_max`, `zones` with
     speed limits, `supervision`, infrastructure `dependencies`, `valid_from` / `valid_until`, and
     `approval`.
   - `intervention`: `mode` (remote assist, on-site), `authority`, `reason`, `commands` in order,
     `start` / `end`, and `outcome`.
   - `maintenance_event`: `performed`, `diagnosis`, `actions`, and `parts` (the part, with the ids
     of the units removed and installed).
   - `requalification_record`: `performed`, `cause`, `corrective_actions`, regression `tests`,
     `result`, and the `return_to_service` decision.
   - `incident_record`: `occurred`, `severity`, `zone`, `location`, involved `assets`, `timeline`
     entries, `description` and `root_cause`.
   - `change_record`: the `changes` (category, target, before, after), `approval`, `effective`,
     and `rollback`.
   - `risk_assessment`: `assessed`, `method`, and `hazards`, each with its scores and mitigations,
     plus `approval`.
2. **Five shared fields place every record in its deployment**, all as declared ids
   (`Knowledge[LogicalId]` or the `Identifiers` list of ADR 0019 §2):
   - `identifiers`: the record's own ids;
   - `site`;
   - `machines`;
   - `configuration`: the configuration the record is bound to, or for a maintenance event the
     as-maintained configuration it states resulted;
   - `related`: other records and evidence it names, such as the incident a change answers, the
     work order behind a requalification, or the video an incident links.

   No field holds a `RecordId` of another declaration. MVL-35 links the ids.
3. **Values are stored as declared.**
   - Severity, results, decisions, authority, supervision mode, method and every score are
     `Knowledge[str]`, verbatim. They are never ranked, mapped or parsed.
   - A hazard's scores are `Score(name, value)`, named by the source's own label (`PLr`,
     `severity`, `risk`) and kept in source order.
   - A `Quantity` is a declared number (`Real`; a declared integer is read with `float()`, as in
     ADR 0019 §6) plus its declared `Unit` through `unit_from_text` (ADR 0013). `m/s` is written
     `m.s^-1` and is never converted.
   - Times are `Timestamp`s on the clock the record names, so "start/end on a named clock" is the
     timestamps' domain. A date-time with an offset is POSIX seconds (ADR 0023 §2).
   - Versions are a `VersionPrimitive` of the kind the source names (ADR 0014). A form that names
     no scheme gives a `DeclaredVersion`.
4. **Lists of stated values are structural.** Statements (commands, actions, constraints,
   mitigations, missions) keep source order and may repeat: two `reset` commands are two commands.
   Each is `Known` or `Ambiguous` non-empty text. Declared-id lists follow ADR 0019 §2: sorted, with
   no repeats. An empty list means the declaration states none. A field a format has no place for
   is `NotCovered`, and a blank is `Unknown`.
5. **No lifecycle logic.** Nothing orders the stages, checks an intervention against its envelope,
   decides whether a requalification passed, or derives a risk level. Each of those is a consumer's
   reading and, if Neptune ever makes it, a `derived/` table (non-negotiable 8).
6. **One implementation pattern for the eight kinds.** Each record and part names one codec per
   field (`_CODECS`). The codec checks, writes and reads that field, and an import-time check fails
   if the codecs and the dataclass fields ever differ. The JSON Schema is still generated from the
   dataclass types (ADR 0021).
7. **The package and its receipt.**
   - A package holding any lifecycle record is a version 3 package (ADR 0037 §1).
   - `receipt.md` lists each new kind with its count in its Records table. No receipt field is added.
   - The package-schema contract is published at **3.0.0**. `SCHEMA_VERSION` is the registry major
     (platform ADR 0002 §3), so a minor version is not possible.
8. **Worked examples.**
   - `mobile_robot` adds a warehouse deployment export: AMR-07 at site S-007 with commissioning,
     its authorisation envelope, a remote assist, an incident, the change that followed, and the
     risk assessment.
   - `manipulator` adds a cell export: commissioning, risk assessment, a joint-drive replacement,
     and the requalification that returned the cell to service.
   - Between them the two examples hold all eight kinds, and every value cites its JSON pointer.

## Alternatives considered

- **Enums for severity, intervention mode, change category and decision.** Every site writes its own
  scale (`S1`–`S4`, `high/medium/low`, a 1–25 matrix), so an enum either rejects real records or
  maps them. Mapping is interpretation. Lost.
- **Record ids for configuration, zone, site and incident references.** A form never states a
  record id. Resolving one at parse time is the cross-source inference MVL-35 owns. Lost.
- **`StructuredRecord` rows only.** A row keeps cells, not meaning, so Deploy and Memory would each
  re-parse forms to find "the authorised speed in DOCK-1". The issue asks for canonical kinds. Lost.
- **A new `deployment` family.** ADR 0017 §4 families are the design contract's source domains.
  These records are world and record context, and a new family changes how sources are attributed.
  Lost.
- **A lifecycle section in `IngestReceipt`.** It changes the receipt document's shape and every
  receipt id, while the Records table already lists the kinds. Revisit if consumers need more.
- **Hand-written `to_json` and `from_json` per kind, as in `world.py`.** About 600 more lines across
  eight kinds and ten parts, with more room for a field to be written but not read. The codec table
  and its import-time check keep the three paths in step. Lost.

## Consequences

- Neptune Deploy (MVL-112 onward) parses forms, tickets and CMMS exports into these kinds and pins
  package-schema 3.0.0. Memory and Learn read them as `stated` evidence.
- A field a real form needs and these kinds lack becomes a new companion kind or a new kind at a
  later schema version. Kinds are frozen (ADR 0023 §1).
- The kinds' schema version is one constant (`LIFECYCLE_SINCE`). Had another addition taken
  version 3 first, only that constant, the generated schema, the examples and the contract version
  would have moved.
