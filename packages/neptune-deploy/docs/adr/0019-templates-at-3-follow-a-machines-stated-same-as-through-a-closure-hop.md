# 0019 — Templates @3 follow a machine's stated same_as through a closure hop

- Status: Accepted
- Date: 2026-10-07
- Issue: MVL-191
- Amends: ADR 0013 §4 and ADR 0014 §1 (a hop direction), ADR 0014 §4 (template versions)

## Context

Memory never merges nodes. It keeps one machine node per id and joins them with `same_as` (Memory
ADR 0008, 0021). The ids come from different sources: an incident report names
`incident_report.machine:ARM-3A`, the run sheet names `manifest:ARM-3A`, and the CMMS names
`cmms.asset:ARM-3A`.

Memory stores each identity once, from the lowest id. A machine's declared ids form a star around
that hub, so one id is one or two hops from any other. A person's assertion can chain further.

The pack compiler never followed `same_as` for machines. In the @2 templates, an incident's
`involves` reached the reported id only. Its configuration, runs and clocks sections were therefore
not covered, even when Memory stated the link (Platform ADR 0013).

## Decision

1. **A `closure` hop direction** (template format `pack-template/1`, additive):
   - `{"predicate": P, "direction": "closure"}` goes from a node to every other node that a chain of
     current P claims joins to it, in either direction;
   - each node reached cites the claims of one shortest chain to it, taken in claim-id order, so the
     chain is deterministic. Those claims are in the pack's claim set and its provenance appendix;
   - **it never follows an inferred claim, whatever the spec's inference policy.** Identity is read
     only as stated or observed. Memory states an inferred identity only as `same_as_candidate`,
     which `closure` over `same_as` never names. A left-out inferred claim is counted as excluded.

   Packs from existing templates are unchanged, so `COMPILER_VERSION` stays `3`.
2. **`incident-timeline@3`** is @2 with these changes:
   - every path through an involved machine also runs through `same_as` `closure` (configuration,
     changes, runs, clocks, the reconstruction's events involving the machine, and co-occurrence);
   - the incident's own records follow `same_as` `closure` instead of one hop each way;
   - a new **machine-identities** section lists the `same_as` claims (known) and `same_as_candidate`
     claims (ambiguous, never followed) about the involved machines' ids.
3. **`configuration-traceability@3`** is @2 with every path also run from each id that `same_as`
   `closure` reaches, plus the same machine-identities section about the subject.
4. **Not linked.** When no `same_as` reaches a machine, its machine-identities section is
   `not_covered`. The reason names the node and the predicates, and the section's description says
   that this means the machine is not linked. Nothing is read from an id that merely looks alike:
   `syslog.host:ARM-3A` is not `manifest:ARM-3A` without a claim that says so.
5. **Changes stay per id.** A `changes` section reads each node's own spans (ADR 0018 §3). A span
   ending on one id and a span starting on another are never read as a change.
6. **Versions.** @1 and @2 are byte for byte unchanged, with their lock entries and goldens. The two
   @3 files are new lock entries. Platform's demo (`harness/demo.py`, PR #162) picks @3.

## Alternatives considered

- **Enumerate `same_as` `out`/`in` hops to depth 2 in the template.** Lost: it is right only for a
  star. An assertion chain is deeper. It also follows inferred claims when the spec includes
  inference.
- **Resolve identity in the compiler for every template.** Lost: it changes what the @1 and @2 packs
  say without a new version (ADR 0013 §4).
- **Follow `same_as_candidate` too.** Lost: a candidate is an ambiguity, never an identity. It is
  listed in machine-identities, every reading shown, and not followed.
- **Match ids with the same value across namespaces.** Lost: two identical names are not the same
  robot (AGENTS.md).

## Consequences

- Over the acceptance snapshot of corpus 2.2.0, `incident-timeline@3` for INC-C3-0011 fills
  machine-identities, configuration in force, configuration changes and runs. Clocks and
  co-occurrence stay not covered, because Memory states no `has_clock` or `co_occurs_within`.
  `configuration-traceability@3` for `manifest:ARM-3A` fills its configuration chain and changes.
- A template author can use `closure` on any node-valued predicate. It is meant for identity
  predicates, and its inferred-claim rule applies to all of them.
- Revisit when Memory states clocks of machines, or when a `same_as` chain needs a depth bound in
  packs.
