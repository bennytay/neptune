"""The assertion adapter on assertion files (ADR 0062): records, citations, findings, probing.

The oracles are independent readings: the standard library's ``json`` reads each fixture and
each cited span again, and ``datetime`` computes every ``authored_at`` instant, and the tests
compare them with what the adapter says.
"""

import json
from datetime import UTC, datetime, timedelta
from fractions import Fraction
from pathlib import Path
from typing import Any, Final

import pytest

from neptune.adapters.assertion import DESCRIPTOR, AssertionAdapter
from neptune.adapters.builtin import default_registry
from neptune.adapters.config import ConfigAdapter
from neptune.adapters.contract import PROBE_HEAD_SIZE, SIGNATURE, STRUCTURE, VERIFIED, ProbeHints
from neptune.adapters.harness import SourceOutput, ingest_source
from neptune.discovery.probe import ProbeEngine
from neptune.discovery.reader import BytesReader
from neptune.identity import canonical_json
from neptune.model.assertion import Assertion, AssertionType
from neptune.model.ids import LogicalId
from neptune.model.knowledge import (
    AssertionKind,
    Known,
    KnownAbsent,
    NotApplicable,
    Unknown,
)
from neptune.model.provenance import JsonPointer, Provenance, Span
from neptune.model.reference import TimestampDomain
from neptune.model.time import Epoch, Timescale

FIXTURES: Final = Path(__file__).parents[2] / "fixtures" / "assertion"
EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)
HEADER: Final = '{"format": "neptune.assertions", "version": 1, "assertions": '


def fixture(name: str) -> bytes:
    return (FIXTURES / f"{name}.json").read_bytes()


def run(data: bytes, **config: Any) -> SourceOutput:
    return ingest_source(AssertionAdapter(), BytesReader(data), config)


def assertions_file(*entries: object, raw: str | None = None) -> bytes:
    body = raw if raw is not None else json.dumps(list(entries), ensure_ascii=False)
    return (HEADER + body + "}").encode()


def entry(**changes: object) -> dict[str, object]:
    base: dict[str, object] = {
        "id": {"namespace": "acme.console", "value": "A-1"},
        "assertion_type": "annotate",
        "author": {"namespace": "acme.staff", "value": "j.ng"},
        "authored_at": "2026-09-14T10:30:00Z",
        "scope": [{"namespace": "fleet.asset_tag", "value": "AMR-07"}],
    }
    base.update(changes)
    return {key: value for key, value in base.items() if value is not ...}


def records(output: SourceOutput) -> list[Assertion]:
    found = [r for r in output.records() if isinstance(r, Assertion)]
    return sorted(found, key=lambda r: str(r.provenance.evidence.locator[0]))


def only(output: SourceOutput) -> Assertion:
    (record,) = records(output)
    return record


def domains(output: SourceOutput) -> dict[str, TimestampDomain]:
    return {r.id: r for r in output.records() if isinstance(r, TimestampDomain)}


def codes(output: SourceOutput) -> list[str]:
    return sorted(f.code for f in output.findings())


def cited_text(data: bytes, provenance: object) -> str:
    assert isinstance(provenance, Provenance)
    (step,) = provenance.evidence.locator
    assert isinstance(step, Span)
    return data.decode("utf-8")[step.start : step.end]


# --- The fixtures ------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["cell_baseline", "fleet_identity", "retraction"])
def test_every_entry_is_one_stated_assertion_citing_its_values(name: str) -> None:
    data = fixture(name)
    declared = json.loads(data)["assertions"]
    output = run(data)
    assert codes(output) == []
    found = records(output)
    assert len(found) == len(declared) == len(domains(output))
    for index, (record, source) in enumerate(zip(found, declared, strict=True)):
        assert record.provenance.assertion_kind is AssertionKind.STATED
        assert record.provenance.evidence.locator == (JsonPointer(f"/assertions/{index}"),)
        assert isinstance(record.identifier, Known)
        assert record.identifier.value == LogicalId(**source["id"])
        assert json.loads(cited_text(data, record.identifier.provenance)) == source["id"]
        assert isinstance(record.assertion_type, Known)
        assert record.assertion_type.value == source["assertion_type"]
        assert isinstance(record.author, Known)
        assert record.author.value == LogicalId(**source["author"])
        assert isinstance(record.scope, Known)
        expected = tuple(
            item if isinstance(item, str) else LogicalId(**item) for item in source["scope"]
        )
        assert record.scope.value == expected
        assert json.loads(cited_text(data, record.scope.provenance)) == source["scope"]
        if "payload" in source:
            assert isinstance(record.payload, Known)
            # Kept exactly as written: the cited span and the value are the same text.
            assert record.payload.value == cited_text(data, record.payload.provenance)
            assert json.loads(record.payload.value) == source["payload"]
        else:
            assert record.payload == KnownAbsent(record.provenance)
        for key in ("rationale", "signature"):
            value = getattr(record, key)
            if key in source:
                assert value == Known(source[key], value.provenance)
                assert json.loads(cited_text(data, value.provenance)) == source[key]
            else:
                assert value == KnownAbsent(record.provenance)
        if "ticket" in source:
            assert isinstance(record.ticket, Known)
            assert record.ticket.value == LogicalId(**source["ticket"])
        if "authored_zone" in source:
            assert isinstance(record.authored_zone, Known)
            assert record.authored_zone.value == source["authored_zone"]
        else:
            assert record.authored_zone == Unknown()


def test_a_retraction_names_the_earlier_assertion_and_others_do_not() -> None:
    retract, annotate = records(run(fixture("retraction")))
    assert retract.assertion_type == Known(AssertionType.RETRACT, retract.assertion_type.provenance)  # type: ignore[union-attr]
    assert isinstance(retract.retracts, Known)
    assert retract.retracts.value == LogicalId("dc-north.fleet-console", "ASR-2026-0107")
    assert retract.scope == Known((), retract.scope.provenance)  # type: ignore[union-attr]
    assert annotate.retracts == NotApplicable()
    # The retracted assertion's own file is untouched by reading the retraction.
    (confirmed, _) = records(run(fixture("fleet_identity")))
    assert confirmed.identifier == Known(retract.retracts.value, confirmed.identifier.provenance)  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("text", "resolution", "posix"),
    [
        ("2026-09-14T10:32:05+02:00", Fraction(1), True),
        ("2026-08-27T16:05:00.500Z", Fraction(1, 1000), True),
        ("2026-09-21T08:15:00", Fraction(1), False),
        ("1969-12-31T23:59:59.5-00:00", Fraction(1, 10), True),
        ("2026-08-26", Fraction(86_400), False),
        ("2026-09-14T10:30:00.123456789+05:45", Fraction(1, 10**9), True),
    ],
)
def test_authored_at_is_counted_by_adr_0023_on_a_clock_of_its_own(
    text: str, resolution: Fraction, posix: bool
) -> None:
    data = assertions_file(entry(authored_at=text))
    output = run(data)
    record = only(output)
    assert codes(output) == []
    assert isinstance(record.authored_at, Known)
    stamp = record.authored_at.value
    (domain,) = domains(output).values()
    assert stamp.domain_id == domain.id
    assert domain.resolution == Known(resolution)
    assert domain.epoch == Known(Epoch.UNIX)
    assert domain.timescale == (Known(Timescale.POSIX) if posix else Unknown())
    assert domain.field == "authored_at" and domain.scope == ("/assertions/0",)
    # The oracle: datetime's own reading, exact to the microsecond, on UTC for an instant and on
    # the civil clock otherwise; digits past the microsecond are added exactly.
    head, _, rest = text.partition(".")
    digits = rest[: len(rest) - len(rest.lstrip("0123456789"))]
    zone = rest[len(digits) :] if rest else ""
    parsed = datetime.fromisoformat(f"{head}.{digits[:6]}{zone}" if digits else text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    micros = (parsed - EPOCH) // timedelta(microseconds=1)
    expected = Fraction(micros, 10**6) + Fraction(int(digits[6:] or "0"), 10**9)
    assert Fraction(stamp.ticks) * resolution == expected
    assert cited_text(data, record.authored_at.provenance) == f'"{text}"'


# --- Malformed entries -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("changes", "field", "code"),
    [
        ({"id": ...}, "identifier", "assertion.missing_field"),
        ({"id": "A-1"}, "identifier", "assertion.invalid_value"),
        ({"id": {"namespace": "Bad Space", "value": "x"}}, "identifier", "assertion.invalid_value"),
        (
            {"id": {"namespace": "a", "value": "x", "extra": 1}},
            "identifier",
            "assertion.invalid_value",
        ),
        ({"author": None}, "author", "assertion.missing_field"),
        ({"assertion_type": "merge"}, "assertion_type", "assertion.invalid_value"),
        ({"assertion_type": 3}, "assertion_type", "assertion.invalid_value"),
        ({"authored_at": "14/09/2026 10:30"}, "authored_at", "assertion.invalid_value"),
        ({"authored_at": "2026-02-30T10:30:00Z"}, "authored_at", "assertion.invalid_value"),
        ({"authored_at": "2026-09-14T24:00:00Z"}, "authored_at", "assertion.invalid_value"),
        ({"authored_at": "2026-09-14T10:30:00+24:00"}, "authored_at", "assertion.invalid_value"),
        ({"authored_at": "2026-09-14t10:30:00z"}, "authored_at", "assertion.invalid_value"),
        (
            {"authored_at": "9999-12-31T23:59:59.999999999Z"},
            "authored_at",
            "assertion.invalid_value",
        ),
        ({"authored_zone": "+02:00"}, "authored_zone", "assertion.invalid_value"),
        ({"authored_zone": "-"}, "authored_zone", "assertion.invalid_value"),
        ({"authored_at": "2016-12-31T23:59:60Z"}, "authored_at", "assertion.value_not_read"),
        ({"scope": ["rec:" + "x" * 300]}, "scope", "assertion.invalid_value"),
        ({"scope": ...}, "scope", "assertion.missing_field"),
        ({"scope": {"a": 1}}, "scope", "assertion.invalid_value"),
        ({"scope": ["AMR-07"]}, "scope", "assertion.invalid_value"),
        ({"rationale": "   "}, "rationale", "assertion.missing_field"),
        ({"rationale": 7}, "rationale", "assertion.invalid_value"),
        ({"ticket": "FLEET-1"}, "ticket", "assertion.invalid_value"),
    ],
)
def test_a_value_that_cannot_be_read_is_unknown_with_a_finding(
    changes: dict[str, object], field: str, code: str
) -> None:
    output = run(assertions_file(entry(**changes)))
    record = only(output)
    assert isinstance(getattr(record, field), Unknown)
    assert codes(output) == [code]
    (finding,) = output.findings()
    assert finding.records == (record.id,)


def test_optional_parts_left_out_or_null_are_known_absent() -> None:
    record = only(run(assertions_file(entry(payload=None, ticket=None))))
    for field in ("payload", "rationale", "signature", "ticket"):
        assert isinstance(getattr(record, field), KnownAbsent)
    assert record.authored_zone == Unknown()


def test_a_retract_without_a_target_and_a_target_without_a_retract() -> None:
    output = run(assertions_file(entry(assertion_type="retract")))
    assert isinstance(only(output).retracts, Unknown)
    assert codes(output) == ["assertion.missing_field"]
    target = {"namespace": "acme.console", "value": "A-0"}
    output = run(assertions_file(entry(retracts=target)))
    assert only(output).retracts == NotApplicable()
    assert codes(output) == ["assertion.retracts_not_applicable"]
    # When the type is not read, a stated target is kept as stated.
    output = run(assertions_file(entry(assertion_type="merge", retracts=target)))
    assert only(output).retracts == Known(LogicalId(**target), only(output).retracts.provenance)  # type: ignore[union-attr]


def test_a_null_or_repeated_target_is_quiet_where_it_cannot_apply() -> None:
    output = run(assertions_file(entry(retracts=None)))
    assert only(output).retracts == NotApplicable()
    assert codes(output) == []
    output = run(assertions_file(entry(assertion_type="merge", retracts=None)))
    assert isinstance(only(output).retracts, Unknown)
    assert codes(output) == ["assertion.invalid_value"]
    target = '{"namespace": "a", "value": "0"}'
    raw = json.dumps([entry()])[:-2] + f', "retracts": {target}, "retracts": {target}}}]'
    output = run(assertions_file(raw=raw))
    assert only(output).retracts == NotApplicable()
    assert codes(output) == ["assertion.retracts_not_applicable"]


def test_a_value_too_long_to_hold_inside_a_scope_or_an_id_is_not_read() -> None:
    long_id = {"namespace": "fleet.asset_tag", "value": "x" * 200}
    for changes in ({"scope": [long_id]}, {"author": long_id}):
        output = run(assertions_file(entry(**changes)), max_scalar_length=100)
        assert codes(output) == ["assertion.value_not_read"]


def test_a_negative_limit_reads_nothing_and_says_so() -> None:
    output = run(assertions_file(entry(), entry()), max_assertions=-1)
    assert records(output) == []
    (finding,) = output.findings()
    assert finding.code == "assertion.too_many_assertions"
    assert finding.details == {"assertions": 2, "max_assertions": 0}


def test_a_repeated_key_chooses_nothing() -> None:
    raw = (
        '[{"id": {"namespace": "a", "value": "1"}, "assertion_type": "annotate",'
        ' "assertion_type": "retract", "author": {"namespace": "a", "value": "b"},'
        ' "authored_at": "2026-01-01", "scope": []}]'
    )
    output = run(assertions_file(raw=raw))
    assert isinstance(only(output).assertion_type, Unknown)
    assert isinstance(only(output).retracts, Unknown)
    assert codes(output) == ["assertion.duplicate_key"]


def test_unknown_keys_are_reported_and_not_read() -> None:
    data = assertions_file(entry(ledger_time="2026-09-14T10:31:00Z", confidence=0.9))
    output = run(data)
    only(output)
    assert codes(output) == ["assertion.unknown_key"]
    (finding,) = output.findings()
    assert finding.details["keys"] == ["confidence", "ledger_time"]


def test_entries_that_are_not_objects_are_skipped_and_the_rest_read() -> None:
    output = run(assertions_file(entry(), 7, "x", entry(id={"namespace": "a", "value": "2"})))
    assert len(records(output)) == 2
    assert codes(output) == ["assertion.not_an_assertion"] * 2


@pytest.mark.parametrize(
    ("data", "code"),
    [
        (b"[]", "assertion.not_assertions"),
        (b'{"format": "neptune.assertions", "version": 1}', "assertion.not_assertions"),
        (b'{"format": "other", "version": 1, "assertions": []}', "assertion.not_assertions"),
        (
            b'{"format": "neptune.assertions", "format": "neptune.assertions", "version": 1,'
            b' "assertions": []}',
            "assertion.not_assertions",
        ),
        (
            b'{"format": "neptune.assertions", "version": 1, "assertions": {}}',
            "assertion.not_assertions",
        ),
        (
            b'{"format": "neptune.assertions", "version": 2, "assertions": []}',
            "assertion.version_unsupported",
        ),
        (
            b'{"format": "neptune.assertions", "version": "1", "assertions": []}',
            "assertion.version_unsupported",
        ),
        (
            b'{"format": "neptune.assertions", "version": 1, "assertions": [',
            "assertion.syntax_error",
        ),
        (b"", "assertion.syntax_error"),  # not JSON: the format has no empty file
        (b"  \n", "assertion.syntax_error"),
        (b'{"format": "neptune.assertions"\xff}', "assertion.invalid_encoding"),
    ],
)
def test_a_file_that_is_not_a_readable_assertion_file_is_one_finding(
    data: bytes, code: str
) -> None:
    output = run(data)
    assert records(output) == []
    assert codes(output) == [code]


# --- Hostile input -----------------------------------------------------------------------------


def test_limits_stop_reading_with_findings() -> None:
    many = assertions_file(*(entry(id={"namespace": "a", "value": str(i)}) for i in range(5)))
    output = run(many, max_assertions=3)
    assert len(records(output)) == 3
    assert codes(output) == ["assertion.too_many_assertions"]
    assert codes(run(many, max_bytes=100)) == ["assertion.too_large"]
    deep = assertions_file(entry(payload=json.loads("[" * 80 + "]" * 80)))
    assert codes(run(deep)) == ["assertion.too_deep"]
    long_text = assertions_file(entry(rationale="x" * 200))
    output = run(long_text, max_scalar_length=100)
    assert isinstance(only(output).rationale, Unknown)
    assert codes(output) == ["assertion.value_not_read"]


def test_a_surrogate_escape_and_nan_are_findings_not_crashes() -> None:
    raw = json.dumps([entry(rationale="placeholder", payload={"n": 1})])
    raw = raw.replace('"placeholder"', '"\\ud800"').replace('{"n": 1}', '{"n": NaN}')
    output = run(assertions_file(raw=raw))
    record = only(output)
    assert isinstance(record.rationale, Unknown)
    assert record.payload == Known('{"n": NaN}', record.payload.provenance)  # type: ignore[union-attr]
    assert codes(output) == ["assertion.nonstandard_json", "assertion.value_not_read"]


def test_utf16_with_a_byte_order_mark_cites_code_points() -> None:
    text = assertions_file(entry(rationale="Größe ✓")).decode()
    output = run(b"\xff\xfe" + text.encode("utf-16-le"))
    record = only(output)
    assert record.rationale == Known("Größe ✓", record.rationale.provenance)  # type: ignore[union-attr]
    assert cited_text(text.encode(), record.rationale.provenance) == '"Größe ✓"'


# --- Determinism and the contract --------------------------------------------------------------


def test_output_is_byte_identical_across_runs() -> None:
    data = fixture("fleet_identity")

    def lines(output: SourceOutput) -> list[bytes]:
        return [canonical_json.dumps(r.to_json()) for r in output.package_records()]

    assert lines(run(data)) == lines(run(data))
    changed = run(data, max_assertions=99)
    assert lines(changed)[1:] != lines(run(data))[1:]  # a setting is part of the transform


def test_the_descriptor_documents_every_code_it_can_emit() -> None:
    declared = {code.name for code in DESCRIPTOR.finding_codes}
    assert {"assertion.not_assertions", "assertion.missing_field"} <= declared
    assert DESCRIPTOR.record_kinds == ("assertion", "timestamp_domain")


# --- Probing -----------------------------------------------------------------------------------


def probe(data: bytes) -> tuple[float, list[str]]:
    result = AssertionAdapter().probe(data[:PROBE_HEAD_SIZE], ProbeHints("x.json", len(data)))
    return result.confidence, [reason.code for reason in result.reasons]


def test_probe_claims_by_content_and_beats_the_config_adapter() -> None:
    data = fixture("cell_baseline")
    assert probe(data) == (VERIFIED, ["assertion.format"])
    config = ConfigAdapter().probe(data, ProbeHints("x.json", len(data)))
    assert config.confidence == STRUCTURE
    for name in ("cell_baseline.json", "notes.txt", ""):
        chosen = ProbeEngine(default_registry()).probe(BytesReader(data), name)
        assert chosen.adapter == "assertion"


def test_probe_claims_a_long_or_broken_file_by_its_marker_only() -> None:
    long = assertions_file(*(entry(rationale="x" * 1000) for _ in range(80)))
    assert len(long) > PROBE_HEAD_SIZE
    assert probe(long) == (SIGNATURE, ["assertion.marker"])
    broken = assertions_file(raw="[{")
    assert probe(broken) == (SIGNATURE, ["assertion.marker"])
    # The format must be the root object's: a nested pair in a long config is not a claim.
    nested = b'{"plugins": [{"format": "neptune.assertions"}], "pad": "' + b"x" * 70_000 + b'"}'
    assert probe(nested)[0] == 0.0
    chosen = ProbeEngine(default_registry()).probe(BytesReader(nested), "x.json")
    assert chosen.adapter != "assertion"
    escaped = b'{"note": "\\"format\\": \\"neptune.assertions\\"", "pad": "' + b"x" * 70_000
    assert probe(escaped)[0] == 0.0


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b'{"format": "json", "version": 1}',
        b'{"settings": {"format": "neptune.assertions"}}',
        b"[1, 2, 3]",
        b"format: neptune.assertions\n",
        b"\xff\xfe\xff",
    ],
)
def test_probe_declines_what_is_not_an_assertion_file(data: bytes) -> None:
    assert probe(data)[0] == 0.0
