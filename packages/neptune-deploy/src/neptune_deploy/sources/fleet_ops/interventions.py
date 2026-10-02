"""Formant interventions as the compiler's ``Intervention`` lifecycle records (ADR 0010 §4).

An intervention request is what a person or a system states about a remote assist or an on-site
action on one device: its type, its reason, the commands issued, when it began and ended, its
outcome. Each becomes one ``Intervention`` whose provenance is the item it came from and whose
every value cites its own key, ``stated``, exactly as written:

- ``mode`` is ``interventionType``, ``authority`` is ``authority``, ``reason`` is ``message``,
  ``outcome`` is ``outcome``; ``commands`` is the strings of ``commands`` in order.
- ``identifiers`` is ``id`` (namespace ``formant.intervention``); ``machines`` is ``deviceId``
  (``formant.device``). A device id is Formant's own: it is never matched to another system's name
  for the robot.
- ``start`` and ``end`` are ``time`` and ``endTime``, text read by a declared format (root ADR 0023
  §2): an offset names an instant, and none is added where the text states none. Text no declared
  format reads is ``Unknown`` with a finding; the text stays in the table.
- ``site``, ``configuration`` and ``related`` are not in the API's answer: ``site`` and
  ``configuration`` are ``NotCovered``, ``related`` is empty. Nothing links an intervention to a
  task, an incident or a time window another system states. That is not this module's to infer.

An item with no usable ``id`` builds no record (it stays in the table, with a finding).
"""

import re
from collections.abc import Callable
from typing import Any, Final

from neptune.identity import canonical_json
from neptune.identity.provenance import evidence_record_id
from neptune.model.ids import LogicalId
from neptune.model.jsonvalue import JsonValue
from neptune.model.knowledge import AssertionKind, Knowledge, Known, NotCovered, Unknown
from neptune.model.lifecycle import Intervention
from neptune.model.provenance import Provenance, TransformRecord
from neptune.model.reference import TimestampDomain
from neptune.model.time import ClockRole, Epoch, Timescale, Timestamp
from neptune_deploy.lifecycle.times import read_time
from neptune_deploy.sources.fleet_ops.documents import Document, cite, stated

ID_PATTERN: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:\-]{0,255}")
MAX_COMMANDS: Final = 1000
DEFAULT_TIME_FORMATS: Final = ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z")
ID_NAMESPACE: Final = "formant.intervention"
DEVICE_NAMESPACE: Final = "formant.device"

Report = Callable[[str, Any, dict[str, JsonValue]], None]


def _text(value: JsonValue) -> str | None:
    """A stated value as text: text as is, a typed scalar as its canonical JSON text."""
    if isinstance(value, str):
        text = value
    elif isinstance(value, bool | int) or (isinstance(value, float) and value == value):
        try:
            text = canonical_json.dumps(value).decode("utf-8")
        except ValueError:
            return None
    else:
        return None
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return None
    return text or None


class _Item:
    """One intervention item and the ways to read its keys."""

    def __init__(
        self,
        document: Document,
        index: int,
        transform: TransformRecord,
        formats: tuple[str, ...],
        domains: dict[str, TimestampDomain],
        covered: frozenset[str],
        report: Report,
    ) -> None:
        self.document, self.index, self.transform = document, index, transform
        self.item = document.items[index]
        self.formats, self.domains, self.covered, self.report = formats, domains, covered, report

    def absent(self, key: str) -> Knowledge[Any]:
        """A key this item lacks: ``NotCovered`` when no item of the document has it (the system
        did not give this field at all), else ``Unknown`` (it could have, and did not)."""
        return Unknown(self.prov()) if key in self.covered else NotCovered()

    def prov(self, *key: str | int) -> Provenance:
        return stated(self.document, self.transform, "items", self.index, *key)

    def _unreadable(self, key: str, why: str) -> None:
        self.report(
            "value_unreadable",
            cite(self.document, "items", self.index, key),
            {"field": key, "reason": why},
        )

    def text(self, key: str) -> Knowledge[str]:
        if key not in self.item:
            return self.absent(key)
        value = self.item[key]
        if value is None or value == "":
            return Unknown(self.prov(key))
        text = _text(value)
        if text is None:
            self._unreadable(key, "not_text")
            return Unknown(self.prov(key))
        return Known(text, self.prov(key))

    def ident(self, key: str, namespace: str) -> tuple[Knowledge[LogicalId], ...]:
        value = self.item.get(key)
        if isinstance(value, str) and ID_PATTERN.fullmatch(value):
            return (Known(LogicalId(namespace, value), self.prov(key)),)
        return ()

    def time(self, key: str) -> Knowledge[Timestamp]:
        if key not in self.item:
            return self.absent(key)
        value = self.item[key]
        if value is None or value == "":
            return Unknown(self.prov(key))
        reading = read_time(value, self.formats) if isinstance(value, str) else None
        if reading is None:
            self._unreadable(key, "time_format")
            return Unknown(self.prov(key))
        where = cite(self.document, "items", self.index, key)
        domain = self._domain(key, reading.instant, reading.resolution, where)
        return Known(Timestamp(reading.ticks, domain.id), self.prov(key))

    def _domain(self, key: str, instant: bool, resolution: Any, where: Any) -> TimestampDomain:
        cache_key = f"{key}|{instant}|{resolution}"
        if cache_key not in self.domains:
            provenance = Provenance(where, self.transform.id, AssertionKind.STATED)
            self.domains[cache_key] = TimestampDomain(
                id=evidence_record_id(TimestampDomain.kind, where, self.transform),
                provenance=provenance,
                field=key,
                scope=("interventions",),
                role=Known(ClockRole.DOCUMENT),
                resolution=Known(resolution),
                epoch=Known(Epoch.UNIX) if instant else Unknown(),
                timescale=Known(Timescale.POSIX) if instant else Unknown(),
                declared_monotonic=NotCovered(),
            )
        return self.domains[cache_key]

    def commands(self) -> tuple[Knowledge[str], ...]:
        value = self.item.get("commands")
        if not isinstance(value, list):
            return ()
        out: list[Knowledge[str]] = []
        for position, command in enumerate(value[:MAX_COMMANDS]):
            text = _text(command) if isinstance(command, str) else None
            if text is None:
                self._unreadable(f"commands/{position}", "not_text")
                continue
            out.append(Known(text, self.prov("commands", position)))
        if len(value) > MAX_COMMANDS:
            self._unreadable("commands", "too_many")
        return tuple(out)


def build_interventions(
    document: Document,
    transform: TransformRecord,
    formats: tuple[str, ...],
    report: Report,
) -> tuple[list[TimestampDomain], list[Intervention]]:
    """One ``Intervention`` per item of ``document`` that states a usable ``id``, in item order,
    with the clocks its times are on."""
    domains: dict[str, TimestampDomain] = {}
    records: list[Intervention] = []
    skipped = 0
    covered = frozenset(key for item in document.items for key in item)
    for index in range(len(document.items)):
        item = _Item(document, index, transform, formats, domains, covered, report)
        identifiers = item.ident("id", ID_NAMESPACE)
        if not identifiers:
            skipped += 1
            continue
        where = cite(document, "items", index)
        records.append(
            Intervention(
                id=evidence_record_id(Intervention.kind, where, transform),
                provenance=item.prov(),
                identifiers=identifiers,
                site=NotCovered(),
                machines=item.ident("deviceId", DEVICE_NAMESPACE),
                configuration=NotCovered(),
                related=(),
                mode=item.text("interventionType"),
                authority=item.text("authority"),
                reason=item.text("message"),
                commands=item.commands(),
                start=item.time("time"),
                end=item.time("endTime"),
                outcome=item.text("outcome"),
            )
        )
    if skipped:
        report(
            "record_skipped",
            cite(document, "items"),
            {"count": skipped, "reason": "id_invalid"},
        )
    return sorted(domains.values(), key=lambda d: d.id), records
