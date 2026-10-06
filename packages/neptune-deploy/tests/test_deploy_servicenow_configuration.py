"""``servicenow_csv`` 2: a change states the configuration it results in (ADR 0016 §8).

``u_after`` is the change record's ``configuration``, an id under ``servicenow.u_after`` as
written, citing its cell; ``u_before`` stays the change item's ``before`` text, because the change
record kind has no prior-configuration field. A blank ``u_after`` is ``Unknown``, never empty.
"""

from dataclasses import replace
from pathlib import Path
from typing import Any, Final

from neptune.model.ids import LogicalId
from neptune.model.knowledge import Known, Unknown
from neptune.model.provenance import Provenance, RowCell
from neptune.store.package import IngestPackage, read_files, read_package
from neptune_deploy.lifecycle import map_files, preset

BASE: Final = read_package(
    Path(__file__).parent / "fixtures" / "archetypes" / "packages" / "manipulator_cell"
)


def _changes(base: IngestPackage = BASE) -> dict[str, Any]:
    package = read_files(map_files(base, [preset("servicenow_csv")]))
    records = [r for r in package.records if r.kind == "change_record"]
    return {r.identifiers.value[0].value.value: r for r in records}


def test_u_after_is_the_resulting_configuration_and_u_before_the_items_before() -> None:
    changes = _changes()
    controller = changes["CHG0030012"]
    assert controller.configuration.value == LogicalId("servicenow.u_after", "5.6.0")
    (item,) = controller.changes.value
    assert (item.before.value, item.after.value) == ("5.4.2", "5.6.0")
    tool = changes["CHG0030013"]
    assert tool.configuration.value.value == "TCP z=145.5 mm"  # as written, never parsed
    assert isinstance(controller.configuration.provenance, Provenance)
    (cell,) = controller.configuration.provenance.evidence.locator
    assert isinstance(cell, RowCell) and cell.column_name == "u_after"


def test_a_blank_u_after_is_an_unknown_configuration_never_empty() -> None:
    def blank(record: Any) -> Any:
        if record.kind != "structured_record":
            return record
        cells = tuple(
            Unknown(c.provenance) if c == Known("5.6.0", c.provenance) else c for c in record.cells
        )
        return replace(record, cells=cells)

    changes = _changes(replace(BASE, records=tuple(blank(r) for r in BASE.records)))
    assert isinstance(changes["CHG0030012"].configuration, Unknown)
    assert isinstance(changes["CHG0030012"].changes.value[0].after, Unknown)
    assert isinstance(changes["CHG0030013"].configuration, Known)


def test_the_preset_is_version_2_and_maps_deterministically() -> None:
    assert preset("servicenow_csv").version == "2"
    assert map_files(BASE, [preset("servicenow_csv")]) == map_files(
        BASE, [preset("servicenow_csv")]
    )
