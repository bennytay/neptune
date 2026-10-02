"""The alignment records a thread reads: identity links and clock mappings (ADR 0010 §8, §9).

Package schema 3 (root ADR 0050) adds ``identity_link`` and ``clock_mapping``. Registration reads
each from its verified record line with the compiler's own reader and writes what a thread needs
into the derived index, in the registration transaction, like membership:

- an ``IdentityLink`` becomes one row per id its ``right`` side states (one when ``Known``, one per
  candidate when ``Ambiguous``). A thread keyed by either id lists the link as a ``ThreadLink``
  edge; no record, thread or key is ever joined through it (ADR 0003 §1.5).
- a ``ClockMapping`` becomes the merge's reading of it (``merge.ClockMapping``) as canonical JSON:
  ``slope = rate``, ``offset = anchor.target - rate * anchor.source``, the residual bound, and the
  validity window on the source clock, half-open as ADR 0050 §3 states it. A mapping the merge
  cannot use keeps the reason it cannot.

Both are pure functions of the record and this Ledger version.
"""

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Final, cast

from neptune.identity import canonical_json
from neptune.model.alignment import ClockMapping as ClockMappingRecord
from neptune.model.alignment import IdentityLink, ValidityWindow
from neptune.model.kinds import RECORD_KINDS
from neptune.model.knowledge import Ambiguous, Knowledge, Known, KnownAbsent, NotApplicable
from neptune_ledger.api import codec
from neptune_ledger.api.types import DeclaredKey, ThreadKey, ThreadKind
from neptune_ledger.threads.merge import ClockMapping

# The thread kinds keyed by a LogicalId, whose threads list identity links (ADR 0003 §2's live
# declared kinds; software_version keys are version tokens, never logical ids, and the reserved
# zone, task and person kinds have no members and no links until an ADR makes them live).
LINKED_KINDS: Final = frozenset({"asset", "machine", "run", "sensor", "site"})


class AlignmentError(ValueError):
    """An alignment record states a key the catalog API cannot express (ADR 0010 §1)."""

    def __init__(self, record_id: str, pointer: str, cause: Exception) -> None:
        self.record_id = record_id
        self.pointer = pointer
        self.cause = cause
        super().__init__(f"{record_id} {pointer}: {cause}")


@dataclass(frozen=True)
class LinkRow:
    """One id an identity link's right side states: ``thread_identity_link`` without tenant,
    package and registration key. ``left`` and ``right`` are canonical JSON ``DeclaredKey``s."""

    record_id: str
    left: str
    right: str
    state: str
    assertion_kind: str


@dataclass(frozen=True)
class MappingRow:
    """A clock mapping as the merge reads it: ``thread_clock_mapping`` without tenant, package
    and registration key. ``mapping`` is the canonical JSON ``mapping_json`` writes."""

    record_id: str
    source: str
    target: str
    mapping: str


def link_rows(records: Iterator[Any]) -> tuple[LinkRow, ...]:
    """The link rows of a package's ``identity_link`` records (JSON, any order), sorted."""
    out: set[LinkRow] = set()
    for data in records:
        link = cast("IdentityLink", _read("identity_link", data))
        left = _declared(link.id, "/left", link.left.namespace, link.left.value)
        if isinstance(link.right, Known):
            stated = [("/right", link.right.value)]
            state = "known"
        else:
            assert isinstance(link.right, Ambiguous)  # the model admits nothing else
            candidates = enumerate(link.right.candidates)
            stated = [(f"/right/candidates/{i}", c.value) for i, c in candidates]
            state = "ambiguous"
        for pointer, right in stated:
            text = _declared(link.id, pointer, right.namespace, right.value)
            out.add(LinkRow(link.id, left, text, state, str(link.provenance.assertion_kind)))
    return tuple(sorted(out, key=lambda r: (r.record_id, r.right)))


def mapping_rows(records: Iterator[Any]) -> tuple[MappingRow, ...]:
    """The mapping rows of a package's ``clock_mapping`` records (JSON, any order), sorted."""
    out = []
    for data in records:
        mapping = cast("ClockMappingRecord", _read("clock_mapping", data))
        text = canonical_json.dumps(mapping_json(mapping)).decode("utf-8")
        out.append(MappingRow(mapping.id, mapping.source, mapping.target, text))
    return tuple(sorted(out, key=lambda r: r.record_id))


def mapping_json(mapping: ClockMappingRecord) -> dict[str, Any]:
    """What the merge reads from a ``ClockMapping`` record (ADR 0010 §9), as JSON.

    Fractions are ``[numerator, denominator]`` in lowest terms. ``unsupported``, present only
    when the merge cannot use the mapping, says why. ``window`` is the validity window in source
    ticks, ``start`` inclusive and ``end`` exclusive, each absent on an open side; ``window`` is
    absent when the validity the record states cannot be checked, so no entry can use it.
    Canonical JSON has no null (root ADR 0004), so absence is a missing key.
    """
    slope = offset = bound = Fraction(0)
    reasons = []
    anchor, rate, residual = mapping.anchor, mapping.rate, mapping.residual_bound
    if isinstance(anchor, Known) and isinstance(rate, Known):
        slope = rate.value
        offset = anchor.value.target.ticks - slope * anchor.value.source.ticks
    for name, field in (("anchor", anchor), ("rate", rate)):
        if not isinstance(field, Known):
            reasons.append(f"{name} is {_state(field)}")
    if isinstance(residual, Known):
        bound = Fraction(residual.value.ticks)
    elif not isinstance(residual, KnownAbsent):
        # ADR 0003 §3.1's "0 if it declares none" is a stated absence; an unstated bound is not 0.
        reasons.append(f"residual_bound is {_state(residual)}")
    out: dict[str, Any] = {
        "bound": _fraction(bound),
        "offset": _fraction(offset),
        "slope": _fraction(slope),
    }
    if reasons:
        out["unsupported"] = "; ".join(reasons)
    window = _window(mapping.validity)
    if window is not None:
        out["window"] = window
    return out


def clock_mapping(record_id: str, source: str, target: str, text: str) -> ClockMapping:
    """The merge's ``ClockMapping`` from a stored ``thread_clock_mapping`` row."""
    data = canonical_json.loads(text.encode("utf-8"))
    assert isinstance(data, dict)
    window = data.get("window")
    return ClockMapping(
        mapping_id=record_id,
        source=source,
        target=target,
        slope=Fraction(*data["slope"]),
        offset=Fraction(*data["offset"]),
        bound=Fraction(*data["bound"]),
        window=None if window is None else (window.get("start"), window.get("end")),
        unsupported=data.get("unsupported"),
    )


def link_key(kind: str, text: str) -> ThreadKey:
    """The thread key of ``kind`` for a stored canonical JSON ``DeclaredKey``."""
    data = canonical_json.loads(text.encode("utf-8"))
    assert isinstance(data, Mapping)
    declared = DeclaredKey(str(data["namespace"]), str(data["value"]))
    return ThreadKey(cast("ThreadKind", kind), declared)


def declared_json(key: DeclaredKey) -> str:
    return canonical_json.dumps({"namespace": key.namespace, "value": key.value}).decode("utf-8")


def _declared(record_id: str, pointer: str, namespace: str, value: str) -> str:
    """A link's id as canonical JSON, once the catalog API accepts it as a thread key."""
    key = DeclaredKey(namespace, value)
    try:
        codec.to_json(ThreadKey("machine", key))
    except (ValueError, TypeError) as exc:
        raise AlignmentError(record_id, pointer, exc) from exc
    return declared_json(key)


def _read(kind: str, data: Any) -> object:
    _, read = RECORD_KINDS[kind]
    return read(data)


def _window(validity: Knowledge[ValidityWindow]) -> dict[str, int] | None:
    """The source-clock window, half-open, an open side absent; None when it cannot be checked."""
    if isinstance(validity, NotApplicable):  # a timeless relation holds at every instant
        return {}
    if not isinstance(validity, Known):
        return None
    sides: dict[str, int] = {}
    for name in ("start", "end"):
        bound = getattr(validity.value, name)
        if isinstance(bound, Known):
            sides[name] = bound.value.ticks
        elif not isinstance(bound, KnownAbsent):  # Unknown or NotCovered: cannot be checked
            return None
    return sides


def _fraction(value: Fraction) -> list[int]:
    return [value.numerator, value.denominator]


def _state(field: Knowledge[Any]) -> str:
    return str(getattr(field, "state", type(field).__name__))
