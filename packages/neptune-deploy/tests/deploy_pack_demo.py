"""The Demo v1 packs over the acceptance corpus snapshot (ADR 0014), and the sample PDF.

``python deploy_pack_demo.py`` rewrites ``docs/samples/incident-timeline-INC-C3-0011.pdf``;
``test_deploy_packs_incident`` checks it is what the compiler renders today.
"""

import copy
import json
from functools import cache
from pathlib import Path
from typing import Any, Final

import deploy_pack_corpus as corpus
from neptune_deploy.packs import EvidencePack, PackSpec, Snapshot, compile_pack, read_snapshot
from neptune_deploy.packs import render_pdf as render
from neptune_deploy.packs.snapshot import Interval, Node, Stamp

SAMPLE: Final = (
    Path(__file__).resolve().parents[1] / "docs" / "samples" / "incident-timeline-INC-C3-0011.pdf"
)


def as_node(value: dict[str, Any]) -> Node:
    return Node(value["node_type"], value["node_id"])


ARM: Final = as_node(corpus.ARM)
AMR: Final = as_node(corpus.AMR)
LEG: Final = as_node(corpus.LEG)
INC_C3: Final = as_node(corpus.event(corpus.INC_C3))
INC_0007: Final = as_node(corpus.event(corpus.INC_0007_CMMS))
SYSLOG_PSTOP: Final = as_node(corpus.event(corpus.SYSLOG_PSTOP))
SYSLOG_WARN: Final = as_node(corpus.event(corpus.SYSLOG_WARN))
INTERVENTION: Final = as_node(corpus.event(corpus.INTERVENTION))
CONTROLLER_FAULT: Final = as_node(corpus.event(corpus.CONTROLLER_FAULT))
INC_0007_REPORT: Final = as_node(corpus.event(corpus.INC_0007_REPORT))

PLANT2_YEAR: Final = Interval(Stamp(corpus.P2_LIFE, corpus.wall(2026, 1, 1)), "open")
S007_YEAR: Final = Interval(Stamp(corpus.S7_LIFE, corpus.wall(2026, 1, 1)), "open")
INC_C3_HOUR: Final = Interval(
    Stamp(corpus.P2_REPORT, corpus.wall(2026, 9, 14, 14)),
    Stamp(corpus.P2_REPORT, corpus.wall(2026, 9, 14, 15)),
)
INC_0007_HOUR: Final = Interval(
    Stamp(corpus.S7_LIFE, corpus.wall(2026, 4, 2, 14)),
    Stamp(corpus.S7_LIFE, corpus.wall(2026, 4, 2, 15)),
)


def document() -> dict[str, Any]:
    """A fresh copy of the snapshot document (tests mutate it)."""
    return copy.deepcopy(_document())


@cache
def _document() -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(corpus.fixture_path().read_bytes())
    return loaded


@cache
def snapshot() -> Snapshot:
    return read_snapshot(_document())


def spec(
    template: str,
    subject: Node,
    interval: Interval,
    snap: Snapshot | None = None,
    inference: str = "exclude",
) -> PackSpec:
    return PackSpec(template, 1, subject, interval, (snap or snapshot()).id, inference)


def traceability(
    subject: Node = ARM, interval: Interval = PLANT2_YEAR, **kwargs: Any
) -> EvidencePack:
    snap = kwargs.pop("snap", None) or snapshot()
    return compile_pack(spec("configuration-traceability", subject, interval, snap, **kwargs), snap)


def incident(
    subject: Node = INC_C3, interval: Interval = INC_C3_HOUR, **kwargs: Any
) -> EvidencePack:
    snap = kwargs.pop("snap", None) or snapshot()
    return compile_pack(spec("incident-timeline", subject, interval, snap, **kwargs), snap)


def demo_packs() -> dict[str, EvidencePack]:
    """The four packs the demo shows: two traceability reports, two reconstructions."""
    return {
        "arm-3a": traceability(),
        "amr-07": traceability(AMR, S007_YEAR),
        "inc-c3-0011": incident(),
        "inc-0007": incident(INC_0007, INC_0007_HOUR),
    }


def sample_bytes() -> bytes:
    return render(incident())


if __name__ == "__main__":
    SAMPLE.parent.mkdir(parents=True, exist_ok=True)
    SAMPLE.write_bytes(sample_bytes())
