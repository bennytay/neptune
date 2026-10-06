"""A stub Ledger that answers ``threads_of`` as the catalog does (Ledger ADR 0003 §2, ADR 0018).

``catalog(packages)`` holds every record and answers, for each, the subject memberships the
Ledger's thread table gives the kinds Memory's configuration and calibration consolidators read:
a ``run`` opens a run thread (declared when its ``logical_id`` is ``Known``, else anchored on its
record-level evidence); a ``hardware_configuration``, ``software_configuration``, ``calibration``
or ``configuration_snapshot`` (Ledger ADR 0017) opens an anchored configuration thread, when its
evidence source is a content id. Every other kind is held in no thread, lifecycle records
included, as the real catalog holds them (the acceptance snapshot's generator asks a real one).
Thread ids are computed as the Ledger computes them: sha256 of the key's canonical JSON.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Final

from neptune.identity import canonical_json
from neptune.model.ids import LogicalId, logical_id_from_json
from neptune.model.provenance import evidence_ref_from_json
from neptune_memory.ledger import Membership, StubLedger, ThreadKey, ThreadsOf

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

Record = dict[str, object]
CONFIGURATION_KINDS: Final = frozenset(
    {"calibration", "configuration_snapshot", "hardware_configuration", "software_configuration"}
)


def package_id(name: str) -> str:
    """A content id for a test package (the catalog's package ids are manifest digests)."""
    return "sha256:" + hashlib.sha256(name.encode()).hexdigest()


def key_json(key: ThreadKey) -> dict[str, object]:
    inner = key.declared.to_json() if key.declared is not None else key.anchor.to_json()  # type: ignore[union-attr]
    return {"key": inner, "kind": key.kind}


def thread_id(key: ThreadKey) -> str:
    return "sha256:" + hashlib.sha256(canonical_json.dumps(key_json(key))).hexdigest()  # type: ignore[arg-type]


def anchored(kind: str, record: Record) -> ThreadKey:
    provenance = record["provenance"]
    return ThreadKey(kind, None, evidence_ref_from_json(provenance["evidence"]))  # type: ignore[index]


def declared(kind: str, node: LogicalId) -> ThreadKey:
    return ThreadKey(kind, node, None)


def subject(key: ThreadKey, package: str) -> Membership:
    return Membership(thread_id(key), key, package, ("subject",))


def _subject_keys(record: Record) -> list[ThreadKey]:
    kind = record.get("kind")
    if kind == "run":
        logical = record.get("logical_id")
        if isinstance(logical, dict) and logical.get("knowledge") == "known":
            return [declared("run", logical_id_from_json(logical["value"]))]
        return [anchored("run", record)]
    if kind in CONFIGURATION_KINDS:
        provenance = record["provenance"]
        if not isinstance(provenance["evidence"]["source"], str):  # type: ignore[index]
            return []  # the Ledger anchors a thread only on a content id
        return [anchored("configuration", record)]
    return []


def answers(packages: Mapping[str, Sequence[Record]]) -> dict[str, ThreadsOf]:
    """``threads_of`` for every record id the packages hold, as the catalog would answer."""
    found: dict[str, list[Membership]] = {}
    for package in sorted(packages):
        for record in packages[package]:
            rid = record.get("id")
            if isinstance(rid, str):
                listed = found.setdefault(rid, [])
                listed.extend(subject(key, package) for key in _subject_keys(record))
    return {
        rid: ThreadsOf(
            rid,
            "found",
            tuple(sorted(set(memberships), key=lambda m: (m.thread_id, m.package_id))),
            (),
        )
        for rid, memberships in found.items()
    }


def catalog(packages: Mapping[str, Sequence[Record]], *, without: Sequence[str] = ()) -> StubLedger:
    """A Ledger holding ``packages`` that answers ``threads_of``; record ids in ``without`` are
    answered ``unknown_record`` (a record the catalog does not hold, as a hostile export may)."""
    held = answers(packages)
    for rid in without:
        held.pop(rid, None)
    return StubLedger({pid: (1, list(records)) for pid, records in packages.items()}, "1.7.0", held)
