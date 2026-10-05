"""A descriptor's optional ``ChunkExtent`` (ADR 0069): which context keys name the source bytes
each chunk decodes, checked by ``check_plan`` and read by the runtime to cite a lost chunk."""

from dataclasses import replace
from typing import Any

import pytest

from neptune.adapters.check import check_plan
from neptune.adapters.contract import (
    ChunkExtent,
    ContractError,
    Plan,
    chunk_extent,
    configure,
    make_chunk,
)
from neptune.adapters.flightlog import DESCRIPTOR as FLIGHTLOG
from neptune.adapters.mcap import DESCRIPTOR as MCAP
from neptune.adapters.rosbag1 import DESCRIPTOR as ROSBAG1
from neptune.adapters.text import DESCRIPTOR as TEXT
from neptune.adapters.text import TextAdapter
from neptune.discovery.reader import BytesReader

SOURCE = BytesReader(b"first paragraph\n\nsecond\n")
CONFIG = configure(TEXT, None)


def chunk(**context: Any) -> Any:
    return make_chunk(SOURCE, CONFIG, {"part": "blocks", **context}, 0)


def test_the_byte_window_adapters_declare_it_and_say_so() -> None:
    for descriptor in (MCAP, ROSBAG1, FLIGHTLOG, TEXT):
        assert descriptor.extent == ChunkExtent()
        assert descriptor.to_json()["extent"] == {"end": "end", "start": "start"}
    assert "extent" not in replace(TEXT, extent=None).to_json()


@pytest.mark.parametrize(
    ("start", "end"),
    [("a", "a"), ("Start", "end"), ("", "end")],
)
def test_an_extent_is_two_distinct_context_keys(start: str, end: str) -> None:
    with pytest.raises(ValueError):
        ChunkExtent(start, end)


def test_a_descriptor_extent_must_be_a_chunk_extent() -> None:
    with pytest.raises(ContractError, match="ChunkExtent"):
        replace(TEXT, extent=("start", "end"))  # type: ignore[arg-type]


def test_a_chunk_names_its_window_or_none() -> None:
    size = SOURCE.size
    assert chunk_extent(ChunkExtent(), chunk(start=0, end=5), size) == (0, 5)
    assert chunk_extent(ChunkExtent(), chunk(start=size, end=size), size) == (size, size)
    assert chunk_extent(ChunkExtent(), chunk(), size) is None  # a declarations chunk
    assert chunk_extent(None, chunk(start=0, end=5), size) is None  # the adapter names none
    other = ChunkExtent("offset", "stop")
    assert chunk_extent(other, chunk(offset=2, stop=4, start=0, end=1), size) == (2, 4)


@pytest.mark.parametrize(
    "context",
    [
        {"start": 0},  # one key without the other
        {"end": 3},
        {"start": "0", "end": 3},
        {"start": True, "end": 3},  # a bool is not an offset
        {"start": 1.0, "end": 3},
        {"start": 4, "end": 3},  # backwards
        {"start": -1, "end": 3},
        {"start": 0, "end": 10**9},  # past the source
    ],
)
def test_a_malformed_extent_breaks_the_contract(context: dict[str, Any]) -> None:
    with pytest.raises(ContractError, match="extent"):
        chunk_extent(ChunkExtent(), chunk(**context), SOURCE.size)


def test_check_plan_refuses_a_plan_whose_extent_is_malformed() -> None:
    plan = TextAdapter(chunk_bytes=4).plan(SOURCE, CONFIG)
    check_plan(TEXT, SOURCE, CONFIG, plan)  # every real chunk names a window inside the source
    bad = Plan((*plan.chunks, chunk(start=3, end=SOURCE.size + 1)))
    with pytest.raises(ContractError, match="extent"):
        check_plan(TEXT, SOURCE, CONFIG, bad)
    # An adapter that declares no extent is not held to its context's keys.
    check_plan(replace(TEXT, extent=None), SOURCE, CONFIG, bad)
