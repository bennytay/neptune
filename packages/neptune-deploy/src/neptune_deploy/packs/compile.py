"""``PackSpec`` + snapshot -> ``EvidencePack`` (ADR 0013 §3, §5, §6).

A pack is sections of entries; an entry is statements about one node over one interval; a
statement is one claim, cited by id. Nothing in a pack is not a claim of the snapshot, except the
structure around claims and the explicit states that say why there are none:

- ``known``: the entry's claims state it.
- ``ambiguous``: Memory states several readings (``*_candidate`` claims, or a decided reading
  beside them); every reading is shown and none is chosen.
- ``unknown``: Memory states that nothing is stated (``*_unknown`` claims naming the record that
  leaves it open).
- ``conflict``: claims that cannot all hold disagree (a ``one`` predicate with two objects, or a
  timeline event placed at two times on the pack clock); every one is shown, none is chosen.
- A section with no entries is ``not_covered`` with its reason (what was looked for, about which
  nodes, in which snapshot, and what was left out); a section the template does not hold for the
  subject's type is ``not_applicable``.

Claims on a clock other than the pack interval's are never compared with it: they are listed
under ``other_clocks``. Inferred claims are left out unless the spec includes them, and are then
marked on every statement.
"""

import dataclasses
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from functools import cache
from typing import Final

from neptune.identity import canonical_json
from neptune.identity.hashing import content_id
from neptune.model.jsonvalue import JsonObject, JsonValue
from neptune_deploy.packs.appendix import Appendix, build_appendix
from neptune_deploy.packs.errors import PackError
from neptune_deploy.packs.snapshot import Claim, Interval, Node, Snapshot
from neptune_deploy.packs.spec import PackSpec
from neptune_deploy.packs.templates import Hop, SectionTemplate, Template, TemplateRegistry

PACK_SCHEMA: Final = "neptune-deploy.evidence-pack/1"
COMPILER_ID: Final = "neptune-deploy.packs"
COMPILER_VERSION: Final = "1"
PACK_PREFIX: Final = "pack:"


@cache
def builtin_registry() -> TemplateRegistry:
    return TemplateRegistry.builtin()


@dataclass(frozen=True)
class Statement:
    """One claim, as the pack states it."""

    claim: Claim
    role: str  # the template's knowledge role for its predicate

    def to_json(self) -> JsonObject:
        out: dict[str, JsonValue] = {
            "assertion_kind": self.claim.assertion_kind,
            "claims": [self.claim.id],
            "knowledge": self.role,
            "object": self.claim.object,
            "predicate": self.claim.predicate,
            "subject": self.claim.subject.to_json(),
        }
        if self.claim.inferred:
            provenance = self.claim.raw["provenance"]
            assert isinstance(provenance, Mapping)
            out["inferred"] = {
                "confidence": self.claim.raw["confidence"],
                "model": provenance["model"],
            }
        return out


@dataclass(frozen=True)
class Entry:
    node: Node
    valid: Interval
    knowledge: str  # known | ambiguous | unknown | conflict
    statements: tuple[Statement, ...]
    placement_records: tuple[str, ...] | None = None  # timeline sections only

    def to_json(self) -> JsonObject:
        out: dict[str, JsonValue] = {
            "knowledge": self.knowledge,
            "node": self.node.to_json(),
            "statements": [s.to_json() for s in self.statements],
            "valid": self.valid.to_json(),
        }
        if self.placement_records is not None:
            out["placement_records"] = list(self.placement_records)
        return out

    @property
    def claim_ids(self) -> tuple[str, ...]:
        return tuple(s.claim.id for s in self.statements)


@dataclass(frozen=True)
class ScopeNode:
    node: Node
    via: tuple[str, ...]  # the claims of every path that reached it; () for the subject

    def to_json(self) -> JsonObject:
        return {"node": self.node.to_json(), "via": list(self.via)}


@dataclass(frozen=True)
class FindingNote:
    """A resolver finding about a claim the section cites."""

    id: str
    code: str
    claim: str
    others: tuple[str, ...]

    def to_json(self) -> JsonObject:
        return {"claim": self.claim, "code": self.code, "id": self.id, "others": list(self.others)}


@dataclass(frozen=True)
class Section:
    template: SectionTemplate
    knowledge: str  # known | not_covered | not_applicable
    scope: tuple[ScopeNode, ...]
    entries: tuple[Entry, ...]
    other_clocks: tuple[Entry, ...]
    findings: tuple[FindingNote, ...]
    excluded_inferred: tuple[str, ...]
    outside_interval: int
    reason: JsonObject | None
    cited: frozenset[str] = frozenset()  # every claim its entries and scope cite

    def to_json(self) -> JsonObject:
        out: dict[str, JsonValue] = {
            "description": self.template.description,
            "entries": [e.to_json() for e in self.entries],
            "excluded": {
                "inferred": len(self.excluded_inferred),
                "outside_interval": self.outside_interval,
            },
            "findings": [f.to_json() for f in self.findings],
            "id": self.template.id,
            "kind": self.template.kind,
            "knowledge": self.knowledge,
            "other_clocks": [e.to_json() for e in self.other_clocks],
            "predicates": dict(self.template.predicates),
            "scope": [s.to_json() for s in self.scope],
            "title": self.template.title,
        }
        if self.reason is not None:
            out["reason"] = self.reason
        return out


@dataclass(frozen=True)
class EvidencePack:
    id: str
    spec: PackSpec
    template: Template
    snapshot: Snapshot
    sections: tuple[Section, ...]
    claims: tuple[Claim, ...]  # every claim the pack cites, by id
    appendix: Appendix
    excluded_inferred: int
    included_inferred: int

    def to_json(self) -> JsonObject:
        return {
            "appendix": self.appendix.to_json(),
            "claims": [claim.raw for claim in self.claims],
            "compiler": {"id": COMPILER_ID, "version": COMPILER_VERSION},
            "id": self.id,
            "inference": {
                "excluded": self.excluded_inferred,
                "included": self.included_inferred,
                "policy": self.spec.inference,
            },
            "schema": PACK_SCHEMA,
            "sections": [s.to_json() for s in self.sections],
            "snapshot": {
                "generation": self.snapshot.generation,
                "graph_schema_version": 1,
                "head": self.snapshot.head,
                "id": self.snapshot.id,
                "vocabulary_version": self.snapshot.vocabulary_version,
            },
            "spec": self.spec.to_json(),
            "subject": self.spec.subject.to_json(),
            "template": {
                "description": self.template.description,
                "id": self.template.id,
                "sha256": self.template.sha256,
                "title": self.template.title,
                "version": self.template.version,
            },
        }


def pack_id(spec: PackSpec, template: Template) -> str:
    """``pack:sha256:<hex>`` of the spec (which names the snapshot), the template's content hash
    and the compiler version: the same inputs always name the same pack, and a compiler upgrade
    names a new one."""
    payload: JsonObject = {
        "compiler": {"id": COMPILER_ID, "version": COMPILER_VERSION},
        "spec": spec.to_json(),
        "template": template.sha256,
    }
    return PACK_PREFIX + content_id(canonical_json.dumps(payload))


def compile_pack(
    spec: PackSpec, snapshot: Snapshot, registry: TemplateRegistry | None = None
) -> EvidencePack:
    """Compile ``spec`` over ``snapshot``. Refuses a snapshot that is not the one the spec names,
    a template the registry lacks, and a subject type the template does not cover."""
    if spec.snapshot != snapshot.id:
        raise PackError(
            "snapshot_mismatch", f"the spec names {spec.snapshot}; the snapshot is {snapshot.id}"
        )
    template = (registry or builtin_registry()).get(spec.template_id, spec.template_version)
    if spec.subject.node_type not in template.subject_types:
        raise PackError(
            "subject_type_unsupported",
            f"{template.key} covers {', '.join(template.subject_types)}, not"
            f" {spec.subject.node_type}",
        )
    sections = tuple(_section(section, spec, snapshot) for section in template.sections)
    cited: set[str] = set()
    for section in sections:
        cited.update(section.cited)
        for note in section.findings:
            # A finding's other claims join the claim set, but an excluded inference never does.
            cited.update(
                i
                for i in (note.claim, *note.others)
                if i in snapshot.versions
                and (spec.inference == "include" or not snapshot.versions[i].inferred)
            )
    claims = tuple(snapshot.versions[i] for i in sorted(cited))
    excluded = {i for section in sections for i in section.excluded_inferred}
    excluded.update(
        i
        for section in sections
        for note in section.findings
        for i in (note.claim, *note.others)
        if i in snapshot.versions and i not in cited
    )
    return EvidencePack(
        id=pack_id(spec, template),
        spec=spec,
        template=template,
        snapshot=snapshot,
        sections=sections,
        claims=claims,
        appendix=build_appendix(claims, snapshot.head),
        excluded_inferred=len(excluded),
        included_inferred=sum(1 for c in claims if c.inferred),
    )


# --- Sections -----------------------------------------------------------------------------------


def _section(template: SectionTemplate, spec: PackSpec, snapshot: Snapshot) -> Section:
    if spec.subject.node_type not in template.subject_types:
        return Section(
            template=template,
            knowledge="not_applicable",
            scope=(),
            entries=(),
            other_clocks=(),
            findings=(),
            excluded_inferred=(),
            outside_interval=0,
            reason={
                "section_subject_types": list(template.subject_types),
                "subject_type": spec.subject.node_type,
            },
        )
    include = spec.inference == "include"
    excluded: set[str] = set()
    scope = _scope(template.about, spec.subject, snapshot, include, excluded)
    inside: list[Statement] = []
    beyond: list[Statement] = []  # on the pack clock, outside the interval
    other: list[Statement] = []
    on_clock: set[Node] = set()  # nodes with a selected claim on the pack clock
    for node in sorted(scope):
        for claim in snapshot.by_subject.get(node, ()):
            role = template.predicates.get(claim.predicate)
            if role is None:
                continue
            if claim.inferred and not include:
                excluded.add(claim.id)
                continue
            statement = Statement(claim, role)
            if claim.valid.clock == spec.clock:
                on_clock.add(node)
            if claim.valid.overlaps(spec.interval):
                inside.append(statement)
            elif claim.valid.clock == spec.clock:
                beyond.append(statement)
            else:
                other.append(statement)
    if template.kind == "timeline":
        entries = _timeline(inside, beyond, other, snapshot.cardinality)
        other_entries = _grouped(
            [s for s in other if s.claim.subject not in on_clock], snapshot.cardinality
        )
    elif template.kind == "states":
        entries = _grouped(inside, snapshot.cardinality)
        other_entries = _grouped(other, snapshot.cardinality)
    else:
        entries = _each(inside)
        other_entries = _each(other)
    scope_nodes = tuple(ScopeNode(node, tuple(sorted(scope[node]))) for node in sorted(scope))
    shown = frozenset(i for e in (*entries, *other_entries) for i in e.claim_ids)
    outside = sum(1 for s in beyond if s.claim.id not in shown)
    cited = shown | {i for s in scope_nodes for i in s.via}
    findings = tuple(
        FindingNote(f.id, f.code, f.claim, f.others)
        for f in snapshot.current_findings
        if f.claim in cited or cited.intersection(f.others)
    )
    reason: JsonObject | None = None
    knowledge = "known"
    if not entries and not other_entries:
        knowledge = "not_covered"
        reason = {
            "inferred_excluded": len(excluded),
            "missing_from_vocabulary": sorted(
                p for p in template.predicates if p not in snapshot.cardinality
            ),
            "nodes": [n.to_json() for n in sorted(scope)],
            "outside_interval": outside,
            "predicates": sorted(template.predicates),
            "snapshot": snapshot.id,
        }
    return Section(
        template=template,
        knowledge=knowledge,
        scope=scope_nodes,
        entries=entries,
        other_clocks=other_entries,
        findings=findings,
        excluded_inferred=tuple(sorted(excluded)),
        outside_interval=outside,
        reason=reason,
        cited=cited,
    )


def _scope(
    paths: Iterable[tuple[Hop, ...]],
    subject: Node,
    snapshot: Snapshot,
    include: bool,
    excluded: set[str],
) -> dict[Node, frozenset[str]]:
    """Every node a path reaches from ``subject``, with the claims it went through. Hops follow
    current claims whatever their valid time (the hop claims are cited, so their times show);
    an excluded inferred claim is never followed."""
    reached: dict[Node, frozenset[str]] = {}
    for path in paths:
        frontier: dict[Node, frozenset[str]] = {subject: frozenset()}
        for hop in path:
            step: dict[Node, frozenset[str]] = {}
            for node, via in frontier.items():
                index = snapshot.by_subject if hop.direction == "out" else snapshot.by_object
                for claim in index.get(node, ()):
                    if claim.predicate != hop.predicate:
                        continue
                    target = claim.object_node if hop.direction == "out" else claim.subject
                    if target is None:
                        continue
                    if claim.inferred and not include:
                        excluded.add(claim.id)
                        continue
                    step[target] = step.get(target, frozenset()) | via | {claim.id}
            frontier = step
        for node, via in frontier.items():
            reached[node] = reached.get(node, frozenset()) | via
    return reached


def _order(statements: Iterable[Statement]) -> tuple[Statement, ...]:
    return tuple(
        sorted(statements, key=lambda s: (s.claim.predicate, s.claim.object_key, s.claim.id))
    )


def _knowledge(statements: Sequence[Statement], cardinality: Mapping[str, str]) -> str:
    objects: dict[str, set[bytes]] = defaultdict(set)
    for s in statements:
        if cardinality.get(s.claim.predicate) == "one" and s.role == "known":
            objects[s.claim.predicate].add(s.claim.object_key)
    if any(len(found) > 1 for found in objects.values()):
        return "conflict"
    roles = {s.role for s in statements}
    if roles == {"known"}:
        return "known"
    if roles == {"unknown"}:
        return "unknown"
    return "ambiguous"


def _entry_key(entry: Entry) -> tuple[tuple[str, int, int, int], Node]:
    return (entry.valid.sort_key(), entry.node)


def _each(statements: Iterable[Statement]) -> tuple[Entry, ...]:
    """``claims`` sections: one entry per claim."""
    entries = [Entry(s.claim.subject, s.claim.valid, s.role, (s,)) for s in statements]
    return tuple(sorted(entries, key=lambda e: (e.node, e.valid.sort_key(), e.claim_ids)))


def _slots(statements: Iterable[Statement]) -> dict[tuple[Node, Interval], list[Statement]]:
    slots: dict[tuple[Node, Interval], list[Statement]] = defaultdict(list)
    for s in statements:
        slots[(s.claim.subject, s.claim.valid)].append(s)
    return slots


def _grouped(statements: Iterable[Statement], cardinality: Mapping[str, str]) -> tuple[Entry, ...]:
    """``states`` sections: one entry per node and valid interval, its state decided by the
    roles of its claims. Entries of one node whose intervals overlap and that state different
    objects of a ``one`` predicate are both a conflict."""
    entries = [
        Entry(node, valid, _knowledge(group, cardinality), _order(group))
        for (node, valid), group in _slots(statements).items()
    ]
    entries.sort(key=lambda e: (e.node, e.valid.sort_key()))
    by_node: dict[Node, list[int]] = defaultdict(list)
    for index, entry in enumerate(entries):
        by_node[entry.node].append(index)
    conflicted: set[int] = set()
    for indices in by_node.values():
        for i, a in enumerate(indices):
            for b in indices[i + 1 :]:
                if _clash(entries[a], entries[b], cardinality):
                    conflicted.update((a, b))
    return tuple(
        dataclasses.replace(e, knowledge="conflict") if i in conflicted else e
        for i, e in enumerate(entries)
    )


def _clash(a: Entry, b: Entry, cardinality: Mapping[str, str]) -> bool:
    if not a.valid.overlaps(b.valid):
        return False
    for left in a.statements:
        if left.role != "known" or cardinality.get(left.claim.predicate) != "one":
            continue
        for right in b.statements:
            if (
                right.role == "known"
                and right.claim.predicate == left.claim.predicate
                and right.claim.object_key != left.claim.object_key
            ):
                return True
    return False


def _timeline(
    inside: Sequence[Statement],
    beyond: Sequence[Statement],
    other: Sequence[Statement],
    cardinality: Mapping[str, str],
) -> tuple[Entry, ...]:
    """``timeline`` sections: one entry per event placement on the pack clock, in time order. An
    event placed at two times on that clock is a conflict, and every placement of it on that
    clock is shown, inside the interval or not. Records that every claim of a placement cites
    but not every claim of the event does are that placement's own (the clock mapping and target
    clock it was placed through)."""
    slots = _slots(inside)
    elsewhere = _slots(beyond)
    times: dict[Node, int] = defaultdict(int)
    for node, _valid in (*slots, *elsewhere):
        times[node] += 1
    conflicted = {node for node, _valid in slots if times[node] > 1}
    shown = {**slots, **{key: group for key, group in elsewhere.items() if key[0] in conflicted}}
    shared: dict[Node, frozenset[str]] = {}
    for s in (*inside, *beyond, *other):
        records = frozenset(s.claim.records)
        node = s.claim.subject
        shared[node] = shared[node] & records if node in shared else records
    entries: list[Entry] = []
    for (node, valid), group in shown.items():
        own = frozenset.intersection(*(frozenset(s.claim.records) for s in group))
        knowledge = "conflict" if node in conflicted else _knowledge(group, cardinality)
        entries.append(
            Entry(node, valid, knowledge, _order(group), tuple(sorted(own - shared[node])))
        )
    return tuple(sorted(entries, key=_entry_key))
