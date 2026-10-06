"""The acceptance-corpus snapshot (``tests/fixtures/acceptance_corpus.graph.json``) is what Memory's
own pipeline makes of the MVL-181 corpus, and a graph document Memory's codec reads.

A regeneration states the same facts on any host. It is byte-identical wherever the compiler's
transforms record the same libraries as ``acceptance_corpus.environment.json`` (the generator's
docstring says why they can differ). Deploy and Context consume the file (``docs/contracts.md``).
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from functools import cache
from typing import TYPE_CHECKING, Final

import pytest

from neptune.identity import canonical_json
from neptune_memory.consolidate.snapshot import default_registrations
from neptune_memory.schema.codec import graph_from_json, graph_problems

if TYPE_CHECKING:
    from types import ModuleType

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
    "python packages/neptune-memory/tests/fixtures/acceptance_corpus_snapshot.py"
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


def facts(data: bytes) -> list[bytes]:
    """Each claim without what names Ledger records (its id and its record ids, so supersedes too):
    subject, predicate, object, validity, assertion kind, consolidator and evidence."""
    graph = graph_from_json(canonical_json.loads(data.rstrip(b"\n")))
    out = []
    for claim in graph.resolution.claims:
        content = dict(claim.content_json())
        provenance = dict(content.pop("provenance"))  # type: ignore[arg-type]
        provenance.pop("records")
        out.append(canonical_json.dumps({**content, "provenance": provenance}))
    return sorted(out)


@pytest.mark.slow
def test_a_regeneration_states_the_same_facts(regenerated: tuple[bytes, bytes]) -> None:
    assert facts(regenerated[0]) == facts(committed()), (
        f"acceptance_corpus.graph.json is stale (the corpus, the compiler or Memory changed). "
        f"{REGENERATE}"
    )


@pytest.mark.slow
def test_a_regeneration_with_the_same_libraries_is_byte_identical(
    regenerated: tuple[bytes, bytes],
) -> None:
    graph, environment = regenerated
    recorded = generator().ENVIRONMENT.read_bytes()
    if environment != recorded:
        here = canonical_json.loads(environment.rstrip(b"\n"))
        there = canonical_json.loads(recorded.rstrip(b"\n"))
        assert isinstance(here, dict) and isinstance(there, dict)
        assert here["corpus"] == there["corpus"], f"the corpus changed. {REGENERATE}"
        mine, theirs = here["transforms"], there["transforms"]
        assert isinstance(mine, dict) and isinstance(theirs, dict)
        differ = {k: (mine.get(k), theirs.get(k)) for k in mine.keys() | theirs.keys()}
        differ = {k: pair for k, pair in sorted(differ.items()) if pair[0] != pair[1]}
        pytest.skip(f"libraries here differ from the snapshot's (here, snapshot): {differ}")
    assert graph == committed(), f"acceptance_corpus.graph.json is stale. {REGENERATE}"


def test_the_snapshot_decodes_with_the_codec_and_reencodes_to_its_bytes() -> None:
    graph = document()
    assert graph_problems(canonical_json.loads(committed().rstrip(b"\n"))) == ()
    assert canonical_json.dumps(graph.to_json()) + b"\n" == committed()
    assert graph.head == generator().REGISTERED_AT


def test_the_snapshot_is_built_by_every_default_consolidator_and_nothing_else() -> None:
    graph = document()
    registered = sorted(r.consolidator_id for r in default_registrations())
    assert sorted(b.consolidator_id for b in graph.builds) == registered
    assert {c.provenance.consolidator_id for c in graph.resolution.claims} <= set(registered)
    assert graph.resolution.claims, "the corpus consolidates to no claim at all"


def test_the_snapshot_stays_under_the_fixture_limit() -> None:
    assert len(committed()) < MAX_FIXTURE_BYTES
