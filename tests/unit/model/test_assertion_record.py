"""The assertion record (ADR 0062): shape, strict reader, the schema, and what it refuses.

MVL-183 acceptance: an assertion is stated evidence with author, time and scope; a retraction
names an earlier assertion and changes nothing; inferred provenance is refused; the kind is from
schema version that added it on (``ASSERTION_SINCE``) and leaves older packages' bytes alone.
"""

from dataclasses import replace
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from neptune.derived.provenance import InferredProvenance
from neptune.identity import canonical_json
from neptune.identity.hashing import content_id
from neptune.identity.provenance import evidence_record_id, transform_record
from neptune.model.assertion import (
    ASSERTION_SINCE,
    IANA_ZONE_MAX,
    Assertion,
    AssertionType,
    assertion_from_json,
    is_iana_zone,
)
from neptune.model.ids import LogicalId, RecordId
from neptune.model.kinds import KIND_SINCE, RECORD_KINDS, kinds_at, package_version
from neptune.model.knowledge import (
    Ambiguous,
    AssertionKind,
    Candidate,
    Known,
    KnownAbsent,
    NotApplicable,
    NotCovered,
    Unknown,
)
from neptune.model.provenance import EvidenceRef, JsonPointer, Provenance, Span
from neptune.model.record import SCHEMA_VERSION, Family, SchemaVersionError
from neptune.model.schema import canonical_schema
from neptune.model.time import Timestamp

FILE = b'{"format": "neptune.assertions", "version": 1, "assertions": []}'
SOURCE = content_id(FILE)
ADAPTER = transform_record(adapter_id="assertion", adapter_version="0.1.0", config={})
VALIDATOR = Draft202012Validator(canonical_schema())
CLOCK = evidence_record_id(
    "timestamp_domain", EvidenceRef(SOURCE, (JsonPointer("/assertions/0/authored_at"),)), ADAPTER
)
TAG = LogicalId("fleet.asset_tag", "AMR-07")
SERIAL = LogicalId("vda5050.serial_number", "OTTO-1500-22871")
EARLIER = LogicalId("dc-north.fleet-console", "ASR-2026-0107")
RECORD = RecordId("rec:sha256:" + "ab" * 32)


def cite(start: int, end: int) -> Provenance:
    return Provenance(EvidenceRef(SOURCE, (Span(start, end),)), ADAPTER.id, AssertionKind.STATED)


def at(index: int = 0) -> Provenance:
    evidence = EvidenceRef(SOURCE, (JsonPointer(f"/assertions/{index}"),))
    return Provenance(evidence, ADAPTER.id, AssertionKind.STATED)


def assertion(**changes: Any) -> Assertion:
    provenance = changes.pop("provenance", at())
    fields: dict[str, Any] = {
        "identifier": Known(LogicalId("dc-north.fleet-console", "ASR-2026-0108"), cite(10, 60)),
        "assertion_type": Known(AssertionType.SAME_IDENTITY, cite(70, 85)),
        "author": Known(LogicalId("dc-north.staff", "m.okafor"), cite(90, 140)),
        "authored_at": Known(Timestamp(1_789_374_725, CLOCK), cite(150, 177)),
        "authored_zone": Known("Europe/Berlin", cite(190, 205)),
        "scope": Known((TAG, SERIAL, RECORD), cite(210, 330)),
        "retracts": NotApplicable(),
        "payload": Known('{"shift": "B", "n": 1.50}', cite(340, 365)),
        "rationale": Known("Chassis plate reads 22871.", cite(380, 408)),
        "signature": KnownAbsent(provenance),
        "ticket": Known(LogicalId("jira.dc-north", "FLEET-412"), cite(420, 470)),
    }
    fields.update(changes)
    record_id = evidence_record_id(Assertion.kind, provenance.evidence, ADAPTER)
    return Assertion(id=record_id, provenance=provenance, **fields)


def retraction(**changes: Any) -> Assertion:
    base: dict[str, Any] = {
        "assertion_type": Known(AssertionType.RETRACT),
        "retracts": Known(EARLIER, cite(500, 560)),
        "scope": Known(()),
        "payload": KnownAbsent(at()),
        "authored_zone": Unknown(),
    }
    return assertion(**{**base, **changes})


def roundtrip(record: Assertion) -> Assertion:
    line = canonical_json.dumps(record.to_json())
    again = assertion_from_json(canonical_json.loads(line))
    assert again == record
    assert canonical_json.dumps(again.to_json()) == line
    VALIDATOR.validate(canonical_json.loads(line))
    return again


# --- Shape and round trip ----------------------------------------------------------------------


def test_the_kind_is_registered_in_its_own_family_from_its_version() -> None:
    assert RECORD_KINDS["assertion"] == (Assertion, assertion_from_json)
    assert Assertion.family is Family.ASSERTION
    assert KIND_SINCE["assertion"] == ASSERTION_SINCE == SCHEMA_VERSION
    assert "assertion" in kinds_at(ASSERTION_SINCE)
    assert "assertion" not in kinds_at(ASSERTION_SINCE - 1)
    assert package_version(["assertion", "timestamp_domain"]) == ASSERTION_SINCE
    assert package_version(["timestamp_domain", "identity_link"]) == 3


@pytest.mark.parametrize(
    "record",
    [
        assertion(),
        retraction(),
        assertion(assertion_type=Known(AssertionType.ACCEPT_BASELINE), scope=Known((RECORD,))),
        assertion(assertion_type=Known(AssertionType.ANNOTATE), payload=KnownAbsent(at())),
        # What the adapter writes where nothing could be read.
        assertion(
            identifier=Unknown(),
            assertion_type=Unknown(cite(70, 85)),
            author=Unknown(),
            authored_at=Unknown(),
            authored_zone=Unknown(),
            scope=Unknown(cite(210, 330)),
            retracts=Unknown(),
            payload=Unknown(),
            rationale=Unknown(),
            signature=NotCovered(),
            ticket=Unknown(),
        ),
        assertion(
            author=Ambiguous(
                (
                    Candidate(LogicalId("acme.staff", "a"), cite(1, 2)),
                    Candidate(LogicalId("acme.staff", "b"), cite(3, 4)),
                )
            )
        ),
    ],
)
def test_assertions_round_trip_and_validate(record: Assertion) -> None:
    roundtrip(record)


def test_scope_keeps_declared_order_and_both_kinds_of_reference() -> None:
    data = assertion().to_json()
    scope = data["scope"]
    assert isinstance(scope, dict)
    assert scope["value"] == [TAG.to_json(), SERIAL.to_json(), RECORD]
    assert data["schema_version"] == ASSERTION_SINCE and data["kind"] == "assertion"
    # An empty scope is a declaration of none, distinct from a scope that was not read.
    assert roundtrip(retraction()).scope == Known(())


def test_the_json_is_deterministic() -> None:
    first = canonical_json.dumps(assertion().to_json())
    assert first == canonical_json.dumps(assertion().to_json())
    assert first == canonical_json.dumps(assertion_from_json(canonical_json.loads(first)).to_json())


# --- Retraction ----------------------------------------------------------------------------------


def test_a_retract_names_what_it_retracts_and_nothing_else_does() -> None:
    assert roundtrip(retraction()).retracts == Known(EARLIER, cite(500, 560))
    roundtrip(retraction(retracts=Unknown()))  # a retract whose target could not be read
    with pytest.raises(ValueError, match="retracts must be one of"):
        retraction(retracts=NotApplicable())
    with pytest.raises(ValueError, match="only a retract"):
        assertion(retracts=Known(EARLIER))
    with pytest.raises(ValueError, match="only a retract"):
        assertion(retracts=Unknown())
    # A type that was not read may or may not retract.
    roundtrip(assertion(assertion_type=Unknown(), retracts=NotApplicable()))
    roundtrip(assertion(assertion_type=Unknown(), retracts=Known(EARLIER)))


# --- What it refuses ---------------------------------------------------------------------------


def test_an_assertion_is_stated_never_observed_or_inferred() -> None:
    observed = Provenance(at().evidence, ADAPTER.id, AssertionKind.OBSERVED)
    with pytest.raises(ValueError, match="stated"):
        assertion(provenance=observed)
    inferred = InferredProvenance((at().evidence,), ADAPTER.id)
    with pytest.raises(TypeError, match="derived"):
        replace(assertion(), provenance=inferred)  # type: ignore[arg-type]
    data = canonical_json.loads(canonical_json.dumps(assertion().to_json()))
    assert isinstance(data, dict) and isinstance(data["provenance"], dict)
    with pytest.raises(ValueError, match="observed or stated"):
        assertion_from_json(
            {**data, "provenance": {**data["provenance"], "assertion_kind": "inferred"}}
        )


@pytest.mark.parametrize(
    ("changes", "match"),
    [
        ({"identifier": NotApplicable()}, "identifier must be one of"),
        ({"identifier": KnownAbsent(at())}, "identifier must be one of"),
        ({"author": Known("m.okafor")}, "author must be a LogicalId"),
        ({"assertion_type": Known("same_identity")}, "assertion_type must be a AssertionType"),
        ({"authored_at": Known(1_789_374_725)}, "authored_at must be a Timestamp"),
        ({"authored_at": NotApplicable()}, "authored_at must be one of"),
        ({"authored_zone": KnownAbsent(at())}, "authored_zone must be one of"),
        ({"authored_zone": Known("+02:00")}, "IANA"),
        ({"authored_zone": Known("Europe/../etc")}, "IANA"),
        ({"scope": Known(["rec:sha256:" + "ab" * 32])}, "scope must be a tuple"),
        ({"scope": Known(("rec:sha256:AB",))}, "not a record id"),
        ({"scope": Known((("fleet", "AMR-07"),))}, "record id or a LogicalId"),
        ({"scope": KnownAbsent(at())}, "scope must be one of"),
        ({"payload": Known("")}, "payload must be non-empty"),
        ({"rationale": Known("")}, "rationale must be non-empty"),
        ({"rationale": NotApplicable()}, "rationale must be one of"),
        ({"signature": Known(7)}, "signature must be a str"),
        ({"ticket": Known("FLEET-412")}, "ticket must be a LogicalId"),
    ],
)
def test_malformed_values_are_refused(changes: dict[str, Any], match: str) -> None:
    with pytest.raises((ValueError, TypeError), match=match):
        assertion(**changes)


def test_the_reader_refuses_extra_or_missing_keys_and_other_versions() -> None:
    data = canonical_json.loads(canonical_json.dumps(assertion().to_json()))
    assert isinstance(data, dict)
    with pytest.raises(ValueError, match="unexpected"):
        assertion_from_json({**data, "ledger_time": 1})  # the Ledger's, never the record's
    missing = dict(data)
    del missing["rationale"]
    with pytest.raises(ValueError, match="missing"):
        assertion_from_json(missing)
    with pytest.raises(SchemaVersionError, match="newer"):
        assertion_from_json({**data, "schema_version": SCHEMA_VERSION + 1})
    for older in range(1, ASSERTION_SINCE):
        with pytest.raises(SchemaVersionError, match=f"from schema version {ASSERTION_SINCE}"):
            assertion_from_json({**data, "schema_version": older})
    with pytest.raises(ValueError):
        assertion_from_json({**data, "assertion_type": {"knowledge": "known", "value": "merge"}})
    with pytest.raises(ValueError):
        assertion_from_json({**data, "scope": {"knowledge": "known", "value": [7]}})


def test_the_schema_rejects_what_the_reader_rejects_by_shape() -> None:
    data = canonical_json.loads(canonical_json.dumps(assertion().to_json()))
    assert isinstance(data, dict)
    for broken in (
        {**data, "schema_version": ASSERTION_SINCE - 1},
        {**data, "assertion_type": {"knowledge": "known", "value": "merge"}},
        {**data, "scope": {"knowledge": "known", "value": [{"namespace": "x"}]}},
        {**data, "scope": {"knowledge": "known", "value": ["not a record id"]}},
        {**data, "transaction_time": 1},
    ):
        assert list(VALIDATOR.iter_errors(broken)), broken


# --- Boundaries --------------------------------------------------------------------------------


def test_zone_names_are_checked_by_spelling_only() -> None:
    for name in ("UTC", "Europe/Berlin", "America/Argentina/Buenos_Aires", "Etc/GMT-5", "EST5EDT"):
        assert is_iana_zone(name)
    for name in ("", "/Europe", "Europe/", "Europe//Berlin", "Europe/Ber lin", "../x", "+10:00"):
        assert not is_iana_zone(name)
    assert is_iana_zone("A" * IANA_ZONE_MAX) and not is_iana_zone("A" * (IANA_ZONE_MAX + 1))
    # Spelling, not lookup: a name no tz release has is still a declared name.
    roundtrip(assertion(authored_zone=Known("Mars/Jezero")))


def test_extreme_ticks_and_long_text_round_trip() -> None:
    roundtrip(assertion(authored_at=Known(Timestamp(-(2**63), CLOCK))))
    roundtrip(assertion(authored_at=Known(Timestamp(2**63 - 1, CLOCK))))
    roundtrip(assertion(rationale=Known("é" * 100_000), scope=Known(tuple([TAG] * 3))))
