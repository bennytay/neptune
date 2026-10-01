"""Packages across schema versions (ADR 0037 §1): a package is written at the lowest version that
holds its records, so adding a kind changes no package that does not use it, and every version
from 1 on stays readable.
"""

import io
from dataclasses import replace
from typing import Any

import pytest

from neptune.adapters.config import ConfigAdapter
from neptune.adapters.harness import ingest_source
from neptune.discovery.reader import BytesReader
from neptune.identity import canonical_json
from neptune.identity.hashing import content_id, digest_stream
from neptune.identity.revisions import SourceLedger
from neptune.model import record as record_module
from neptune.model.kinds import kinds_at
from neptune.model.package import PackageFile
from neptune.model.record import SchemaVersionError
from neptune.model.source import LocalPath
from neptune.store.package import (
    MANIFEST,
    RECEIPT,
    RECEIPT_TEXT,
    PackageError,
    package_files,
    read_files,
    table_path,
)
from neptune.store.receipt import build_receipt, render_receipt

CONFIG = b"rate: 20.0\nframe: base_link\n"
NOTES = b"field notes\n"


def ledger_records() -> list[Any]:
    ledger = SourceLedger()
    ledger.observe(LocalPath("params.yaml"), digest_stream(io.BytesIO(CONFIG)))
    ledger.observe(LocalPath("notes.txt"), digest_stream(io.BytesIO(NOTES)))
    return [*ledger.artifacts(), *ledger.revisions(), *ledger.absences()]


def with_config() -> list[Any]:
    output = ingest_source(ConfigAdapter(), BytesReader(CONFIG))
    return [*ledger_records(), *output.package_records()]


def document(files: dict[str, bytes], name: str) -> dict[str, Any]:
    data = canonical_json.loads(files[name])
    assert isinstance(data, dict)
    return data


def test_a_package_without_configuration_is_a_version_1_package() -> None:
    files = package_files(ledger_records())
    assert document(files, MANIFEST)["schema_version"] == 1
    assert document(files, RECEIPT)["schema_version"] == 1
    assert table_path("configuration_value") not in files
    assert sorted(document(files, MANIFEST)["tables"]) == sorted(kinds_at(1))
    package = read_files(files)
    assert (package.manifest.version, package.receipt.version) == (1, 1)


def test_a_package_with_configuration_is_a_version_2_package() -> None:
    files = package_files(with_config())
    manifest = document(files, MANIFEST)
    assert manifest["schema_version"] == 2 and document(files, RECEIPT)["schema_version"] == 2
    assert sorted(manifest["tables"]) == sorted(kinds_at(2))
    assert manifest["tables"]["configuration_value"] == 3
    # Records keep the version of their kind: only the new kinds say 2.
    lines = files[table_path("configuration_snapshot")] + files[table_path("transform_record")]
    versions = {version_of(line) for line in lines.splitlines()}
    assert versions == {1, 2}
    package = read_files(files)
    assert dict(package.receipt.records)["configuration_value"] == 3
    assert (package.manifest.version, package.receipt.version) == (2, 2)


def version_of(line: bytes) -> object:
    data = canonical_json.loads(line)
    assert isinstance(data, dict)
    return data["schema_version"]


def test_a_version_1_reader_refuses_a_version_2_package_by_its_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    files = package_files(with_config())
    monkeypatch.setattr(record_module, "SCHEMA_VERSION", 1)
    with pytest.raises(SchemaVersionError, match="newer than this reader"):
        read_files(files)


def rewrite(files: dict[str, bytes], name: str, data: dict[str, Any]) -> dict[str, bytes]:
    return {**files, name: canonical_json.dumps(data)}


def test_tables_must_be_exactly_the_kinds_of_the_package_version() -> None:
    files = package_files(with_config())
    claimed_1 = rewrite(files, MANIFEST, {**document(files, MANIFEST), "schema_version": 1})
    with pytest.raises(PackageError, match="schema version 1, and no other"):
        read_files(claimed_1)
    old = package_files(ledger_records())
    claimed_2 = rewrite(old, MANIFEST, {**document(old, MANIFEST), "schema_version": 2})
    with pytest.raises(PackageError, match="schema version 2, and no other"):
        read_files(claimed_2)


def test_a_package_claiming_a_version_above_its_records_is_refused() -> None:
    # Every check but the version rule passes: the v2 tables are there and empty, the receipt is
    # a version 2 receipt of the same records, and the manifest lists every file's hash.
    files = package_files(ledger_records())
    package = read_files(files)
    receipt = build_receipt(package.records, version=2)
    forged = {**files, RECEIPT: canonical_json.dumps(receipt.to_json())}
    forged[RECEIPT_TEXT] = render_receipt(receipt).encode("utf-8")
    for kind in set(kinds_at(2)) - set(kinds_at(1)):
        forged[table_path(kind)] = b""
    tables = tuple(
        sorted((kind, dict(package.manifest.tables).get(kind, 0)) for kind in kinds_at(2))
    )
    listed = tuple(
        sorted(
            (
                PackageFile(path, len(data), content_id(data))
                for path, data in forged.items()
                if path != MANIFEST
            ),
            key=lambda f: f.path,
        )
    )
    manifest = replace(package.manifest, version=2, tables=tables, files=listed, receipt=receipt.id)
    forged[MANIFEST] = canonical_json.dumps(manifest.to_json())
    with pytest.raises(PackageError, match="records are of version 1"):
        read_files(forged)
    # The boundary: the same package at version 1, and a version 2 package holding a v2 record.
    assert read_files(files).manifest.version == 1
    assert read_files(package_files(with_config())).manifest.version == 2


def test_a_receipt_of_another_version_is_refused() -> None:
    files = package_files(ledger_records())
    package = read_files(files)
    other = replace(package.receipt, version=2)
    edited = {**files, RECEIPT: canonical_json.dumps(other.to_json())}
    receipt = edited[RECEIPT]
    listed = tuple(
        replace(f, size=len(receipt), sha256=content_id(receipt)) if f.path == RECEIPT else f
        for f in package.manifest.files
    )
    manifest = replace(package.manifest, files=listed)
    edited[MANIFEST] = canonical_json.dumps(manifest.to_json())
    with pytest.raises(PackageError, match="different schema versions"):
        read_files(edited)


def test_a_receipt_is_never_built_at_a_version_that_cannot_hold_its_records() -> None:
    with pytest.raises(ValueError, match="cannot hold"):
        build_receipt(with_config(), version=1)
    assert build_receipt(with_config()).version == 2
    assert build_receipt(ledger_records()).version == 1
