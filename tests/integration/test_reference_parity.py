"""Validation and salvage check the same references, read from the model (ADR 0069 §2).

Over every worked example (four platforms, two deployments, every record kind they hold): the
references validation's ``dangling_reference`` checks are exactly those the runtime's salvage
check reads (``model.references.named``), and removing any one referenced record from an example
is reported by validation, naming that record.
"""

import dataclasses
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Final

import pytest

from neptune.identity import canonical_json
from neptune.model.ids import is_external
from neptune.model.kinds import RECORD_KINDS
from neptune.model.references import named
from neptune.validate.engine import Bounds, Context, Inputs
from neptune.validate.rules import dangling_reference, references_checked

pytestmark = pytest.mark.integration

MODEL: Final = Path(__file__).parents[1] / "fixtures" / "model"
EXAMPLES: Final = sorted(p.parent.name for p in MODEL.glob("*/records"))


def records_of(example: str) -> list[Any]:
    found = []
    for path in sorted((MODEL / example / "records").glob("*.jsonl")):
        read = RECORD_KINDS[path.stem][1]
        found += [read(canonical_json.loads(line)) for line in path.read_bytes().splitlines()]
    return found


def context(records: list[Any]) -> Context:
    return Context(SimpleNamespace(records=tuple(records)), Bounds(), Inputs())  # type: ignore[arg-type]


def salvage_view(records: list[Any]) -> set[tuple[str, str, str]]:
    """What the salvage check reads: every record's and finding's references."""
    return {(r.kind, field, target) for r in records for field, target in named(r)}


def test_the_examples_cover_the_kinds_that_reference() -> None:
    assert len(EXAMPLES) == 6
    kinds = {r.kind for example in EXAMPLES for r in records_of(example)}
    referencing = {kind for kind, *_ in (k for e in EXAMPLES for k in salvage_view(records_of(e)))}
    assert len(kinds) >= 30 and len(referencing) >= 15


@pytest.mark.parametrize("example", EXAMPLES)
def test_validation_checks_exactly_what_salvage_checks(example: str) -> None:
    records = records_of(example)
    checked = {(r.kind, field, target) for r, field, target in references_checked(context(records))}
    assert checked and checked == salvage_view(records)


def _holds_id(value: Any) -> bool:
    """Whether a field's value holds a record id: bare, in a tuple, or as a ``Known``'s value."""
    value = getattr(value, "value", value)
    items = value if isinstance(value, tuple) else (value,)
    return any(isinstance(item, str) and item.startswith("rec:sha256:") for item in items)


def test_every_kind_s_typed_references_are_read() -> None:
    """For every record kind in the examples, each top-level field the model types as a record id
    and does not mark external is read by ``named`` whenever it holds one; external ones never."""
    for example in EXAMPLES:
        for record in records_of(example):
            read = {field for field, _ in named(record)}
            for field in dataclasses.fields(record):
                if "RecordId" not in str(field.type) or field.name in ("id", "transform"):
                    continue
                held = _holds_id(getattr(record, field.name))
                if is_external(field):
                    assert field.name not in read, (record.kind, field.name)
                elif held:
                    assert field.name in read, (record.kind, field.name)


@pytest.mark.parametrize("example", EXAMPLES)
def test_removing_any_referenced_record_is_reported_by_validation(example: str) -> None:
    records = records_of(example)
    assert not list(dangling_reference(context(records)))  # every example resolves whole
    targets = sorted({target for _, _, target in salvage_view(records)})
    assert targets
    by_id = {getattr(r, "id", None): r for r in records}
    for target in targets:
        if target not in by_id:
            continue  # named by a record, held by none: the whole example would have said so
        rest = [r for r in records if getattr(r, "id", None) != target]
        reported = {d.details["target"] for d in dangling_reference(context(rest))}
        assert target in reported, (example, by_id[target].kind, target)
