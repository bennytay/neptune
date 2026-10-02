"""One declared entry (a table row, a manifest entry) as a typed record (ADR 0063 §2 to §5).

A table row and a configuration mapping are read into the same shape, ``Entry``: each field by
its key (``field_key``), with the text it declares and where. ``build`` then makes the record its
kind calls for. Keys are matched against short fixed lists; a key that is not on them stays in
the row or the configuration, cited there, and is never guessed at.
"""

import math
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from neptune.context._emit import Output, as_id, identifiers, names, split_items, sub_span
from neptune.model._fields import Identifiers
from neptune.model.finding import FindingCategory
from neptune.model.ids import LogicalId, RecordId, check_token
from neptune.model.knowledge import Knowledge, Known, KnownAbsent, Unknown
from neptune.model.provenance import EvidenceRef
from neptune.model.spatial import CrsCode, GeodeticPosition
from neptune.model.task import Requirement, TaskBrief, WorkOrder
from neptune.model.world import Asset, Site

# The kinds a register or manifest section declares, by the key naming its rows' ids: the first
# of these a table's columns hold names its kind; the others are references (ADR 0063 §2).
KIND_KEYS: Final = (
    ("requirement_id", "requirement"),
    ("work_order_id", "work_order"),
    ("task_id", "task"),  # a task register lists the assets it involves, not the reverse
    ("asset_id", "asset"),
    ("site_id", "site"),
)
# Keys naming another thing's id: references, never an identifier of the entry itself.
_REFERENCES: Final = frozenset(
    {
        "site_id",
        "asset_id",
        "asset_ids",
        "parent_id",
        "task_id",
        "requirement_id",
        "work_order_id",
        "procedure_id",
        "machine_id",
        "machine_ids",
        "robot_id",
        "robot_ids",
    }
)
_NAME: Final = ("name", "title")
_ALIASES: Final = ("aliases", "alias")
_CATEGORY: Final = ("category", "type")
_SITE: Final = ("site_id", "site")
_PARENT: Final = ("parent_id", "parent")
_TASK: Final = ("task_id", "task")
_ASSETS: Final = ("asset_ids", "assets", "asset_id", "asset")
_MACHINES: Final = (
    "machine_ids",
    "machines",
    "machine_id",
    "machine",
    "robot_ids",
    "robots",
    "robot_id",
    "robot",
)
_TEXT: Final = ("text", "statement", "requirement", "description")
_LATITUDE: Final = ("latitude", "lat")
_LONGITUDE: Final = ("longitude", "lon", "lng")
_HEIGHT: Final = ("altitude", "height", "elevation")
_SERIAL: Final = ("serial", "serial_number")
_EXTERNAL: Final = ("external_id",)
# A coordinate is a decimal number as written: no exponent, no thousands separator, no unit.
_DECIMAL: Final = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)")


@dataclass(frozen=True)
class Value:
    """One declared value: its text (``None`` when blank, null or not text), where it is, and
    for a configuration's sequence its items or for a number the format's own reading."""

    evidence: EvidenceRef
    text: str | None = None
    absent: bool = False  # the format defines it as "none" (JSON or YAML null)
    not_text: bool = False  # a boolean or a collection where text was expected
    items: tuple["Value", ...] | None = None
    number: float | None = None


Entry = Mapping[str, Value]
# What a field the entry's shape has no place for is: ``NotCovered`` for a table without the
# column, ``Unknown`` for a manifest entry without the key.
Missing = Callable[[Output, EvidenceRef], Knowledge[Any]]


def _first(entry: Entry, keys: Sequence[str]) -> Value | None:
    for key in keys:
        if key in entry:
            return entry[key]
    return None


class _Reader:
    def __init__(
        self, out: Output, entry: Entry, evidence: EvidenceRef, missing: Missing, kind: str
    ) -> None:
        self.out, self.entry, self.evidence, self.missing, self.kind = (
            out,
            entry,
            evidence,
            missing,
            kind,
        )

    def text(self, keys: Sequence[str]) -> Knowledge[str]:
        value = _first(self.entry, keys)
        if value is None:
            return self.missing(self.out, self.evidence)
        if value.absent:
            return self.out.absent(value.evidence)
        if value.text is None:
            if value.not_text:
                self.out.finding(
                    "value_not_text",
                    FindingCategory.UNSUPPORTED,
                    value.evidence,
                    f"the {self.kind}'s {keys[0]} is not text; it is left unknown",
                    {"field": keys[0]},
                )
            return self.out.unknown(value.evidence)
        return self.out.known(value.text, value.evidence)

    def ref(self, keys: Sequence[str], namespace: str) -> Knowledge[LogicalId]:
        return as_id(self.text(keys), namespace)

    def items(self, keys: Sequence[str]) -> list[Known[str]]:
        """Every item of a list value: a sequence's items, or one text split at ``;`` and ``,``."""
        value = _first(self.entry, keys)
        if value is None:
            return []
        values = value.items if value.items is not None else (value,)
        found: list[Known[str]] = []
        for item in values:
            if item.text is None:
                continue
            if value.items is not None:  # a sequence item is one value, never split
                found.append(self.out.known(item.text, item.evidence))
                continue
            for start, end in split_items(item.text):
                cited = sub_span(item.evidence, start, end)
                found.append(self.out.known(item.text[start:end], cited))
        return found

    def refs(self, keys: Sequence[str], namespace: str) -> Identifiers:
        return identifiers(
            Known(LogicalId(namespace, item.value), item.provenance) for item in self.items(keys)
        )

    def identifiers(self, id_keys: Sequence[str], namespace: str) -> Identifiers:
        """The entry's own id under ``namespace``, its serial and external ids, and any other
        ``<scheme>_id`` key as an id in that scheme (``cmms_id`` → ``("cmms", …)``)."""
        found: list[Known[LogicalId]] = []
        own = self.text(id_keys)
        if isinstance(own, Known):
            found.append(own.map(lambda text: LogicalId(namespace, text)))
        schemes = [(key, "serial") for key in _SERIAL] + [(key, "external") for key in _EXTERNAL]
        for key in sorted(self.entry):
            other = key.endswith("_id") and key not in _REFERENCES and key not in id_keys
            if other and key not in _EXTERNAL and _scheme(key[:-3]):
                schemes.append((key, key[:-3]))
        for key, scheme in schemes:
            value = self.entry.get(key)
            if value is not None and value.text is not None:
                found.append(self.out.known(LogicalId(scheme, value.text), value.evidence))
        return identifiers(found)

    def coordinate(self, keys: Sequence[str]) -> float | Unknown | None:
        """A coordinate: ``None`` when the shape has no place for it, ``Unknown`` when blank or
        not a decimal number (a finding says which)."""
        value = _first(self.entry, keys)
        if value is None:
            return None
        number = value.number
        if number is None and value.text is not None and _DECIMAL.fullmatch(value.text.strip()):
            number = float(value.text.strip())
        if number is not None and math.isfinite(number):
            return number  # a decimal too long for a double reads as infinite: not usable
        if value.text is not None or value.not_text or number is not None:
            self.out.finding(
                "coordinate_not_decimal",
                FindingCategory.UNREPRESENTABLE,
                value.evidence,
                f"the {self.kind}'s {keys[0]} is not a decimal number; its location is unknown",
                {"field": keys[0]},
            )
        return self.out.unknown(value.evidence)

    def location(self) -> Knowledge[GeodeticPosition]:
        """A position from a latitude and a longitude, citing the entry (ADR 0020 §1). The CRS is
        the entry's ``crs`` (``EPSG:4326``) or unknown; units and height reference are never
        assumed."""
        latitude, longitude = self.coordinate(_LATITUDE), self.coordinate(_LONGITUDE)
        if latitude is None and longitude is None:
            return self.missing(self.out, self.evidence)
        if not isinstance(latitude, float) or not isinstance(longitude, float):
            return self.out.unknown(self.evidence)
        height = self.coordinate(_HEIGHT)
        crs_text = self.text(("crs",))
        crs: Knowledge[CrsCode] = crs_text  # type: ignore[assignment]  # every state but Known
        if isinstance(crs_text, Known):
            authority, colon, code = crs_text.value.partition(":")
            crs = Unknown(crs_text.provenance)
            if colon and authority.strip() and code.strip():
                try:
                    crs = crs_text.map(lambda _: CrsCode(authority.strip(), code.strip()))
                except ValueError:  # longer than any registry's code: not a CRS code
                    self.out.finding(
                        "crs_not_a_code",
                        FindingCategory.UNREPRESENTABLE,
                        self.evidence,
                        f"the {self.kind}'s crs is not an authority:code pair; it is unknown",
                    )
        return self.out.known(
            GeodeticPosition(
                latitude=latitude,
                longitude=longitude,
                height=(
                    self.out.known(height, self.evidence)
                    if isinstance(height, float)
                    else height
                    if height is not None
                    else self.missing(self.out, self.evidence)
                ),
                crs=crs,
                angle_unit=self.out.unknown(self.evidence),
                height_unit=self.out.unknown(self.evidence),
                height_reference=self.out.unknown(self.evidence),
            ),
            self.evidence,
        )


def _scheme(text: str) -> bool:
    try:
        check_token("namespace", text)
    except ValueError:
        return False
    return True


def build(
    out: Output,
    kind: str,
    entry: Entry,
    evidence: EvidenceRef,
    declared_in: RecordId,
    missing: Missing,
    id_keys: Sequence[str],
    *,
    site: Knowledge[LogicalId] | None = None,
    task: Knowledge[LogicalId] | None = None,
) -> Any | None:
    """The record of ``kind`` that ``entry`` declares, or ``None`` (and a finding) when it names
    nothing. ``site`` and ``task`` are what an enclosing declaration states (a site's nested
    ``assets``, a task's nested ``requirements``) when the entry itself states none."""
    read = _Reader(out, entry, evidence, missing, kind)
    namespace = kind
    ids = read.identifiers(id_keys, namespace)
    name = read.text(_TEXT if kind == "requirement" else _NAME)
    if not ids and not isinstance(name, Known):
        out.finding(
            "unnamed_declaration",
            FindingCategory.MISSING,
            evidence,
            f"this {kind} entry states neither an id nor a name; no record is made",
            {"kind": kind},
        )
        return None
    own_site = read.ref(_SITE, "site")
    if site is not None and not isinstance(own_site, Known | KnownAbsent):
        own_site = site
    own_task = read.ref(_TASK, "task")
    if task is not None and not isinstance(own_task, Known | KnownAbsent):
        own_task = task
    record_id = out.record_id(_KIND_NAMES[kind], evidence)
    prov = out.prov(evidence)
    if kind == "site":
        return Site(
            record_id,
            prov,
            ids,
            name,
            names(read.items(_ALIASES)),
            read.ref(_PARENT, "site"),
            read.location(),
        )
    if kind == "asset":
        return Asset(
            record_id,
            prov,
            ids,
            name,
            names(read.items(_ALIASES)),
            read.text(_CATEGORY),
            own_site,
            read.ref(_PARENT, "asset"),
            read.location(),
        )
    if kind == "task":
        return TaskBrief(
            record_id,
            prov,
            declared_in,
            ids,
            name,
            read.text(("objective",)),
            own_site,
            read.refs(_ASSETS, "asset"),
            read.refs(_MACHINES, "machine"),
        )
    if kind == "requirement":
        return Requirement(record_id, prov, declared_in, ids, name, own_task)
    return WorkOrder(
        record_id,
        prov,
        declared_in,
        ids,
        name,
        read.text(("status",)),
        own_site,
        read.refs(_ASSETS, "asset"),
        own_task,
    )


# The record kind each declared kind is written as.
_KIND_NAMES: Final = {
    "site": Site.kind,
    "asset": Asset.kind,
    "task": TaskBrief.kind,
    "requirement": Requirement.kind,
    "work_order": WorkOrder.kind,
}
