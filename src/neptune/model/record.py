"""The envelope every canonical record shares, and the schema versions that write it (ADR 0017).

Each record is one canonical-JSON line in its kind's table (ADR 0002). Two envelope keys make the
line self-describing: ``kind`` names the record kind, and so its table, and ``schema_version`` is
the lowest version of this model whose readers read the line: the version that added its kind
(ADR 0037 §1). A reader refuses a newer version instead of guessing, and reads every older one
as it is, since the model only grows.

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

# The newest version of the canonical model: what this code reads and can write. It became 1 at
# the M1 gate (ADR 0023), 2 with the configuration kinds (ADR 0037), 3 with the alignment kinds
# (ADR 0050), 4 with the deployment lifecycle kinds (ADR 0051), 5 with the assertion kind
# (ADR 0062), 6 with the civil time zone kind and list states (ADR 0061), 7 with the task kinds
# (ADR 0063) and 8 with the robot-description kinds (ADR 0039). The model only grows: a newer
# version adds record kinds, enum members, locator steps or states a field may hold, through an
# ADR, and never changes an existing field's JSON. A record of any version from
# OLDEST_READABLE_VERSION on is therefore valid as it is: its migration is the identity. A record
# is written at the version that added its kind, or the later version that added a shape it uses,
# so an addition never changes the bytes of records that do not use it (ADR 0037 §1, ADR 0061 §6).
SCHEMA_VERSION: Final = 8
OLDEST_READABLE_VERSION: Final = 1
ENVELOPE_KEYS: Final = frozenset({"kind", "schema_version"})


class Family(StrEnum):
    """What a record kind describes. Every kind belongs to exactly one family (ADR 0017 §4).

    ``machine`` to ``run`` are the design contract's source domains. A source is attributed to a
    domain by the families of the records that cite it; no record kind straddles two.
    """

    SOURCE = "source"  # which bytes exist and where they were seen
    LINEAGE = "lineage"  # what produced the other records
    FINDING = "finding"  # what went wrong, was skipped, or could not be represented
    REFERENCE = "reference"  # the clocks and frames that times and poses are expressed in
    MACHINE = "machine"  # machine context: embodiment, sensors, calibration, software
    WORLD = "world"  # world / record context: sites, assets, maps, photos, documents, registers
    TASK = "task"  # task context: briefs, requirements, procedure steps, work orders
    RUN = "run"  # run / experience evidence: sessions and their timestamped streams
    ALIGNMENT = "alignment"  # what evidence says relates other records: ids, clocks, frames, runs
    ASSERTION = "assertion"  # what a person declared about other records: identities, baselines


class SchemaVersionError(ValueError):
    """A record was written by a schema version this reader cannot read."""


def envelope(
    kind: str, body: Mapping[str, JsonValue], version: int = OLDEST_READABLE_VERSION
) -> JsonObject:
    """A record's JSON: its fields plus ``kind`` and ``schema_version``.

    ``version`` is the lowest schema version whose readers read the line: the version that added
    the kind, or for a package's documents the package's version (ADR 0037 §1).
    """
    if ENVELOPE_KEYS & body.keys():
        raise ValueError(f"record fields may not use the envelope keys {sorted(ENVELOPE_KEYS)}")
    check_schema_version(version)
    return {**body, "kind": kind, "schema_version": version}


def record_object(
    data: JsonValue, kind: str, keys: set[str], since: int = OLDEST_READABLE_VERSION
) -> Mapping[str, JsonValue]:
    """Check one record's JSON strictly: a readable version, this kind, and exactly ``keys``.

    ``since`` is the version that added the kind, so no line of it is older. The version is
    checked first, so a record from another version fails with a ``SchemaVersionError``, never
    with an error about keys that version was entitled to have.
    """
    if not isinstance(data, Mapping):
        raise ValueError(f"a {kind} must be a JSON object, got {type(data).__name__}")
    if "schema_version" not in data:
        raise ValueError(f"a {kind} needs a schema_version")
    version = data["schema_version"]
    check_schema_version(version)
    if is_int(version) and version < since:
        raise SchemaVersionError(f"a {kind} is from schema version {since} on, not {version}")
    if data.get("kind") != kind:
        raise ValueError(f"expected kind {kind!r}, got {data.get('kind')!r}")
    return exact_object(data, kind, keys | ENVELOPE_KEYS)


def check_schema_version(version: JsonValue) -> None:
    """This reader reads every version from the M1 gate to its own; the model only grows."""
    if not is_int(version) or version < 0:
        raise ValueError(f"schema_version must be a non-negative integer, got {version!r}")
    if version > SCHEMA_VERSION:
        raise SchemaVersionError(
            f"record schema version {version} is newer than this reader's {SCHEMA_VERSION}"
        )
    if version < OLDEST_READABLE_VERSION:
        raise SchemaVersionError(
            f"record schema version {version} predates the M1 gate; drafts were never persisted"
        )
