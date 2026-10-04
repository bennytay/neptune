"""``model.references.named``: what a record names, read from the model's field types, so the
salvage check (ADR 0069 §2) needs no hand list of reference fields."""

from pathlib import Path
from typing import Any, Final

from neptune.adapters.config import ConfigAdapter
from neptune.adapters.harness import ingest_source
from neptune.adapters.mcap import McapAdapter
from neptune.adapters.tabular import TabularAdapter
from neptune.discovery.reader import BytesReader
from neptune.model.references import named

FIXTURES: Final = Path(__file__).parents[2] / "fixtures"
LOOKALIKE: Final = "rec:sha256:" + "a" * 64


def records_of(adapter: Any, data: bytes) -> tuple[list[Any], Any]:
    output = ingest_source(adapter, BytesReader(data))
    found = [r for o in output.outputs for r in (*o.records, *o.findings)]
    return [*found, *output.plan.findings], output.config.transform


def test_a_whole_source_names_only_records_it_holds() -> None:
    """Every reference a real adapter's whole output makes resolves inside that output: the
    walker finds no false reference, so a salvage refused for one is a real loss."""
    for adapter, data in (
        (McapAdapter(chunk_bytes=512), (FIXTURES / "mcap" / "robot.mcap").read_bytes()),
        (ConfigAdapter(chunk_values=2), b'{"a": 1, "b": {"c": [1, 2]}, "a": 2}\n'),
    ):
        records, transform = records_of(adapter, data)
        ids = {r.id for r in records}
        names = [(r.kind, f, t) for r in records for f, t in named(r)]
        assert names, "the walker must find the references these outputs make"
        assert {t for _, _, t in names} <= ids | {transform.id}


def test_references_come_from_typed_fields_nested_or_not() -> None:
    records, _ = records_of(
        McapAdapter(chunk_bytes=512), (FIXTURES / "mcap" / "robot.mcap").read_bytes()
    )
    fields = {(r.kind, f) for r in records for f, _ in named(r)}
    assert ("stream", "run") in fields and ("stream", "clocks") in fields
    # A Timestamp's domain_id, nested in a field typed Timestamp: found through the type.
    assert ("run", "first") in fields or ("stream", "first") in fields
    config, _ = records_of(ConfigAdapter(), b'{"a": 1, "a": 2}\n')
    found = {(r.kind, f) for r in config for f, _ in named(r)}
    assert ("configuration_value", "snapshot") in found
    assert ("ingest_finding", "records") in found


def test_data_that_looks_like_an_id_is_not_a_reference() -> None:
    """Cells and configuration values are typed as data, never as ids: text that happens to
    read like one names nothing."""
    csv = b"t_ns,ref\n0," + LOOKALIKE.encode() + b"\n"
    tabular, _ = records_of(TabularAdapter(), csv)
    config, _ = records_of(ConfigAdapter(), b'{"ref": "' + LOOKALIKE.encode() + b'"}\n')
    for record in (*tabular, *config):
        assert LOOKALIKE not in {t for _, t in named(record)}
    assert any(LOOKALIKE in repr(r) for r in (*tabular, *config))  # it is there, as data


def test_ids_provenance_and_transforms_are_not_references() -> None:
    records, transform = records_of(ConfigAdapter(), b'{"a": 1, "a": 2}\n')
    for record in records:
        targets = {t for _, t in named(record)}
        assert record.id not in targets and transform.id not in targets
    assert list(named(object())) == [] and list(named(int)) == []
