"""Parsing the Ledger records the run consolidator reads (ADR 0009 §1).

Parsing is kept apart from the run policy: each parser turns one Ledger record into a typed value
or raises ``Malformed``, and decides nothing about runs. ``consolidate.runs`` applies the policy.

Compiler kinds are read with the compiler's own strict readers, so Memory reads exactly the
package-schema shape:

- ``run`` (root ADR 0018 §1): a session one piece of evidence declares, with its declared
  ``logical_id`` and ``machine`` and its ``first`` / ``last`` instants (both inclusive).
- ``run_assembly`` (root ADR 0050 §7, ADR 0066 §1): the files that form one ``Run``, each member a
  ``SourceRevision`` id with its role and the evidence that places it; ``rule`` and the producer's
  transform are the grouping rule and its version.
- ``source_revision``: which bytes a member revision is, to find the ``Run`` those bytes declare.
- ``clock_mapping`` (root ADR 0050 §5) and ``timestamp_domain``: to place a run's interval on a
  civil clock, only where the evidence states the map.
- ``site``: the site register, read only for the ids it declares.

One kind is a Ledger stand-in until the compiler records what a manifest says a run involved
(root ADR 0047 declares it; no record carries it yet): ``run_declaration {id, run, machine, site,
task, evidence}``. ``run`` names the run by its declared logical id, or by its record as
``{"namespace": "record", "value": <run record id>}``; ``machine``, ``site`` and ``task`` are
compiler ``Knowledge`` of a ``LogicalId``; ``evidence`` cites the manifest entry. It is ``stated``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, TypeVar

from neptune.model.alignment import (
    ClockMapping,
    RunAssembly,
    clock_mapping_from_json,
    run_assembly_from_json,
)
from neptune.model.ids import LogicalId, RecordId, logical_id_from_json, parse_record_id
from neptune.model.knowledge import Ambiguous, Known, from_json
from neptune.model.provenance import EvidenceRef, evidence_ref_from_json, provenance_from_json
from neptune.model.run import Run, run_from_json
from neptune.model.source import SourceRevision, source_revision_from_json
from neptune.model.world import Site, site_from_json
from neptune_memory.consolidate.identity_records import Clock, Malformed, clock, declared

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping

    from neptune.model.jsonvalue import JsonValue
    from neptune.model.knowledge import Knowledge

_T = TypeVar("_T")

# Ledger record kinds the run consolidator reads.
RUN: Final = "run"
RUN_ASSEMBLY: Final = "run_assembly"
SOURCE_REVISION: Final = "source_revision"
CLOCK_MAPPING: Final = "clock_mapping"
TIMESTAMP_DOMAIN: Final = "timestamp_domain"
SITE: Final = "site"
RUN_DECLARATION: Final = "run_declaration"

# The namespace a ``run_declaration`` uses to name a run that declares no logical id by its record.
RECORD_NAMESPACE: Final = "record"

__all__ = [
    "CLOCK_MAPPING",
    "RECORD_NAMESPACE",
    "RUN",
    "RUN_ASSEMBLY",
    "RUN_DECLARATION",
    "SITE",
    "SOURCE_REVISION",
    "TIMESTAMP_DOMAIN",
    "Clock",
    "Declaration",
    "Inferred",
    "Malformed",
    "assembly",
    "clock",
    "declaration",
    "mapping",
    "revision",
    "run",
    "site",
]


class Inferred(ValueError):
    """An inferred record (a ``derived/`` record): never a ground for a run claim."""


def _strict(parse: Callable[[JsonValue], _T], record: Mapping[str, object]) -> _T:
    """A compiler reader over one record; whatever it refuses is malformed here."""
    provenance = record.get("provenance")
    if isinstance(provenance, dict) and provenance.get("assertion_kind") == "inferred":
        raise Inferred(f"an inferred {record.get('kind')!r} record is a derived/ record")
    try:
        return parse(dict(record))  # type: ignore[arg-type]
    except (ValueError, TypeError, KeyError, RecursionError) as exc:
        raise Malformed(str(exc) or type(exc).__name__) from exc


def _ids(knowledge: Knowledge[LogicalId]) -> Iterable[LogicalId]:
    if isinstance(knowledge, Known):
        return (knowledge.value,)
    if isinstance(knowledge, Ambiguous):
        return tuple(c.value for c in knowledge.candidates)
    return ()


def _declared_ids(*fields: Knowledge[LogicalId]) -> None:
    """Every id a field states is a declared value (ADR 0006 §9): never blank or padded."""
    for field in fields:
        for value in _ids(field):
            declared(value)


def run(record: Mapping[str, object]) -> Run:
    """The compiler's ``Run``; a declared id or machine that is blank or padded is malformed."""
    parsed = _strict(run_from_json, record)
    _declared_ids(parsed.logical_id, parsed.machine)
    return parsed


def assembly(record: Mapping[str, object]) -> RunAssembly:
    return _strict(run_assembly_from_json, record)


def revision(record: Mapping[str, object]) -> SourceRevision:
    return _strict(source_revision_from_json, record)


def mapping(record: Mapping[str, object]) -> ClockMapping:
    return _strict(clock_mapping_from_json, record)


def site(record: Mapping[str, object]) -> Site:
    parsed = _strict(site_from_json, record)
    _declared_ids(*parsed.identifiers)
    return parsed


@dataclass(frozen=True)
class Declaration:
    """What a manifest entry says a run involved (the ``run_declaration`` stand-in)."""

    record: RecordId
    run: LogicalId
    machine: Knowledge[LogicalId]
    site: Knowledge[LogicalId]
    task: Knowledge[LogicalId]
    evidence: tuple[EvidenceRef, ...]


_DECLARATION_KEYS: Final = frozenset({"evidence", "id", "kind", "machine", "run", "site", "task"})


def _logical_id(data: JsonValue) -> LogicalId:
    return declared(logical_id_from_json(data))


def _knowledge(record: Mapping[str, object], name: str) -> Knowledge[LogicalId]:
    try:
        value = from_json(record[name], _logical_id, provenance_from_json)  # type: ignore[arg-type]
    except Malformed:
        raise
    except (ValueError, TypeError, KeyError, RecursionError) as exc:
        raise Malformed(f"{name!r}: {exc}") from exc
    return value


def declaration(record: Mapping[str, object]) -> Declaration:
    keys = set(record)
    if keys != _DECLARATION_KEYS:
        missing, extra = sorted(_DECLARATION_KEYS - keys), sorted(keys - _DECLARATION_KEYS)
        raise Malformed(f"run_declaration keys: missing {missing}, unexpected {extra}")
    evidence = record["evidence"]
    if not isinstance(evidence, (list, tuple)) or not evidence:
        raise Malformed("'evidence' must be a non-empty list of evidence refs")
    try:
        rid = parse_record_id(record["id"])  # type: ignore[arg-type]
        named = _logical_id(record["run"])  # type: ignore[arg-type]
        refs = tuple(evidence_ref_from_json(item) for item in evidence)
    except Malformed:
        raise
    except (ValueError, TypeError, KeyError, RecursionError) as exc:
        raise Malformed(str(exc) or type(exc).__name__) from exc
    return Declaration(
        record=rid,
        run=named,
        machine=_knowledge(record, "machine"),
        site=_knowledge(record, "site"),
        task=_knowledge(record, "task"),
        evidence=refs,
    )
