# 0013 — Corpus 2.2.0: machine aliases for every source that names a machine, and the incident in its same-event assertion

- Status: Accepted
- Date: 2026-10-07
- Issue: MVL-191
- Amends: ADR 0009 §4 (the run sheet's machine aliases, the same-event assertion's scope)

## Context

The incident report's ARM-3A was not linked to the run sheet's. Each source keys a machine in the
namespace of the mapping that reads it:

- Deploy's `incident_report` template uses `incident_report.machine`;
- `requalification_csv` uses `requalification.robot`;
- Memory's event-table declaration for the syslog export uses `syslog.host`.

The run sheet declared only `cmms.asset` and `servicenow.ci`. Memory joins ids into `same_as` only
when one stated record declares them together (Memory ADR 0021). So no claim linked
`incident_report.machine:ARM-3A` to `manifest:ARM-3A`. The incident pack's configuration, runs and
clocks sections were empty, and so was ARM-3A's traceability chain.

The same-event assertion named the CMMS stop and the syslog stop, but not the incident report that
both belong to.

## Decision

1. **Aliases.** A machine declares an alias for every source in the hand-over that names it. Each
   alias is in the namespace its mapping keys machines by, and its value is written as the source
   writes it:
   - ARM-3A: `cmms.asset`, `servicenow.ci`, `incident_report.machine`, `requalification.robot` and
     `syslog.host`;
   - AMR-07: `cmms.asset`, `servicenow.ci`, `incident_report.machine` (INC-0007) and
     `requalification.robot` (RQ-S007-0007);
   - AMR-05, AMR-06 and LEG-01 are unchanged.

   PLC-C3 writes syslog, but it is no machine of the run sheet, so it gets no alias.
2. **The check.** A platform test reads the mappings (Deploy's presets, its incident template and
   Memory's event-table declaration in its acceptance-snapshot config). It requires every alias
   namespace to be one those mappings key machines by, and requires each source to write the
   machine with that value. The ingest test already requires every alias to be a stated identifier
   of the machine record with no `alias_namespace_unrepresentable`.
3. **The assertion.** ASR-C3-0011-01's scope gains `{incident_report.incident, INC-C3-0011}`, the
   incident report as Deploy's template identifies it. `require_assertion_scopes` (ADR 0009 §6)
   keeps it declared by the mapped package. The assertion's id, author, ticket and payload are
   unchanged.
4. **Version 2.2.0** (minor, ADR 0007 §3). Only `neptune.yaml` and the assertions file change. No
   evidence id moves, and no answer changes meaning. `assert.same-stop`'s `says` now names the
   incident report too.

## Alternatives considered

- **Have Memory or Deploy match `ARM-3A` across namespaces.** Lost: equal strings in different
  namespaces are never joined without a record that declares both (Memory ADR 0021 §1). Two
  identical names are not the same robot.
- **Rewrite the exports to say `cmms.asset`.** Lost: sources are what the plant gave us. An export
  that names a machine differently needs an alias, never a rewrite (ADR 0009, Consequences).
- **A second assertion for the incident.** Lost: one person stated one event. A second assertion
  would give two hubs for one fact.
- **Major version.** Lost: no cited evidence moved and no answer changed meaning.

## Consequences

- Memory's acceptance snapshot gains six `same_as` claims (10 to 16). Five are machine links and
  one joins the incident record to the stop.
- Deploy's `incident-timeline@3` and `configuration-traceability@3` (Deploy ADR 0019) follow them.
  The demo's incident PDF now fills its configuration and runs sections. Its clocks section stays
  not covered, because Memory states no `has_clock`.
- A new source that names a machine needs an alias in the same corpus version. The namespace test
  fails until it has one.
