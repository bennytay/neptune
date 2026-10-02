"""Malformed, hostile and repeated runs of the document mapper (ADR 0003 §7, §8).

One corrupt document is findings, never a failed run: a scan with no text layer, a rotated page, a
form revision no template is registered for, a section that crosses a page, a template that is
ambiguous. Edits to the compiler's own records stand in for documents the fixtures do not hold.
"""

import hashlib
import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from neptune.model.knowledge import Known, NotCovered, Unknown
from neptune.model.provenance import EvidenceRef, Span
from neptune.store.package import IngestPackage, read_files, read_package
from neptune_deploy.lifecycle import (
    DOCUMENT_FINDINGS,
    DOCUMENT_MAPPER_ID,
    DOCUMENT_MAPPER_VERSION,
    DocumentTemplate,
    MappingError,
    TemplateRegistry,
    load_template,
    map_files,
    map_package,
    parse_mapping,
    parse_template,
    preset,
)
from neptune_deploy.lifecycle.cli import main

FIXTURES = Path(__file__).parent / "fixtures" / "documents"
TEMPLATES = FIXTURES / "templates"
RISK = TEMPLATES / "risk_amr_iso3691_4.json"


def _base(name: str) -> IngestPackage:
    return read_package(FIXTURES / "packages" / name)


def _templates() -> list[DocumentTemplate]:
    return list(TemplateRegistry.from_paths([TEMPLATES]).templates())


def _mapped(base: IngestPackage, templates: list[DocumentTemplate] | None = None) -> IngestPackage:
    return read_files(map_files(base, templates=templates or _templates()))


def _of(package: IngestPackage, kind: str) -> list[Any]:
    return [r for r in package.records if r.kind == kind]


def _codes(package: IngestPackage) -> dict[str, list[Any]]:
    out: dict[str, list[Any]] = {}
    for finding in _of(package, "ingest_finding"):
        out.setdefault(finding.code.split(".")[-1], []).append(finding)
    return out


def _with(base: IngestPackage, change: Callable[[Any], Any]) -> IngestPackage:
    """The base with each record changed; not re-verified, as the edits stand in for documents."""
    return replace(base, records=tuple(change(r) for r in base.records))


def _retext(old: str, new: str) -> Callable[[Any], Any]:
    """Replace a block's text, and its cited span with it: the compiler's own invariant."""

    def change(record: Any) -> Any:
        if record.kind != "document_block" or not isinstance(record.text, Known):
            return record
        if record.text.value != old:
            return record
        evidence = record.provenance.evidence
        span = evidence.locator[-1]
        assert isinstance(span, Span)
        moved = EvidenceRef(
            evidence.source, (*evidence.locator[:-1], Span(span.start, span.start + len(new)))
        )
        provenance = replace(record.provenance, evidence=moved)
        return replace(record, text=Known(new, record.text.provenance), provenance=provenance)

    return change


# --- The three malformed documents the issue names ---------------------------------------------


def test_a_scan_with_no_text_layer_is_a_finding_not_a_failure() -> None:
    package = _mapped(_base("malformed"))
    (scan,) = _codes(package)["no_text_layer"]
    assert scan.severity == "warning" and scan.category == "missing"
    # The other documents of the same package are still mapped.
    assert len(_of(package, "risk_assessment")) == 1
    assert len(_of(package, "incident_record")) == 1
    scanned = next(
        d
        for d in _of(_base("malformed"), "document_record")
        if d.provenance.evidence == scan.subject
    )
    assert scanned.title.value == "Scan 0042"
    assert not any(
        isinstance(b.text, Known)
        for b in _of(_base("malformed"), "document_block")
        if b.document == scanned.id
    )


def test_a_rotated_page_changes_neither_the_values_nor_their_citations_only_the_receipt() -> None:
    clean = _mapped(_base("warehouse_amr"))
    package = _mapped(_base("malformed"))
    (rotated,) = _codes(package)["page_rotated"]
    assert rotated.details == {"degrees": [90], "pages": [1]} and rotated.severity == "info"
    (risk,) = _of(package, "risk_assessment")
    (reference,) = _of(clean, "risk_assessment")
    assert risk.provenance.evidence != reference.provenance.evidence  # another file's bytes
    assert [[s.value.value for s in h.scores] for h in risk.hazards] == [
        [s.value.value for s in h.scores] for h in reference.hazards
    ]

    # Spans are the extracted text's, so a rotation moves none of them.
    def spans(record: Any) -> list[Any]:
        return [h.hazard.provenance.evidence.locator for h in record.hazards]

    assert spans(risk) == spans(reference)


def test_a_form_revision_no_template_is_registered_for_gets_no_record_and_no_guess() -> None:
    package = _mapped(_base("malformed"))
    (mismatch,) = _codes(package)["template_version_mismatch"]
    assert mismatch.severity == "warning"
    assert mismatch.details["templates"] == [
        {"template": "risk.amr_iso3691_4", "version": "2", "found": "3"}
    ]
    assert len(_of(package, "risk_assessment")) == 1  # only the rotated one, revision 2
    revision3 = next(
        d
        for d in _of(_base("malformed"), "document_record")
        if d.provenance.evidence == mismatch.subject
    )
    assert all(
        r.provenance.evidence != revision3.provenance.evidence
        for r in _of(package, "risk_assessment")
    )


def test_registering_the_revision_makes_the_same_document_match_that_version() -> None:
    newer = json.loads(RISK.read_text())
    newer["version"] = "3"
    newer["form"]["version"] = "3"
    newer["fields"]["method"] = {"label": "Method", "required": True}
    registry = [*_templates(), parse_template(json.dumps(newer).encode())]
    package = _mapped(_base("malformed"), registry)
    assert "template_version_mismatch" not in _codes(package)
    versions = sorted(
        f.details["version"]
        for f in _codes(package)["template_matched"]
        if f.details["template"] == "risk.amr_iso3691_4"
    )
    assert versions == ["2", "3"]
    assert len(_of(package, "risk_assessment")) == 2


def test_a_section_that_crosses_a_page_cannot_be_one_citation_so_its_field_is_unknown() -> None:
    package = _mapped(_base("malformed"))
    (finding,) = _codes(package)["section_not_contiguous"]
    assert finding.details["reference"] == "Description" and finding.severity == "warning"
    (incident,) = _of(package, "incident_record")
    assert isinstance(incident.description, Unknown)
    # The section that is one span is still read verbatim.
    assert isinstance(incident.root_cause, Known)
    assert incident.root_cause.value.startswith("The rack face was 40 mm")


# --- Matching ----------------------------------------------------------------------------------


def test_two_templates_matching_one_document_pick_neither() -> None:
    twin = json.loads(RISK.read_text())
    twin["id"] = "risk.amr_twin"
    package = _mapped(
        _base("warehouse_amr"), [*_templates(), parse_template(json.dumps(twin).encode())]
    )
    (finding,) = _codes(package)["template_ambiguous"]
    assert finding.severity == "error"
    assert finding.details["templates"] == ["risk.amr_iso3691_4@2", "risk.amr_twin@2"]
    assert _of(package, "risk_assessment") == []
    assert len(_of(package, "incident_record")) == 1


def test_a_document_no_template_matches_is_listed_not_dropped() -> None:
    package = _mapped(
        _base("warehouse_amr"), [load_template(TEMPLATES / "commissioning_cell3.json")]
    )
    assert len(_codes(package)["document_unmatched"]) == 2
    assert [
        r
        for r in package.records
        if r.kind.endswith(("record", "baseline", "assessment"))
        and r.kind not in ("transform_record", "document_record")
    ] == []


def test_a_declared_form_that_lacks_required_structure_is_a_finding_that_names_it() -> None:
    template = json.loads(RISK.read_text())
    template["requires"]["labels"].append("Hull number")
    package = _mapped(_base("warehouse_amr"), [parse_template(json.dumps(template).encode())])
    (finding,) = _codes(package)["template_structure_missing"]
    assert finding.details["templates"][0]["missing"] == ["label Hull number"]
    assert _of(package, "risk_assessment") == []


def test_an_untagged_document_has_no_tables_so_a_template_that_requires_one_does_not_match() -> (
    None
):
    """The compiler does not guess tables in an untagged PDF (root ADR 0038 section 5); the
    mapper sees labels and sections only, and says the document was not matched."""
    base = _base("manipulator_cell")
    kept = tuple(r for r in base.records if r.kind not in ("structured_table", "structured_record"))
    package = _mapped(replace(base, records=kept))
    assert _of(package, "risk_assessment") == [] and _of(package, "commissioning_baseline") == []
    assert len(_codes(package)["document_unmatched"]) == 2


# --- Values the document leaves blank, repeats or writes unreadably ---------------------------


def test_a_blank_required_value_is_unknown_with_a_finding() -> None:
    package = _mapped(
        _with(_base("warehouse_amr"), _retext("Occurred at: 2026-04-02 14:07", "Occurred at:"))
    )
    (incident,) = _of(package, "incident_record")
    assert isinstance(incident.occurred, Unknown)
    (blank,) = _codes(package)["value_blank"]
    assert blank.details["reference"] == "Occurred at"


def test_a_date_that_does_not_read_under_its_declared_format_is_unknown_not_guessed() -> None:
    package = _mapped(
        _with(
            _base("warehouse_amr"), _retext("Assessed on: 2026-03-12", "Assessed on: 12 March 2026")
        )
    )
    (risk,) = _of(package, "risk_assessment")
    assert isinstance(risk.assessed, Unknown)
    (finding,) = _codes(package)["value_unreadable"]
    assert finding.details["reference"] == "Assessed on"


def test_a_label_the_document_shows_twice_is_unknown_not_the_first_one() -> None:
    package = _mapped(_with(_base("warehouse_amr"), _retext("Zone: Z3", "Site: HH-DC9")))
    (incident,) = _of(package, "incident_record")
    assert isinstance(incident.site, Unknown)
    (finding,) = _codes(package)["label_repeated"]
    assert finding.details["reference"] == "Site" and len(finding.related) == 1


def test_two_documents_stating_one_identifier_are_both_kept_with_a_finding() -> None:
    package = _mapped(
        _with(
            _base("inspection_quadruped"),
            _retext("Work order: WO-QD-6107", "Work order: WO-QD-5521"),
        )
    )
    events = _of(package, "maintenance_event")
    assert len(events) == 2
    (finding,) = _codes(package)["identifier_repeated"]
    assert finding.records == (finding.records[0],) and finding.records[0] in {e.id for e in events}
    assert len(finding.related) == 1


def test_list_findings_are_one_per_value_and_name_the_record_and_the_field() -> None:
    blank = _mapped(_with(_base("warehouse_amr"), _retext("Assets: PAL-5521; RACK-14B", "Assets:")))
    (incident,) = _of(blank, "incident_record")
    assert incident.assets == ()
    (finding,) = _codes(blank)["list_cell_blank"]
    assert finding.details["field"] == "/assets" and finding.records == (incident.id,)
    edits = _retext("Assets: PAL-5521; RACK-14B", "Assets: PAL-5521; ; PAL-5521")
    package = _mapped(_with(_base("warehouse_amr"), edits))
    (incident,) = _of(package, "incident_record")
    assert [a.value.value for a in incident.assets] == ["PAL-5521"]
    codes = _codes(package)
    (empty,) = codes["list_part_empty"]
    (again,) = codes["list_id_repeated"]
    assert empty.details["field"] == again.details["field"] == "/assets"
    assert empty.records == again.records == (incident.id,)


def test_a_table_with_no_rows_gives_an_empty_list_and_a_statement_with_none_gives_none() -> None:
    base = _base("warehouse_amr")
    table = next(
        t
        for t in _of(base, "structured_table")
        if isinstance(t.header, Known) and t.header.value[0] == "Hazard"
    )
    kept = tuple(
        r for r in base.records if not (r.kind == "structured_record" and r.table == table.id)
    )
    package = _mapped(replace(base, records=kept))
    (risk,) = _of(package, "risk_assessment")
    assert risk.hazards == ()
    assert isinstance(risk.configuration, NotCovered)


# --- Wrapped paragraphs, repeated forms, claimed tables --------------------------------------


def test_a_label_on_a_later_line_of_a_wrapped_block_is_read_from_its_own_line() -> None:
    edits = _retext("Assessed on: 2026-03-12", "Note: none\nAssessed on: 2026-03-12")
    package = _mapped(_with(_base("warehouse_amr"), edits))
    (risk,) = _of(package, "risk_assessment")
    (plain,) = _of(_mapped(_base("warehouse_amr")), "risk_assessment")
    assert isinstance(risk.assessed, Known) and isinstance(plain.assessed, Known)
    assert risk.assessed.value.ticks == plain.assessed.value.ticks
    assert "label_absent" not in _codes(package)
    # The form's own labels may share one block too: the template still matches.
    both = _with(
        _with(_base("warehouse_amr"), _retext("Revision: 2", "Revision: 2\nForm: RA-3691-AMR")),
        _retext("Form: RA-3691-AMR", "Note: none"),
    )
    assert len(_of(_mapped(both), "risk_assessment")) == 1


def test_a_wrapped_value_a_value_under_its_label_and_a_stray_line_are_never_dropped() -> None:
    wrapped = "Location: Aisle 14, rack face B, between the\ncharging dock and the fire door"
    base = _with(_base("warehouse_amr"), _retext("Location: Aisle 14, rack face B", wrapped))
    package = _mapped(base)
    (incident,) = _of(package, "incident_record")
    # The value is every line up to the next label line, joined by one space, cited by its lines.
    assert (
        incident.location.value
        == "Aisle 14, rack face B, between the charging dock and the fire door"
    )
    (note,) = _codes(package)["label_value_wrapped"]
    spans = [r.locator[-1] for r in note.related]
    assert len(spans) == 2 and spans[0].end < spans[1].start
    assert incident.location.provenance.evidence.locator[-1] == Span(spans[0].start, spans[1].end)
    assert note.records == (incident.id,) and note.details["reference"] == "Location"
    assert "text_unread" not in _codes(package)

    under = _with(
        _base("warehouse_amr"),
        _retext("Approved by: Site safety lead", "Approved by:\nSite safety lead"),
    )
    (risk,) = _of(_mapped(under), "risk_assessment")
    authority = risk.approval.authority
    assert isinstance(authority, Known) and authority.value == "Site safety lead"
    cited = authority.provenance.evidence  # type: ignore[union-attr]
    assert cited.locator[-1].end - cited.locator[-1].start == len("Site safety lead")

    stray = _with(
        _base("warehouse_amr"),
        _retext("Assessed on: 2026-03-12", "Stray line here\nAssessed on: 2026-03-12"),
    )
    package = _mapped(stray)
    (unread,) = _codes(package)["text_unread"]
    # The line no label took is listed with its own span, not the block's.
    (line,) = unread.related
    assert line.locator[-1].end - line.locator[-1].start == len("Stray line here")
    assert unread.details["count"] == 1
    assert "label_value_wrapped" not in _codes(package)


def test_a_block_of_thousands_of_continuation_lines_is_a_finding_of_bounded_size() -> None:
    lines = [f"continues {n}" for n in range(5000)]
    huge = "Location: Aisle 14\n" + "\n".join(lines)
    base = _with(_base("warehouse_amr"), _retext("Location: Aisle 14, rack face B", huge))
    files = map_files(base, templates=_templates())
    package = read_files(files)
    (note,) = _codes(package)["label_value_wrapped"]
    assert len(note.related) == 10 and note.details["lines"] == 5001
    findings = len(files["records/ingest_finding.jsonl"])
    plain = len(
        map_files(_base("warehouse_amr"), templates=_templates())["records/ingest_finding.jsonl"]
    )
    assert findings - plain < 4096  # the finding does not grow with the block


def test_the_form_id_is_an_identifier_so_the_line_after_it_is_not_part_of_it() -> None:
    edit = _retext("Form: RA-3691-AMR", "Form: RA-3691-AMR\nIssued by: Safety office")
    package = _mapped(_with(_base("warehouse_amr"), edit))
    assert len(_of(package, "risk_assessment")) == 1  # not unmatched by "RA-3691-AMR Issued by..."
    (unread,) = _codes(package)["text_unread"]
    (line,) = unread.related  # the line after the form id is left for `text_unread`, with its span
    assert line.locator[-1].end - line.locator[-1].start == len("Issued by: Safety office")


def test_the_next_label_line_ends_a_value() -> None:
    two = _retext("Site: HH-DC2", "Site: HH-DC2\nOccurred at: 2026-04-02 14:07")
    gone = _retext("Occurred at: 2026-04-02 14:07", "Note: none")
    package = _mapped(_with(_with(_base("warehouse_amr"), two), gone))
    (incident,) = _of(package, "incident_record")
    assert incident.site.value.value == "HH-DC2"  # the next label line is not a wrapped value
    assert isinstance(incident.occurred, Known)
    wrapped = _codes(package).get("label_value_wrapped", [])
    assert all(incident.id not in f.records for f in wrapped)


def test_a_label_said_again_with_the_same_value_is_that_value_and_with_another_is_unknown() -> None:
    same = _mapped(_with(_base("warehouse_amr"), _retext("Zone: Z3", "Site: HH-DC2")))
    (incident,) = _of(same, "incident_record")
    assert isinstance(incident.site, Known) and "label_repeated" not in _codes(same)
    other = _mapped(_with(_base("warehouse_amr"), _retext("Zone: Z3", "Site: HH-DC9")))
    (incident,) = _of(other, "incident_record")
    assert isinstance(incident.site, Unknown) and "label_repeated" in _codes(other)


def test_rows_are_only_a_top_level_field_of_a_template() -> None:
    text = RISK.read_text()
    template = json.loads(text)
    nested = json.loads(text)
    nested["fields"]["hazards"] = [
        {
            "hazard": {"label": "Hazard"},
            "scores": {"rows": "hazards", "each": {"column": "Severity"}},
        }
    ]
    with pytest.raises(MappingError, match="expected a list of parts"):
        parse_template(json.dumps(nested).encode())
    inside = json.loads(text)
    inside["fields"]["approval"] = {"decision": {"rows": "hazards", "each": {}}}
    with pytest.raises(MappingError):
        parse_template(json.dumps(inside).encode())
    assert parse_template(json.dumps(template).encode()).id == "risk.amr_iso3691_4"


def test_a_form_shown_twice_is_that_form_if_every_statement_agrees_and_not_otherwise() -> None:
    agree = _mapped(
        _with(_base("warehouse_amr"), _retext("Reviewer: R. Okafor", "Form: RA-3691-AMR"))
    )
    assert len(_of(agree, "risk_assessment")) == 1
    differ = _mapped(
        _with(_base("warehouse_amr"), _retext("Reviewer: R. Okafor", "Form: RA-OTHER"))
    )
    assert _of(differ, "risk_assessment") == []
    assert "document_unmatched" in _codes(differ)
    two = _mapped(_with(_base("warehouse_amr"), _retext("Reviewer: R. Okafor", "Revision: 3")))
    assert _of(two, "risk_assessment") == []
    (finding,) = _codes(two)["template_version_mismatch"]
    assert "found" not in finding.details["templates"][0]


def test_findings_about_a_table_row_of_a_document_name_the_document_not_the_table() -> None:
    def garble(record: Any) -> Any:
        if record.kind != "structured_record":
            return record
        cells = tuple(
            replace(c, value="yesterday")
            if isinstance(c, Known) and isinstance(c.value, str) and c.value[:2] == "20"
            else c
            for c in record.cells
        )
        return replace(record, cells=cells)

    package = _mapped(_with(_base("manipulator_cell"), garble))
    documents = {d.id for d in _of(_base("manipulator_cell"), "document_record")}
    unreadable = _codes(package)["value_unreadable"]
    assert any(f.details["field"].startswith("/tests/") for f in unreadable)
    for finding in unreadable:
        assert finding.details["document"] in documents


def test_a_table_of_a_matched_document_is_not_read_again_by_a_mapping_file() -> None:
    mapping = parse_mapping(
        json.dumps(
            {
                "schema": "neptune-deploy.lifecycle-mapping/1",
                "id": "pdf.tests",
                "version": "1",
                "rules": [
                    {
                        "id": "test",
                        "kind": "risk_assessment",
                        "requires": ["Test", "Result"],
                        "fields": {"identifiers": [{"column": "Test", "namespace": "t.test"}]},
                    }
                ],
            }
        ).encode()
    )
    base = _base("manipulator_cell")
    alone = read_files(map_files(base, [mapping]))
    assert len(_of(alone, "risk_assessment")) > 1  # the table matches the mapping by itself
    both = read_files(map_files(base, [mapping], _templates()))
    assert len(_of(both, "risk_assessment")) == 1  # only the template's: its table is claimed


# --- Lineage, determinism, immutability -------------------------------------------------------


def _tree(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


@pytest.mark.parametrize(
    "name", ["warehouse_amr", "manipulator_cell", "inspection_quadruped", "malformed"]
)
def test_the_same_package_and_templates_give_byte_identical_output_in_any_template_order(
    name: str,
) -> None:
    base = _base(name)
    forward = map_files(base, templates=_templates())
    backward = map_files(base, templates=list(reversed(_templates())))
    assert forward == backward
    assert forward == map_files(_base(name), templates=_templates())


def test_the_base_package_is_never_changed() -> None:
    root = FIXTURES / "packages" / "warehouse_amr"
    before = _tree(root)
    map_files(read_package(root), templates=_templates())
    assert _tree(root) == before


def test_every_record_names_a_transform_that_is_in_the_package_and_cites_the_template() -> None:
    package = _mapped(_base("manipulator_cell"))
    transforms = {r.id: r for r in _of(package, "transform_record")}
    mine = [t for t in transforms.values() if t.adapter_id == DOCUMENT_MAPPER_ID]
    assert {t.adapter_version for t in mine} == {DOCUMENT_MAPPER_VERSION}
    for transform in mine:
        config = transform.config
        assert config["base_package"] == _base("manipulator_cell").id
        assert config["template"]["id"] in {"risk.robot_cell", "commissioning.cell_report"}
        assert config["template_sha256"].startswith("sha256:")
        assert all(u in transforms for u in transform.upstream)
    for record in package.records:
        provenance = getattr(record, "provenance", None)
        if provenance is not None and record.kind not in ("source_artifact", "source_revision"):
            assert provenance.transform in transforms


def test_a_changed_template_is_new_lineage_beside_the_old() -> None:
    base = _base("warehouse_amr")
    changed = json.loads(RISK.read_text())
    changed["description"] = changed["description"] + " Edited."
    old = _mapped(base)
    templates = [t for t in _templates() if t.id != "risk.amr_iso3691_4"]
    new = _mapped(base, [*templates, parse_template(json.dumps(changed).encode())])
    (a,) = _of(old, "risk_assessment")
    (b,) = _of(new, "risk_assessment")
    assert a.id != b.id and a.provenance.transform != b.provenance.transform
    assert [h.hazard.value for h in a.hazards] == [h.hazard.value for h in b.hazards]


def test_each_time_field_has_a_clock_and_the_declared_zone_is_in_the_config_never_applied() -> None:
    package = _mapped(_base("warehouse_amr"))
    domains = _of(package, "timestamp_domain")
    assert {d.field for d in domains} == {"Assessed on", "Approved on", "Occurred at", "Time"}
    for domain in domains:
        assert domain.role.value == "document"
        assert not isinstance(domain.timescale, Known)  # a civil clock: its scale is unknown
    zones = {
        t.config["template"]["id"]: t.config["template"]["zone"]
        for t in _of(package, "transform_record")
        if t.adapter_id == DOCUMENT_MAPPER_ID and "template" in t.config
    }
    assert zones == {"risk.amr_iso3691_4": "Europe/Berlin", "incident.amr_report": "Europe/Berlin"}


# --- Entry points -----------------------------------------------------------------------------


def test_the_command_line_maps_documents_with_a_template_directory(tmp_path: Path) -> None:
    out = tmp_path / "mapped"
    code = main(
        [
            "map",
            str(FIXTURES / "packages" / "manipulator_cell"),
            "--template",
            str(TEMPLATES),
            "--out",
            str(out),
        ]
    )
    assert code == 0
    package = read_package(out)
    assert len(_of(package, "commissioning_baseline")) == 1
    assert len(_of(package, "risk_assessment")) == 1


def test_the_command_line_needs_something_to_map_and_refuses_a_broken_template(
    tmp_path: Path,
) -> None:
    base = str(FIXTURES / "packages" / "manipulator_cell")
    assert main(["map", base, "--out", str(tmp_path / "a")]) == 2
    broken = tmp_path / "broken.json"
    broken.write_text("{}")
    assert main(["map", base, "--template", str(broken), "--out", str(tmp_path / "b")]) == 2


def test_the_library_needs_a_mapping_or_a_template_and_refuses_the_same_template_twice() -> None:
    with pytest.raises(MappingError, match="at least one"):
        map_files(_base("warehouse_amr"))
    twice = [load_template(RISK), load_template(RISK)]
    with pytest.raises(MappingError, match="same template file"):
        map_files(_base("warehouse_amr"), templates=twice)


def test_documents_and_tables_map_in_one_run_and_a_claimed_table_is_not_reported_unmapped() -> None:
    base = _base("manipulator_cell")
    package = read_files(map_files(base, [preset("register_risk")], _templates()))
    kinds = {r.kind for r in package.records}
    assert {"risk_assessment", "commissioning_baseline"} <= kinds
    pdf_tables = {
        t.id
        for t in _of(base, "structured_table")
        if t.provenance.evidence.locator[0].kind == "pdf:structure"
    }
    unmapped = {
        f.details["table"]
        for f in _of(package, "ingest_finding")
        if f.code.endswith("table_unmapped")
    }
    # The tables of the matched PDFs are accounted for by their templates, not reported unmapped.
    assert pdf_tables and not (pdf_tables & unmapped)


def test_every_document_finding_code_is_documented_and_used_with_its_declared_severity() -> None:
    seen: dict[str, Any] = {}
    for name in ("warehouse_amr", "manipulator_cell", "inspection_quadruped", "malformed"):
        for code, findings in _codes(_mapped(_base(name))).items():
            seen.setdefault(code, findings[0])
    assert seen
    for code, finding in seen.items():
        severity, category, _ = DOCUMENT_FINDINGS[code]
        assert (finding.severity, finding.category) == (severity.value, category.value)


def test_the_package_wrote_is_a_real_package_a_fresh_reader_verifies(tmp_path: Path) -> None:
    out = tmp_path / "mapped"
    package_id = map_package(FIXTURES / "packages" / "warehouse_amr", [], out, _templates())
    assert read_package(out).id == package_id
