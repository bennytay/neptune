"""The Demo v1 snapshot: what its generator writes, a graph-schema document, and cited by the
acceptance corpus's own content ids (ADR 0014)."""

import json
from typing import Any

import pytest
from jsonschema import Draft202012Validator

import deploy_pack_corpus as corpus
from deploy_pack_graphs import REPO, sha
from deploy_pack_support import schema_path
from neptune_deploy.packs import load_snapshot

CLOCKS_SCHEMA = "1.4.0"  # the newest graph-schema on main: clocks and clock maps
MAX_FIXTURE_BYTES = 512 * 1024


def _patched(schema: dict[str, Any]) -> dict[str, Any]:
    """graph-schema 1.4.0 plus what the unmerged minors add that the fixture uses: the ``event``
    node type (1.6.0, PR #125) and the ``delta`` value type and literal (1.7.0, PR #129)."""
    defs = schema["$defs"]
    if "event" not in defs["NodeType"]["enum"]:
        defs["NodeType"]["enum"].append("event")
    if "delta" not in defs["ValueType"]["enum"]:
        defs["ValueType"]["enum"].append("delta")
        defs["TypedLiteral"]["anyOf"].append(
            {
                "additionalProperties": False,
                "properties": {
                    "datatype": {"const": "delta"},
                    "kind": {"const": "literal"},
                    "unit": {"type": "object"},
                    "value": {
                        "additionalProperties": False,
                        "properties": {
                            "earlier": {"$ref": "#/$defs/RecordId"},
                            "later": {"$ref": "#/$defs/RecordId"},
                            "name": {"type": "string"},
                            "quantity": {"const": "parameter"},
                            "representation": {"const": "values"},
                            "values": {"items": {"type": "number"}, "minItems": 1},
                        },
                        "required": [
                            "earlier",
                            "later",
                            "name",
                            "quantity",
                            "representation",
                            "values",
                        ],
                        "type": "object",
                    },
                },
                "required": ["datatype", "kind", "unit", "value"],
                "type": "object",
            }
        )
    return schema


def validator(definition: str) -> Draft202012Validator:
    schema = _patched(json.loads(schema_path("graph-schema", CLOCKS_SCHEMA).read_text("utf-8")))
    return Draft202012Validator({"$defs": schema["$defs"], "$ref": f"#/$defs/{definition}"})


def test_the_fixture_is_what_the_generator_writes() -> None:
    assert corpus.fixture_path().read_bytes() == corpus.fixture_bytes()
    assert len(corpus.fixture_bytes()) <= MAX_FIXTURE_BYTES


def test_the_snapshot_is_a_graph_schema_document() -> None:
    document = json.loads(corpus.fixture_path().read_bytes())
    validator("Graph").validate(document)
    # Without the patch, the event node type is what 1.4.0 lacks.
    plain = json.loads(schema_path("graph-schema", CLOCKS_SCHEMA).read_text("utf-8"))
    unpatched = Draft202012Validator({"$defs": plain["$defs"], "$ref": "#/$defs/Graph"})
    assert not unpatched.is_valid(document)


def test_the_snapshot_reads_and_uses_every_newer_value_type() -> None:
    snap = load_snapshot(corpus.fixture_path().read_bytes())
    datatypes = {c.object.get("datatype") for c in snap.claims}
    assert {"clock_map", "delta", "text"} <= datatypes
    assert {c.subject.node_type for c in snap.claims} >= {
        "clock",
        "configuration",
        "event",
        "machine",
        "run",
        "sensor",
        "site",
        "zone",
    }


def test_every_corpus_source_is_the_acceptance_corpus_file_it_names() -> None:
    lock = json.loads((REPO / "harness/acceptance/corpus.lock.json").read_text("utf-8"))
    if lock["version"] != corpus.CORPUS_VERSION:
        pytest.skip(f"the snapshot is frozen at corpus {corpus.CORPUS_VERSION}")
    for path, content in corpus.CORPUS.items():
        assert lock["files"][path]["sha256"] == content, path


def test_every_claim_cites_a_corpus_file_or_a_named_synthetic_source() -> None:
    document = json.loads(corpus.fixture_path().read_bytes())
    corpus_ids = set(corpus.CORPUS.values())
    synthetic = {
        f"sha256:{sha('synthetic source ' + name)}"
        for name in (
            "AMR-07 controller log",
            "AMR-07 lidar calibration file",
            "S-007 CMMS incidents",
            "S-007 fleet manager syslog",
            "S-007 intervention log",
            "S-007 time-sync statement",
        )
    }
    used = {ref["source"] for c in document["claims"] for ref in c["provenance"]["evidence"]}
    assert used <= corpus_ids | synthetic
    assert used & corpus_ids == corpus_ids  # every frozen corpus file is cited
