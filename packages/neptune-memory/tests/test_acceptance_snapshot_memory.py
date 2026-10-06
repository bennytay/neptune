"""The acceptance-corpus snapshot (``tests/fixtures/acceptance_corpus.graph.json``) is what Memory's
own pipeline makes of the MVL-181 corpus, and a graph document Memory's codec reads.

A regeneration is byte-identical on any host. ``acceptance_corpus.environment.json`` names the
libraries the compiler's transforms record, so a failure says which one moved. Deploy and Context
consume the file (``docs/contracts.md``).
"""

from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
from functools import cache
from typing import TYPE_CHECKING, Final

import pytest

from neptune.identity import canonical_json
from neptune.identity.ids import config_hash
from neptune_memory.cli import read_configs, registrations
from neptune_memory.derived.clocks import CLOCKS_MODEL, ESTIMATES_CONSOLIDATOR_ID
from neptune_memory.schema.codec import graph_from_json, graph_problems
from neptune_memory.schema.reference import ReferenceReader

if TYPE_CHECKING:
    from types import ModuleType

    from neptune.model.jsonvalue import JsonValue
    from neptune_memory.schema.codec import GraphDocument

MAX_FIXTURE_BYTES: Final = 512 * 1024


@cache
def generator() -> ModuleType:
    from pathlib import Path

    path = Path(__file__).resolve().parent / "fixtures" / "acceptance_corpus_snapshot.py"
    spec = importlib.util.spec_from_file_location("acceptance_corpus_snapshot", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def committed() -> bytes:
    data: bytes = generator().SNAPSHOT.read_bytes()
    return data


def document() -> GraphDocument:
    return graph_from_json(canonical_json.loads(committed().rstrip(b"\n")))


REGENERATE: Final = (
    "Regenerate it from the repository root, under the Python CI installs: uv run --all-packages "
    "--all-groups python packages/neptune-memory/tests/fixtures/acceptance_corpus_snapshot.py"
)


@pytest.fixture(scope="module")
def regenerated(tmp_path_factory: pytest.TempPathFactory) -> tuple[bytes, bytes]:
    """(graph, environment) regenerated in a fresh process (the compiler's sandbox forks, which a
    threaded test process should not do) under another hash seed and time zone."""
    out = tmp_path_factory.mktemp("acceptance")
    env = {**os.environ, "PYTHONHASHSEED": "4242", "TZ": "Pacific/Chatham"}
    script = [sys.executable, str(generator().__file__), "--out", str(out / "graph.json")]
    script += ["--environment-out", str(out / "environment.json")]
    done = subprocess.run(script, env=env, capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr[-2000:]
    return (out / "graph.json").read_bytes(), (out / "environment.json").read_bytes()


RECORD_ID: Final = re.compile(rb"rec:sha256:[0-9a-f]{64}")


def facts(data: bytes) -> list[bytes]:
    """Each claim's content with every Ledger record id masked: subject and object types and values,
    predicate, validity ticks, assertion kind, consolidator and the evidence (source content ids and
    locators). A failure here means the snapshot states other facts; a byte-check failure alone
    means only lineage ids moved (an adapter or library version renames its transform and every
    record downstream of it, Deploy's lifecycle records included)."""
    graph = graph_from_json(canonical_json.loads(data.rstrip(b"\n")))
    return sorted(
        RECORD_ID.sub(b"rec:*", canonical_json.dumps(claim.content_json()))
        for claim in graph.resolution.claims
    )


@pytest.mark.slow
def test_a_regeneration_states_the_same_facts(regenerated: tuple[bytes, bytes]) -> None:
    assert facts(regenerated[0]) == facts(committed()), (
        f"acceptance_corpus.graph.json is stale (the corpus, the compiler or Memory changed). "
        f"{REGENERATE}"
    )


def differences(here: JsonValue, recorded: JsonValue) -> dict[str, object]:
    """What differs between two environments, by corpus and by adapter: (here, snapshot)."""
    if not isinstance(here, dict) or not isinstance(recorded, dict):
        return {"environment": (here, recorded)}
    out: dict[str, object] = {}
    if here.get("corpus") != recorded.get("corpus"):
        out["corpus"] = (here.get("corpus"), recorded.get("corpus"))
    mine, theirs = here.get("transforms"), recorded.get("transforms")
    if not isinstance(mine, dict) or not isinstance(theirs, dict):
        return {**out, "transforms": (mine, theirs)}
    for adapter in sorted(mine.keys() | theirs.keys()):
        if mine.get(adapter) != theirs.get(adapter):
            out[adapter] = (mine.get(adapter), theirs.get(adapter))
    return out


@pytest.mark.slow
def test_a_regeneration_is_byte_identical(regenerated: tuple[bytes, bytes]) -> None:
    """On any host, in CI and locally: the compiler no longer records host-bound library versions
    in id-bearing content, so a difference here is a stale snapshot (the corpus, an adapter or its
    version, a library ``uv.lock`` pins, or the Python minor ``.python-version`` pins)."""
    graph, environment = regenerated
    recorded = generator().ENVIRONMENT.read_bytes()
    if environment != recorded:
        changed = differences(
            canonical_json.loads(environment.rstrip(b"\n")),
            canonical_json.loads(recorded.rstrip(b"\n")),
        )
        pytest.fail(f"the compiler's transforms changed (here, snapshot): {changed}. {REGENERATE}")
    assert graph == committed(), f"acceptance_corpus.graph.json is stale. {REGENERATE}"


def test_differences_name_each_changed_adapter_and_the_corpus() -> None:
    base: JsonValue = {
        "corpus": "acceptance 1.0.0",
        "transforms": {
            "calibration 0.1.1": {"pyyaml": "6.0.3"},
            "tabular 0.2.0": {"pyarrow": "25"},
        },
    }
    bumped: JsonValue = {
        "corpus": "acceptance 1.1.0",
        "transforms": {
            "calibration 0.1.2": {"pyyaml": "6.0.3"},
            "tabular 0.2.0": {"pyarrow": "26"},
        },
    }
    assert differences(base, base) == {}
    assert differences(bumped, base) == {
        "corpus": ("acceptance 1.1.0", "acceptance 1.0.0"),
        "calibration 0.1.1": (None, {"pyyaml": "6.0.3"}),
        "calibration 0.1.2": ({"pyyaml": "6.0.3"}, None),
        "tabular 0.2.0": ({"pyarrow": "26"}, {"pyarrow": "25"}),
    }
    assert differences([], base) == {"environment": ([], base)}


def test_the_snapshot_decodes_with_the_codec_and_reencodes_to_its_bytes() -> None:
    graph = document()
    assert graph_problems(canonical_json.loads(committed().rstrip(b"\n"))) == ()
    assert canonical_json.dumps(graph.to_json()) + b"\n" == committed()
    assert graph.head == generator().HEAD


def test_the_snapshot_is_built_by_memory_rebuild_with_estimates_and_nothing_else() -> None:
    graph = document()
    configs = read_configs(generator().CONFIG)
    registered = {
        r.consolidator_id: config_hash(r.config)
        for r in registrations(with_estimates=True, configs=configs)
    }
    assert {b.consolidator_id: str(b.config_hash) for b in graph.builds} == registered
    assert {c.provenance.consolidator_id for c in graph.resolution.claims} <= set(registered)
    assert graph.resolution.claims, "the corpus consolidates to no claim at all"


def test_inferred_claims_are_exactly_the_relayed_estimates_and_one_flag_drops_them() -> None:
    """The compiler's estimated clock fits arrive only as ``memory.time_estimates`` claims, every
    one ``inferred`` with the compiler's model; nothing deterministic is inferred; and a reader
    asked for ``include_inferred=False`` sees none of them."""
    graph = document()
    claims = graph.resolution.claims
    inferred = [c for c in claims if str(c.assertion_kind) == "inferred"]
    assert inferred, "the snapshot relays no estimated clock mapping"
    assert {c.provenance.consolidator_id for c in inferred} == {ESTIMATES_CONSOLIDATOR_ID}
    assert {c.provenance.model for c in inferred} == {CLOCKS_MODEL}
    assert all(
        str(c.assertion_kind) == "inferred"
        for c in claims
        if c.provenance.consolidator_id == ESTIMATES_CONSOLIDATOR_ID
    )
    reader = ReferenceReader(graph)
    for subject in {c.subject for c in inferred}:
        everything = reader.claims(subject, None, graph.head)
        evidence_only = reader.claims(subject, None, graph.head, include_inferred=False)
        assert {c.id for c in everything.claims} >= {c.id for c in inferred if c.subject == subject}
        assert all(str(c.assertion_kind) != "inferred" for c in evidence_only.claims)


def test_the_snapshot_stays_under_the_fixture_limit() -> None:
    assert len(committed()) < MAX_FIXTURE_BYTES
