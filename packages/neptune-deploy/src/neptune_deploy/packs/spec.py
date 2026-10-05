"""``PackSpec``: what a pack is compiled from (ADR 0013 §1).

A spec is JSON (``neptune-deploy.pack-spec/1``)::

    {
      "schema": "neptune-deploy.pack-spec/1",
      "template": {"id": "configuration-lineage", "version": 1},
      "subject": {"node_type": "machine", "node_id": "asset-tag:ARM-06"},
      "interval": {"start": {"domain_id": "rec:sha256:…", "ticks": 0}, "end": "open"},
      "snapshot": "snapshot:sha256:…",
      "inference": "exclude"
    }

``subject`` is a site, a deployment or a machine (a graph-schema node). ``interval`` is on one
clock; claims on another clock are never compared with it. ``snapshot`` is the id of the frozen
graph document the pack reads (``snapshot.snapshot_id``). ``inference`` is ``exclude`` (the
default) or ``include``; included inferred content is marked wherever it appears.
"""

from dataclasses import dataclass
from typing import Any, Final

from neptune.model.jsonvalue import JsonObject, JsonValue
from neptune_deploy.packs._read import SHA256, TOKEN, Reader, parse_document
from neptune_deploy.packs.errors import PackError
from neptune_deploy.packs.snapshot import SNAPSHOT_PREFIX, Interval, Node, Stamp, read_interval

SPEC_SCHEMA: Final = "neptune-deploy.pack-spec/1"
SUBJECT_TYPES: Final = ("deployment", "machine", "site")
INFERENCE_POLICIES: Final = ("exclude", "include")
MAX_SPEC_BYTES: Final = 1024 * 1024

_R: Final = Reader("spec_malformed")


@dataclass(frozen=True)
class PackSpec:
    template_id: str
    template_version: int
    subject: Node
    interval: Interval
    snapshot: str
    inference: str = "exclude"

    def __post_init__(self) -> None:
        # Construction in code is held to the same rules as a spec file.
        _fields(self.to_json())

    @property
    def clock(self) -> str:
        """The clock the pack's interval is on (``read_spec`` refuses an interval on two)."""
        return self.interval.start.domain

    def to_json(self) -> JsonObject:
        return {
            "inference": self.inference,
            "interval": self.interval.to_json(),
            "schema": SPEC_SCHEMA,
            "snapshot": self.snapshot,
            "subject": {"node_id": self.subject.node_id, "node_type": self.subject.node_type},
            "template": {"id": self.template_id, "version": self.template_version},
        }


def load_spec(data: bytes) -> PackSpec:
    return read_spec(parse_document(data, "spec_malformed", MAX_SPEC_BYTES))


def read_spec(value: JsonValue) -> PackSpec:
    return PackSpec(**_fields(value))


def _fields(value: JsonValue) -> dict[str, Any]:
    spec = _R.obj(
        value, "", ("interval", "schema", "snapshot", "subject", "template"), ("inference",)
    )
    if spec["schema"] != SPEC_SCHEMA:
        raise _R.fail(f"schema is not {SPEC_SCHEMA}", "/schema")
    template = _R.obj(spec["template"], "/template", ("id", "version"))
    subject = _R.obj(spec["subject"], "/subject", ("node_id", "node_type"))
    interval = read_interval(spec["interval"], "/interval", _R)
    if isinstance(interval.end, Stamp):
        if interval.end.domain != interval.start.domain:
            raise _R.fail("the interval's bounds are on two clocks", "/interval")
        if interval.end.ticks <= interval.start.ticks:
            raise _R.fail("the interval ends at or before its start", "/interval")
    snapshot = _R.string(spec["snapshot"], "/snapshot")
    if not snapshot.startswith(SNAPSHOT_PREFIX) or not SHA256.fullmatch(
        snapshot.removeprefix(SNAPSHOT_PREFIX)
    ):
        raise _R.fail("a snapshot id is snapshot:sha256:<64 hex>", "/snapshot")
    return {
        "template_id": _R.string(template["id"], "/template/id", TOKEN),
        "template_version": _R.integer(template["version"], "/template/version", 1),
        "subject": Node(
            _R.choice(subject["node_type"], "/subject/node_type", SUBJECT_TYPES),
            _R.text(subject["node_id"], "/subject/node_id"),
        ),
        "interval": interval,
        "snapshot": snapshot,
        "inference": _R.choice(spec.get("inference", "exclude"), "/inference", INFERENCE_POLICIES),
    }


__all__ = ["INFERENCE_POLICIES", "SPEC_SCHEMA", "SUBJECT_TYPES", "PackError", "PackSpec"]
