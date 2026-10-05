"""The provenance appendix: every evidence ref and record a pack's claims cite, and how to resolve
each one through the Ledger (ADR 0013 §7).

Requests are documents of the Ledger's catalog API (``contracts/catalog-api``, the version in
``CATALOG_API_VERSION``): ``ResolveRequest`` for an evidence anchor, ``ThreadsOfRequest`` and
``LineageRequest`` for a record. Each is pinned ``as_of`` the snapshot's head, the Ledger
transaction the graph was resolved at (graph-schema ``LedgerTx`` is the Ledger's commit sequence
number), so the answer is the one Memory read. A ref the catalog cannot resolve says why instead:
an external object is fetched through its connector at its revision, and a ref with no locator
step names a whole source, which ``resolve`` does not take.
"""

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Final

from neptune.identity import canonical_json
from neptune.model.jsonvalue import JsonObject, JsonValue
from neptune_deploy.packs.snapshot import Claim

CATALOG_API: Final = "catalog-api"
CATALOG_API_VERSION: Final = "1.7.0"


@dataclass(frozen=True)
class Appendix:
    evidence: tuple[JsonObject, ...]
    records: tuple[JsonObject, ...]

    def to_json(self) -> JsonObject:
        return {"evidence": list(self.evidence), "records": list(self.records)}


def _as_of(head: int) -> dict[str, JsonValue]:
    # The catalog's TxSeq starts at 1; a graph at head 0 read no transaction to pin.
    return {"as_of": head} if head >= 1 else {}


def _ledger(calls: Iterable[tuple[str, JsonObject]]) -> JsonObject:
    return {
        "api": CATALOG_API,
        "api_version": CATALOG_API_VERSION,
        "calls": [{"call": call, "request": request} for call, request in calls],
        "via": "ledger",
    }


def resolution(ref: JsonObject, head: int) -> JsonObject:
    """How to resolve one evidence ref."""
    source = ref["source"]
    if isinstance(source, Mapping):
        return {
            "connector_id": source["connector_id"],
            "object_id": source["object_id"],
            "reason": "an external object: fetch it through its connector at this revision; the"
            " catalog resolves content ids",
            "revision_token": source["revision_token"],
            "via": "connector",
        }
    locator = ref["locator"]
    if isinstance(locator, list | tuple) and not locator:
        return {
            "reason": "a whole-source ref (no locator step): the catalog's resolve takes an"
            " anchor with at least one step; fetch the source by its content id",
            "source": source,
            "via": "none",
        }
    return _ledger([("resolve", {**_as_of(head), "evidence_ref": ref})])


def build_appendix(claims: Iterable[Claim], head: int) -> Appendix:
    refs: dict[bytes, tuple[JsonObject, set[str]]] = {}
    records: dict[str, set[str]] = defaultdict(set)
    for claim in claims:
        for ref in claim.evidence:
            key = canonical_json.dumps(ref)
            refs.setdefault(key, (ref, set()))[1].add(claim.id)
        for record in claim.records:
            records[record].add(claim.id)
        if claim.object_record is not None:
            records[claim.object_record].add(claim.id)
    evidence: tuple[JsonObject, ...] = tuple(
        {"claims": sorted(ids), "ref": ref, "resolve": resolution(ref, head)}
        for _key, (ref, ids) in sorted(refs.items())
    )
    record_entries: tuple[JsonObject, ...] = tuple(
        {
            "claims": sorted(records[record]),
            "record_id": record,
            "resolve": _ledger(
                [
                    ("threads_of", {**_as_of(head), "record_id": record}),
                    ("lineage", {**_as_of(head), "record_id": record}),
                ]
            ),
        }
        for record in sorted(records)
    )
    return Appendix(evidence, record_entries)
