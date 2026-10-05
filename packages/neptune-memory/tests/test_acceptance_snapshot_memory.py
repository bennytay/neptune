"""The acceptance-corpus snapshot (``tests/fixtures/acceptance_corpus.graph.json``) is what Memory's
own pipeline makes of the MVL-181 corpus, byte for byte, and a graph document Memory's codec reads.

Deploy and Context consume this file (``docs/contracts.md``); a hand-made document fails the codec.
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
    from pathlib import Path
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


@pytest.mark.slow
def test_the_committed_snapshot_is_what_the_pipeline_makes_of_the_corpus(tmp_path: Path) -> None:
    """Regenerated in a fresh process (the compiler's sandbox forks, which a threaded test process
    should not do) under another hash seed and time zone."""
    out = tmp_path / "graph.json"
    env = {**os.environ, "PYTHONHASHSEED": "4242", "TZ": "Pacific/Chatham"}
    script = [sys.executable, str(generator().__file__), "--out", str(out)]
    done = subprocess.run(script, env=env, capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr[-2000:]
    assert out.read_bytes() == committed(), (
        "acceptance_corpus.graph.json is stale (the corpus, the compiler or Memory changed). "
        "Regenerate it from the repository root: uv run --all-packages python "
        "packages/neptune-memory/tests/fixtures/acceptance_corpus_snapshot.py"
    )


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
