"""A sandboxed probe's reply is read back strictly, and a forged one is refused (ADR 0033 §1).

The job runs the probe engine over a source in a confined child that read hostile bytes, so the
``SourceProbe`` it sends back is rebuilt and checked: what the job can derive (the sniff, the
selection, the concluding findings) is derived again, the rest is parsed strictly, and the whole
must be what the engine writes. These tests take honest replies from every probe fixture, then
tamper with them the ways a compromised child could.
"""

import copy
import importlib.util
import json
import sys
from collections.abc import Callable
from functools import partial
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any, Final

import pytest

from neptune.adapters.builtin import builtin_adapters
from neptune.adapters.contract import PROBE_HEAD_SIZE
from neptune.adapters.registry import AdapterRegistry
from neptune.discovery.containers import ProbePolicy
from neptune.discovery.probe import PROBE_ID, ProbeEngine, SourceProbe, ask_in_process
from neptune.discovery.reader import BytesReader
from neptune.runtime import wire
from neptune.runtime.sandbox import Limits, Returned, Subprocess

if TYPE_CHECKING:
    from neptune.model.jsonvalue import JsonObject

FIXTURES: Final = Path(__file__).parents[2] / "fixtures"


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


TALLY: Final = _load("tally_adapter", FIXTURES / "adapters" / "tally_adapter.py")
ENGINE: Final = ProbeEngine(AdapterRegistry([*builtin_adapters(), TALLY.TallyAdapter()]))
CORPUS: Final = [
    *sorted((FIXTURES / "probe" / "containers").iterdir()),
    FIXTURES / "probe" / "signatures" / "cloud.zst",
    FIXTURES / "text" / "notes.txt",
]


def honest(data: bytes, name: str) -> tuple[SourceProbe, Any]:
    """The engine's probe of ``data`` and the JSON a sound child would send for it."""
    probed = ENGINE.probe(BytesReader(data), name)
    return probed, json.loads(json.dumps(probed.to_json()))


def read_back(data: bytes, name: str, reply: Any) -> SourceProbe:
    reader = BytesReader(data)
    head = data[: min(len(data), PROBE_HEAD_SIZE)]
    return ENGINE.source_probe_from_json(
        reply, source=reader.content_id, size=reader.size, name=name, head=head
    )


@pytest.mark.parametrize("path", CORPUS, ids=lambda p: p.name)
def test_an_honest_reply_reads_back_as_the_probe_itself(path: Path) -> None:
    data = path.read_bytes()
    probed, reply = honest(data, path.stem)
    assert read_back(data, path.stem, reply) == probed


def _members(reply: Any) -> list[Any]:
    return list(reply["container"]["members"])


FORGERIES: Final[dict[str, tuple[str, Callable[[Any], None]]]] = {
    "a_claim_raised": (
        "members.zip",
        lambda r: r["probes"][0]["result"].update(confidence=1.0),
    ),
    "an_adapter_dropped": ("notes.txt", lambda r: r["probes"].pop()),
    "an_unregistered_adapter": (
        "notes.txt",
        lambda r: r["probes"][0].update(adapter="mallory"),
    ),
    "another_version": ("notes.txt", lambda r: r["probes"][0].update(version="9.9.9")),
    "the_conclusion_removed": ("members.zip", lambda r: r["findings"].pop()),
    "the_selection_rewritten": (
        "notes.txt",
        lambda r: r["selection"].update(status="unsupported", candidates=[]),
    ),
    "a_signature_not_in_the_head": (
        "notes.txt",
        lambda r: r["sniff"]["signatures"].append({"name": "MCAP"}),
    ),
    "another_source": ("notes.txt", lambda r: r.update(size=r["size"] + 1)),
    "an_extra_field": ("notes.txt", lambda r: r.update(extra=1)),
    "a_member_of_an_unknown_kind": (
        "members.zip",
        lambda r: _members(r)[0].update(kind="portal"),
    ),
    "a_member_with_a_null_size": ("members.zip", lambda r: _members(r)[0].update(size=None)),
    "a_member_citing_another_source": (
        "members.zip",
        lambda r: _members(r)[0]["entry"].update(source="sha256:" + "0" * 64),
    ),
    "a_member_nested_past_the_policy": (
        "nested.zip",
        lambda r: _members(r)[0].update(nested=copy.deepcopy(r["container"])),
    ),
    "a_finding_citing_another_source": (
        "members.zip",
        lambda r: r["findings"][0]["subject"]["ref"].update(source="sha256:" + "1" * 64),
    ),
    "the_container_dropped": ("members.zip", lambda r: r.pop("container")),
    "a_container_invented": (
        "notes.txt",
        lambda r: r.update(container={"complete": True, "kind": "zip", "members": []}),
    ),
    "a_container_of_another_kind": ("members.zip", lambda r: r["container"].update(kind="tar")),
    "a_finding_of_another_producer": (
        "corrupt.gz",
        lambda r: r["findings"][0].update(transform="rec:sha256:" + "2" * 64),
    ),
}


@pytest.mark.parametrize("forgery", sorted(FORGERIES))
def test_a_forged_reply_is_refused(forgery: str) -> None:
    fixture, tamper = FORGERIES[forgery]
    path = next(p for p in CORPUS if p.name == fixture)
    data = path.read_bytes()
    _, reply = honest(data, path.stem)
    tamper(reply)
    with pytest.raises(ValueError):
        read_back(data, path.stem, reply)


def test_an_engine_that_opens_no_container_must_say_so() -> None:
    shallow = ProbeEngine(ENGINE.registry, ProbePolicy(max_depth=0))
    data = (FIXTURES / "probe" / "containers" / "members.zip").read_bytes()
    reader = BytesReader(data)
    probed = shallow.probe(reader, "bundle")
    reply = json.loads(json.dumps(probed.to_json()))
    kwargs: Any = {"source": reader.content_id, "size": reader.size, "name": "bundle"}
    assert shallow.source_probe_from_json(reply, head=data, **kwargs) == probed
    reply["findings"] = [f for f in reply["findings"] if "container_limit" not in f["code"]]
    with pytest.raises(ValueError, match="unopened"):
        shallow.source_probe_from_json(reply, head=data, **kwargs)


def test_a_fallback_probe_names_the_cause_and_leaves_the_container_closed() -> None:
    data = (FIXTURES / "probe" / "containers" / "members.zip").read_bytes()
    reader = BytesReader(data)
    cause: JsonObject = {"limit": "wall_seconds", "value": 120}
    probed = ENGINE.probe_head(
        reader.content_id, reader.size, "bundle", data, ask_in_process, cause
    )
    assert probed.container is None
    codes = [f.code for f in probed.findings]
    assert codes == [f"{PROBE_ID}.inspection_failed", f"{PROBE_ID}.unsupported"]
    failed = probed.findings[0]
    assert failed.details == {"container": "zip", "limit": "wall_seconds", "value": 120}
    assert "stopped at its wall_seconds limit" in failed.message
    plain = ENGINE.probe_head(
        BytesReader(b"TALLY1\n").content_id, 7, "x", b"TALLY1\n", ask_in_process, cause
    )
    assert plain.adapter == "tally" and plain.findings == ()  # nothing lost: no container


def test_the_codec_carries_a_probe_across_the_sandbox(tmp_path: Path) -> None:
    box = Subprocess(Limits(cpu_seconds=10, wall_seconds=20))
    for path in CORPUS:
        data = path.read_bytes()
        reader = BytesReader(data)
        head = data[: min(len(data), PROBE_HEAD_SIZE)]
        codec = wire.source_probe(ENGINE, reader.content_id, reader.size, path.stem, head)
        outcome = box.call(partial(ENGINE.probe, reader, path.stem), codec)
        assert outcome == Returned(ENGINE.probe(reader, path.stem)), path.name
