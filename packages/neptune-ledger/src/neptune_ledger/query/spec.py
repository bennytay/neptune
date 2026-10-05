"""Whether a ``QuerySpec`` is inside the contract (Ledger ADR 0016 §1).

A spec outside it is refused with ``invalid_request`` findings, never an exception and never a
guess. Each finding names the field at fault. Every check here needs no catalog read.
"""

import math
from functools import cache
from typing import Final

from neptune.model.frames import FrameRef
from neptune.model.ids import parse_record_id
from neptune.model.spatial import CrsCode
from neptune.model.units import unit_from_json
from neptune_ledger.api import codec
from neptune_ledger.api.types import (
    CatalogFinding,
    CrsReference,
    FrameReference,
    FrameWindow,
    QuerySpec,
)
from neptune_ledger.catalog.projection import shipped_registry
from neptune_ledger.query.budget import QueryLimits, over_ceiling

MAX_SEQ: Final = 2**63 - 1


@cache
def declared_kinds() -> frozenset[str]:
    """Every record kind of every package-schema version the Ledger reads (ADR 0011)."""
    return frozenset(kind for entry in shipped_registry().versions for kind in entry.spec.kinds)


def _bad(subject: str, detail: str) -> CatalogFinding:
    return CatalogFinding("invalid_request", subject, detail)


def problems(spec: object, limits: QueryLimits) -> tuple[CatalogFinding, ...]:
    """Why ``spec`` is refused, or nothing when it is inside the contract."""
    if not isinstance(spec, QuerySpec):
        return (_bad("spec", f"not a QuerySpec: {type(spec).__name__}"),)
    try:
        codec.to_json(spec)
    except (codec.CodecError, TypeError, ValueError) as exc:
        return (_bad("spec", str(exc).splitlines()[0][:300]),)
    found: list[CatalogFinding] = []
    known = declared_kinds()
    for kind in spec.kinds:
        if kind not in known:
            detail = "no package-schema version the Ledger reads declares this record kind"
            found.append(_bad(kind, detail))
    if spec.as_of is not None and spec.as_of > MAX_SEQ:
        found.append(_bad("as_of", "as_of is a tx_seq, an int64"))
    if spec.window is not None and spec.window.first > spec.window.last:
        found.append(_bad("window", "first is after last"))
    if spec.frame is not None:
        found += _frame_problems(spec.frame)
    found += [_bad("budget", text) for text in over_ceiling(spec.budget, limits)]
    if spec.series is not None:
        if spec.window is None:
            detail = "a series join reads the series rows in a window: name one (ADR 0016 §4)"
            found.append(_bad("series", detail))
        if spec.kinds != ("stream",):
            found.append(_bad("series", "a series join reads streams: kinds is exactly stream"))
        if spec.after is not None or spec.limit is not None:
            detail = "a series join is bounded by its budget, not paged: no after or limit"
            found.append(_bad("series", detail))
    return tuple(found)


def _frame_problems(frame: FrameWindow) -> list[CatalogFinding]:
    found: list[CatalogFinding] = []
    try:
        match frame.reference:
            case FrameReference(frame_graph_id=graph, frame_id=frame_id):
                FrameRef(frame_id, parse_record_id(graph))
            case CrsReference(authority=authority, code=code):
                CrsCode(authority, code)
    except (TypeError, ValueError) as exc:
        detail = f"not a declared frame or CRS: {str(exc).splitlines()[0][:200]}"
        found.append(_bad("frame.reference", detail))
    try:
        unit_from_json(frame.unit)
    except (TypeError, ValueError):
        detail = "a unit is a canonical unit symbol (m, mm, deg); coordinates are never converted"
        found.append(_bad("frame.unit", detail))
    low, high = frame.low, frame.high
    if len(low) != len(high) or not all(
        math.isfinite(a) and math.isfinite(b) and a <= b for a, b in zip(low, high, strict=False)
    ):
        detail = "a box is 2 or 3 finite low and high coordinates, low <= high on every axis"
        found.append(_bad("frame.box", detail))
    return found
