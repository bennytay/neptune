"""The acceptance-corpus snapshot (``tests/fixtures/acceptance_corpus.graph.json``) is what Memory's
own pipeline makes of the MVL-181 corpus, and a graph document Memory's codec reads.

A regeneration states the same facts on any host. It is byte-identical wherever the compiler's
transforms record the same libraries as ``acceptance_corpus.environment.json`` (the generator's
docstring says why they can differ). Deploy and Context consume the file (``docs/contracts.md``).
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
from neptune_memory.cli import registrations
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
    locators). Record ids are what the host-bound libraries rename (a transform id, and every record
    downstream of it, such as Deploy's lifecycle records); the byte check pins them in CI."""
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


# Libraries a transform records that come with the CPython build, not with uv.lock: a host may
# differ in them alone and still run the code the snapshot was made with.
# TODO: remove once the compiler moves library and runtime versions out of id-bearing content
# (root non-negotiable 5); then every regeneration is byte-identical and nothing skips.
HOST_BOUND: Final = frozenset({"expat", "python"})
COMPARE: Final = "compare"
SKIP: Final = "skip"
FAIL: Final = "fail"


def byte_check(here: JsonValue, recorded: JsonValue, *, ci: bool) -> tuple[str, str]:
    """(``compare`` | ``skip`` | ``fail``, why) for a regeneration whose environment is ``here``
    against the snapshot's ``recorded``. Skip only where the two differ in host-bound libraries
    alone, and never in CI; any other difference (the corpus, an adapter or its version, a library
    uv.lock pins) means the committed snapshot is stale."""
    if here == recorded:
        return COMPARE, "the same libraries"
    if not isinstance(here, dict) or not isinstance(recorded, dict):
        return FAIL, "an environment is not a JSON object"
    if here.get("corpus") != recorded.get("corpus"):
        return FAIL, f"the corpus changed: {here.get('corpus')} != {recorded.get('corpus')}"
    mine, theirs = here.get("transforms"), recorded.get("transforms")
    if not isinstance(mine, dict) or not isinstance(theirs, dict):
        return FAIL, "an environment has no transforms object"
    if mine.keys() != theirs.keys():
        changed = sorted(mine.keys() ^ theirs.keys())
        return FAIL, f"adapters or adapter versions changed: {changed}"
    host: dict[str, object] = {}
    for adapter in sorted(mine):
        a, b = mine[adapter], theirs[adapter]
        if not isinstance(a, dict) or not isinstance(b, dict):
            return FAIL, f"{adapter}: libraries are not a JSON object"
        for library in sorted(a.keys() | b.keys()):
            if a.get(library) == b.get(library):
                continue
            if library not in HOST_BOUND:
                return FAIL, f"{adapter}: {library} {b.get(library)} -> {a.get(library)}"
            host[f"{adapter}: {library}"] = (a.get(library), b.get(library))
    if ci:
        return FAIL, f"CI must reproduce the snapshot byte for byte; host libraries differ: {host}"
    return SKIP, f"host libraries differ from the snapshot's (here, snapshot): {host}"


@pytest.mark.slow
def test_a_regeneration_with_the_same_libraries_is_byte_identical(
    regenerated: tuple[bytes, bytes],
) -> None:
    graph, environment = regenerated
    recorded = generator().ENVIRONMENT.read_bytes()
    ci = bool(os.environ.get("CI") or os.environ.get("GITHUB_ACTIONS"))
    verdict, why = byte_check(
        canonical_json.loads(environment.rstrip(b"\n")),
        canonical_json.loads(recorded.rstrip(b"\n")),
        ci=ci,
    )
    if verdict == SKIP:
        pytest.skip(why)
    assert verdict == COMPARE, f"acceptance_corpus.environment.json is stale: {why}. {REGENERATE}"
    assert graph == committed(), f"acceptance_corpus.graph.json is stale. {REGENERATE}"


def environment(**transforms: dict[str, str]) -> JsonValue:
    """An environment document; ``calibration_0_1_0`` is the key ``calibration 0.1.0``."""
    keys = {k: "{} {}".format(*k.split("_", 1)).replace("_", ".") for k in transforms}
    return {
        "corpus": "acceptance 1.0.0",
        "transforms": {keys[k]: dict(v) for k, v in transforms.items()},
    }


BASE: Final = environment(
    calibration_0_1_0={"expat": "expat_2.8.5", "python": "3.12", "pyyaml": "6.0.3"},
    tabular_0_2_0={"pyarrow": "25.0.1"},
)


@pytest.mark.parametrize(
    ("here", "ci", "verdict"),
    [
        (BASE, False, COMPARE),
        (BASE, True, COMPARE),
        # expat or python alone: a host difference; skipped locally, a failure in CI
        (
            environment(
                calibration_0_1_0={"expat": "expat_2.8.3", "python": "3.12", "pyyaml": "6.0.3"},
                tabular_0_2_0={"pyarrow": "25.0.1"},
            ),
            False,
            SKIP,
        ),
        (
            environment(
                calibration_0_1_0={"expat": "expat_2.8.3", "python": "3.13", "pyyaml": "6.0.3"},
                tabular_0_2_0={"pyarrow": "25.0.1"},
            ),
            True,
            FAIL,
        ),
        # a uv.lock bump, alone or beside a host difference
        (
            environment(
                calibration_0_1_0={"expat": "expat_2.8.5", "python": "3.12", "pyyaml": "6.0.3"},
                tabular_0_2_0={"pyarrow": "26.0.0"},
            ),
            False,
            FAIL,
        ),
        (
            environment(
                calibration_0_1_0={"expat": "expat_2.8.3", "python": "3.12", "pyyaml": "6.0.4"},
                tabular_0_2_0={"pyarrow": "25.0.1"},
            ),
            False,
            FAIL,
        ),
        # a library added or dropped, an adapter version bumped or an adapter added
        (
            environment(
                calibration_0_1_0={"expat": "expat_2.8.5", "python": "3.12"},
                tabular_0_2_0={"pyarrow": "25.0.1"},
            ),
            False,
            FAIL,
        ),
        (
            environment(
                calibration_0_2_0={"expat": "expat_2.8.5", "python": "3.12", "pyyaml": "6.0.3"},
                tabular_0_2_0={"pyarrow": "25.0.1"},
            ),
            False,
            FAIL,
        ),
        (
            environment(
                calibration_0_1_0={"expat": "expat_2.8.3", "python": "3.12", "pyyaml": "6.0.3"},
                tabular_0_2_0={"pyarrow": "25.0.1"},
                text_0_1_0={},
            ),
            False,
            FAIL,
        ),
        ({"corpus": "acceptance 1.1.0", "transforms": {}}, False, FAIL),
        ([], False, FAIL),
    ],
)
def test_the_byte_check_skips_only_for_host_libraries_outside_ci(
    here: JsonValue, ci: bool, verdict: str
) -> None:
    assert byte_check(here, BASE, ci=ci)[0] == verdict


def test_the_snapshot_decodes_with_the_codec_and_reencodes_to_its_bytes() -> None:
    graph = document()
    assert graph_problems(canonical_json.loads(committed().rstrip(b"\n"))) == ()
    assert canonical_json.dumps(graph.to_json()) + b"\n" == committed()
    assert graph.head == generator().HEAD


def test_the_snapshot_is_built_by_memory_rebuild_with_estimates_and_nothing_else() -> None:
    graph = document()
    registered = sorted(r.consolidator_id for r in registrations(with_estimates=True))
    assert sorted(b.consolidator_id for b in graph.builds) == registered
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
