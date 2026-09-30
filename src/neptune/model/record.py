"""The envelope every canonical record shares, and the schema version that writes it (ADR 0017).

Each record is one canonical-JSON line in its kind's table (ADR 0002). Two envelope keys make the
line self-describing: ``kind`` names the record kind, and so its table, and ``schema_version`` is
the version of this model that wrote it. Readers refuse any other version instead of guessing.

A record has one of three shapes:

- **Evidence records** decode what one source says. They hold ``id`` and one record-level
  ``provenance``, which their ``Knowledge`` fields inherit unless they cite their own. The id is
  ``evidence_record_id(kind, provenance.evidence, transform)`` (ADR 0003, ADR 0016).
- **Ledger records** are the evidence and the transforms themselves: ``source_artifact``,
  ``source_revision``, ``source_absence`` and ``transform_record``. Their ids come from their own
  content. They carry no provenance, because provenance points at them.
- **Findings** (``ingest_finding``) say what went wrong or was left out. A finding names its
  subject, which may be a location with no bytes to cite, and the transform that found it.
"""

from collections.abc import Mapping
from enum import StrEnum
from typing import Final

from neptune.model._fields import exact_object, is_int
from neptune.model.jsonvalue import JsonObject, JsonValue

# The version of the canonical model that writes records. It is 0 until the M1 gate, and shapes
# may still change without a bump. From 1 on, any change to a record's JSON shape or meaning bumps
# it through an ADR, with a migration from the previous version (ADR 0017 §7).
SCHEMA_VERSION: Final = 0
ENVELOPE_KEYS: Final = frozenset({"kind", "schema_version"})


class Family(StrEnum):
    """What a record kind describes. Every kind belongs to exactly one family (ADR 0017 §4).

    The last four are the design contract's source domains. A source is attributed to a domain by
    the families of the records that cite it; no record kind straddles two.
    """

    SOURCE = "source"  # which bytes exist and where they were seen
    LINEAGE = "lineage"  # what produced the other records
    FINDING = "finding"  # what went wrong, was skipped, or could not be represented
    REFERENCE = "reference"  # the clocks and frames that times and poses are expressed in
    MACHINE = "machine"  # machine context: embodiment, sensors, calibration, software
    WORLD = "world"  # world / record context: sites, assets, maps, photos, documents, registers
    TASK = "task"  # task context: briefs, SOPs, requirements, work orders (MVL-33)
    RUN = "run"  # run / experience evidence: sessions and their timestamped streams


class SchemaVersionError(ValueError):
    """A record was written by a schema version this reader cannot read."""


def envelope(kind: str, body: Mapping[str, JsonValue]) -> JsonObject:
    """A record's JSON: its fields plus ``kind`` and ``schema_version``."""
    if ENVELOPE_KEYS & body.keys():
        raise ValueError(f"record fields may not use the envelope keys {sorted(ENVELOPE_KEYS)}")
    return {**body, "kind": kind, "schema_version": SCHEMA_VERSION}


def record_object(data: JsonValue, kind: str, keys: set[str]) -> Mapping[str, JsonValue]:
    """Check one record's JSON strictly: this schema version, this kind, and exactly ``keys``.

    The version is checked first, so a record from another version fails with a
    ``SchemaVersionError``, never with an error about keys that version was entitled to have.
    """
    if not isinstance(data, Mapping):
        raise ValueError(f"a {kind} must be a JSON object, got {type(data).__name__}")
    if "schema_version" not in data:
        raise ValueError(f"a {kind} needs a schema_version")
    check_schema_version(data["schema_version"])
    if data.get("kind") != kind:
        raise ValueError(f"expected kind {kind!r}, got {data.get('kind')!r}")
    return exact_object(data, kind, keys | ENVELOPE_KEYS)


def check_schema_version(version: JsonValue) -> None:
    """This reader reads only ``SCHEMA_VERSION``. Migrations arrive with the first bump."""
    if not is_int(version) or version < 0:
        raise ValueError(f"schema_version must be a non-negative integer, got {version!r}")
    if version > SCHEMA_VERSION:
        raise SchemaVersionError(
            f"record schema version {version} is newer than this reader's {SCHEMA_VERSION}"
        )
    if version < SCHEMA_VERSION:
        raise SchemaVersionError(
            f"record schema version {version} has no migration to {SCHEMA_VERSION}"
        )
