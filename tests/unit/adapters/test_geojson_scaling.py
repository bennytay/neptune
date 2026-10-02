"""GeoJSON adapter: input-controlled costs stay linear in the input (MVL-31 review fixes).

A bound is a ratio between two sizes of the same input, not a wall-clock limit, so a slow
machine does not fail it and a quadratic path does.
"""

import time
from collections.abc import Callable

from neptune.adapters.contract import PROBE_HEAD_SIZE, ProbeHints
from neptune.adapters.geojson import GeoJsonAdapter
from neptune.adapters.geojson._records import Offsets
from neptune.adapters.geojson._scan import bracket_end
from neptune.adapters.harness import ingest_source
from neptune.discovery.reader import BytesReader

_RUNS = 3


def best_seconds(work: Callable[[], object]) -> float:
    best = float("inf")
    for _ in range(_RUNS):
        started = time.perf_counter()
        work()
        best = min(best, time.perf_counter() - started)
    return best


def assert_linear_ish(small: Callable[[], object], big: Callable[[], object], scale: int) -> None:
    """``big`` does ``scale`` times the work of ``small``: quadratic would cost ``scale ** 2``."""
    ratio = best_seconds(big) / max(best_seconds(small), 1e-4)
    assert ratio < scale * 3, f"{scale}x the input cost {ratio:.1f}x"


def unterminated(depth: int, quotes: int, head: bytes) -> bytes:
    """Nesting deeper than ``json`` reads, then a string that never closes and holds escaped
    quotes: the shape that made every later quote a fresh scan to the end."""
    return head + b"[" * depth + b'"' + b'\\"' * quotes


def test_bracket_end_reads_strings_and_escapes() -> None:
    text = r'[["a]", "b\"]", "c\\"], {"k": "}"}] tail'
    assert bracket_end(text, 0) == text.index(" tail")
    assert bracket_end('["x\\', 0) is None
    assert bracket_end('["x\\"', 0) is None
    assert bracket_end('["x" ', 0) is None
    assert bracket_end('[["x"]', 0) is None


def test_bracket_end_stops_at_the_first_unterminated_string() -> None:
    def work(n: int) -> Callable[[], object]:
        text = "[" * 1000 + '"' + '\\"' * n
        return lambda: bracket_end(text, 0)

    assert bracket_end("[" * 1000 + '"' + '\\"' * 50, 0) is None
    assert_linear_ish(work(20_000), work(80_000), 4)


def test_probe_is_linear_on_deep_nesting_and_an_unterminated_string() -> None:
    head = b'{"type":"FeatureCollection","a":'

    def work(quotes: int) -> Callable[[], object]:
        data = unterminated(3000, quotes, head)[:PROBE_HEAD_SIZE]
        hints = ProbeHints("x", len(data))
        return lambda: GeoJsonAdapter().probe(data, hints)

    assert_linear_ish(work(2_000), work(8_000), 4)
    assert work(8_000)() is not None


def test_plan_is_linear_on_deep_nesting_and_an_unterminated_string() -> None:
    head = b'{"type":"FeatureCollection","features":['

    def work(quotes: int) -> Callable[[], object]:
        data = unterminated(3000, quotes, head)
        return lambda: ingest_source(GeoJsonAdapter(), BytesReader(data), {})

    assert_linear_ish(work(5_000), work(20_000), 4)
    codes = {f.code for f in work(5_000)().findings()}  # type: ignore[attr-defined]
    assert "geojson.json_truncated" in codes


def test_offsets_do_not_rewind_on_a_backward_lookup() -> None:
    text = "é" * 50_000 + "x"
    where = Offsets(text, 100)
    assert where.at(0) == 100
    assert where.at(len(text)) == 100 + 2 * 50_000 + 1
    assert where.at(49_999) == 100 + 2 * 49_999
    assert where.at(5) == 110
    assert where.at(4096) == 100 + 2 * 4096
    assert Offsets("", 7).at(0) == 7
    assert Offsets("aé", 0).at(2) == 3


def test_a_non_ascii_feature_costs_the_same_as_its_ascii_twin() -> None:
    def feature(pad: str) -> bytes:
        props = ",".join(f'"k{i}":1' for i in range(1500))
        return (
            f'{{"type":"Feature","geometry":null,"properties":{{"pad":"{pad}",{props}}}}}'
        ).encode()

    def work(data: bytes) -> Callable[[], object]:
        return lambda: ingest_source(GeoJsonAdapter(), BytesReader(data), {})

    ascii_cost = best_seconds(work(feature("e" * 200_000)))
    wide_cost = best_seconds(work(feature("é" * 200_000)))
    assert wide_cost < max(ascii_cost, 1e-3) * 6
