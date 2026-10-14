"""graph-schema 2.5.0's ``clock_description`` literal in a frozen snapshot (Memory ADR 0029).

Memory's acceptance snapshot describes every clock as the Ledger declares it. The pack compiler
reads each description, shows it as declared (never applied to a tick), and refuses a malformed
one with its pointer, like any other literal.
"""

from __future__ import annotations

import copy
import gzip
import json
from pathlib import Path
from typing import Any, Final

import pytest

from neptune_deploy.packs.errors import PackError
from neptune_deploy.packs.snapshot import read_snapshot
from neptune_deploy.packs.text import claim_object

REPO: Final = Path(__file__).resolve().parents[3]
SNAPSHOT: Final = (
    REPO / "packages" / "neptune-memory" / "tests" / "fixtures" / "acceptance_corpus.graph.json.gz"
)


def document() -> dict[str, Any]:
    return json.loads(gzip.decompress(SNAPSHOT.read_bytes()))  # type: ignore[no-any-return]


def _syslog_clock(doc: dict[str, Any]) -> int:
    """The LOG-P2 syslog export's clock: its zone a Deploy mapping declares."""
    return next(
        i
        for i, c in enumerate(doc["claims"])
        if c["predicate"] == "clock_description"
        and c["object"]["value"]["field"] == "Timestamp"
        and c["object"]["value"]["civil_time_zone"].get("value") == "America/New_York"
    )


def test_the_snapshot_reads_with_a_description_of_every_clock() -> None:
    doc = document()
    snapshot = read_snapshot(doc)
    assert not snapshot.unread
    described = {c.subject.node_id for c in snapshot.claims if c.predicate == "clock_description"}
    assert len(described) == 79  # 77 domains and 2 shared civil clocks


def test_a_clock_description_renders_as_declared() -> None:
    doc = document()
    literal = doc["claims"][_syslog_clock(doc)]["object"]
    assert claim_object(literal) == (
        'clock description: field "Timestamp", tick 1/1 s, epoch unix, timescale unknown, wall'
        " clock, zone America/New_York as declared by deploy_event_log_map"
    )


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("civil_time_zone", None),
        ("resolution", {"knowledge": "known", "value": {"denominator": 1, "numerator": 0}}),
        ("epoch", {"knowledge": "ambiguous", "candidates": [{"value": "unix"}]}),
        ("scope", "syslog events"),
        ("field", 7),
    ],
)
def test_a_malformed_clock_description_is_refused(key: str, value: object) -> None:
    doc = copy.deepcopy(document())
    doc["claims"][_syslog_clock(doc)]["object"]["value"][key] = value
    with pytest.raises(PackError, match="snapshot_malformed"):
        read_snapshot(doc)


def test_a_clock_description_of_a_machine_is_refused() -> None:
    doc = copy.deepcopy(document())
    claim = doc["claims"][_syslog_clock(doc)]
    claim["subject"] = {"kind": "node", "node_id": "syslog.host:ARM-3A", "node_type": "machine"}
    with pytest.raises(PackError, match="snapshot_malformed"):
        read_snapshot(doc)
