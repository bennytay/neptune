"""Document template files: declared, versioned, and refused before any document is read (ADR 0003).

A template is the operator's declaration, so a wrong one fails loudly with where in it the problem
is. The fixture templates are the real ones the document mapper tests use.
"""

import copy
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from neptune_deploy.lifecycle import (
    TEMPLATE_SCHEMA,
    DocumentTemplate,
    MappingError,
    TemplateRegistry,
    load_template,
    parse_template,
)

TEMPLATES = Path(__file__).parent / "fixtures" / "documents" / "templates"
RISK = TEMPLATES / "risk_amr_iso3691_4.json"


def _document() -> dict[str, Any]:
    document: dict[str, Any] = json.loads(RISK.read_text())
    return document


def _parse(document: dict[str, Any]) -> DocumentTemplate:
    return parse_template(json.dumps(document).encode(), "test")


def test_every_fixture_template_parses_and_carries_its_declaration() -> None:
    registry = TemplateRegistry.from_paths([TEMPLATES])
    assert len(registry) == 5
    kinds = {t.id: t.kind.kind for t in registry.templates()}
    assert kinds == {
        "risk.amr_iso3691_4": "risk_assessment",
        "risk.robot_cell": "risk_assessment",
        "commissioning.cell_report": "commissioning_baseline",
        "incident.amr_report": "incident_record",
        "maintenance.sop_record": "maintenance_event",
    }
    template = load_template(RISK)
    assert template.version == "2"
    assert template.form is not None and template.form.version == "2"
    assert template.sha256.startswith("sha256:")
    assert template.document["schema"] == TEMPLATE_SCHEMA


def test_the_registry_order_is_stable_whatever_the_order_the_files_are_given() -> None:
    files = sorted(TEMPLATES.glob("*.json"))
    forward = TemplateRegistry.from_paths(files).templates()
    backward = TemplateRegistry.from_paths(reversed(files)).templates()
    assert [t.sha256 for t in forward] == [t.sha256 for t in backward]


def test_two_versions_of_one_template_coexist_and_one_version_twice_does_not() -> None:
    newer = _document()
    newer["version"] = "3"
    newer["form"]["version"] = "3"
    registry = TemplateRegistry([load_template(RISK), _parse(newer)])
    assert registry.get("risk.amr_iso3691_4", "2") is not None
    assert registry.get("risk.amr_iso3691_4", "3") is not None
    assert registry.get("risk.amr_iso3691_4", "4") is None
    with pytest.raises(MappingError, match="two templates are"):
        TemplateRegistry([load_template(RISK), load_template(RISK)])


def _edit(change: Callable[[dict[str, Any]], object]) -> dict[str, Any]:
    document = copy.deepcopy(_document())
    change(document)
    return document


BAD: dict[str, tuple[Callable[[dict[str, Any]], object], str]] = {
    "wrong schema": (
        lambda d: d.update(schema="neptune-deploy.document-template/2"),
        "schema must",
    ),
    "unknown kind": (lambda d: d.update(kind="inspection"), "not a lifecycle kind"),
    "no fields": (lambda d: d.pop("fields"), "missing"),
    "unexpected key": (lambda d: d.update(extra=1), "unexpected"),
    "empty formats": (lambda d: d.update(formats=[]), "at least one format"),
    "blank separator": (lambda d: d.update(separator=" "), "separator"),
    "no zone for a time": (
        lambda d: (d.pop("zone"), d["fields"]["assessed"].pop("zone", None)),
        "civil zone",
    ),
    "nothing required": (
        lambda d: (d.pop("form"), d.pop("requires")),
        "name a form, or at least one",
    ),
    "table required but undeclared": (
        lambda d: d["requires"].update(tables=["hazards", "ghosts"]),
        "'ghosts' is not declared",
    ),
    "table declared and unused": (
        lambda d: d["tables"].update(spare=["A", "B"]),
        "'spare' is declared and never used",
    ),
    "empty header": (lambda d: d["tables"].update(hazards=[]), "at least one header cell"),
    "repeated header cell": (
        lambda d: d["tables"].update(hazards=["Hazard", "Hazard"]),
        "repeats an entry",
    ),
    "column the header lacks": (
        lambda d: d["fields"]["hazards"]["each"]["scores"].append({"column": "Cost"}),
        "no header for",
    ),
    "label and section together": (
        lambda d: d["fields"].update(method={"label": "Method", "section": "Method"}),
        "exactly one",
    ),
    "neither label nor section": (lambda d: d["fields"].update(method={}), "exactly one"),
    "column at the top level": (
        lambda d: d["fields"].update(method={"column": "Method"}),
        "unexpected",
    ),
    "label inside the rows": (
        lambda d: d["fields"]["hazards"]["each"].update(hazard={"label": "Hazard"}),
        "unexpected",
    ),
    "section into an id": (
        lambda d: d["fields"].update(site={"section": "Site", "namespace": "x"}),
        "only a text field",
    ),
    "section of ids": (
        lambda d: d["fields"].update(machines=[{"section": "Machines", "namespace": "x"}]),
        "no split",
    ),
    "rows on a list of ids": (
        lambda d: d["fields"].update(machines={"rows": "hazards", "each": {}}),
        "expected a list of cells",
    ),
    "unknown field": (lambda d: d["fields"].update(hazard_count={"label": "N"}), "no fields"),
    "ignored column of an unread table": (
        lambda d: d["ignore"].update(columns={"nothing": ["A"]}),
        "no rows field reading it",
    ),
    "ignored column the header lacks": (
        lambda d: d["ignore"].update(columns={"hazards": ["Cost"]}),
        "no header cells",
    ),
    "a time with a bad format": (
        lambda d: d["fields"]["assessed"].update(format="%d.%m"),
        "format",
    ),
}


@pytest.mark.parametrize("case", sorted(BAD))
def test_a_wrong_template_is_refused_with_where_it_is_wrong(case: str) -> None:
    change, message = BAD[case]
    with pytest.raises(MappingError, match=message):
        _parse(_edit(change))


def test_a_section_lists_statements_one_per_list_item_so_it_takes_no_delimiter() -> None:
    document: dict[str, Any] = json.loads((TEMPLATES / "commissioning_cell3.json").read_text())
    document["fields"]["constraints"] = [{"section": "Constraints", "split": ";"}]
    with pytest.raises(MappingError, match="no split"):
        _parse(document)


def test_a_template_that_is_not_json_or_is_hostile_is_refused() -> None:
    with pytest.raises(MappingError, match="not a JSON document"):
        parse_template(b"{nope", "t")
    with pytest.raises(MappingError, match="not a JSON document"):
        parse_template(b"\xff\xfe", "t")
    with pytest.raises(MappingError, match="repeats"):
        parse_template(b'{"id": "a", "id": "b"}', "t")
    with pytest.raises(MappingError, match="NaN is not JSON"):
        parse_template(b'{"x": NaN}', "t")
    with pytest.raises(MappingError, match="larger than"):
        parse_template(b" " * (1024 * 1024 + 1), "t")
    with pytest.raises(MappingError, match="not a JSON document"):
        parse_template(b"[" * 100_000, "t")
    with pytest.raises(MappingError, match="expected an object"):
        parse_template(b"[]", "t")


def test_a_template_directory_reads_only_its_json_files(tmp_path: Path) -> None:
    (tmp_path / "risk.json").write_bytes(RISK.read_bytes())
    (tmp_path / "notes.txt").write_text("not a template")
    assert len(TemplateRegistry.from_paths([tmp_path])) == 1
    (tmp_path / "broken.json").write_text("{}")
    with pytest.raises(MappingError, match=r"broken\.json"):
        TemplateRegistry.from_paths([tmp_path])
