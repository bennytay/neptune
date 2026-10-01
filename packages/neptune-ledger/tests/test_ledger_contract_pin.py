"""The package-schema version declared in docs/contracts.md must match the compiler's constant."""

import re
from pathlib import Path

from neptune.model.record import SCHEMA_VERSION

CONTRACTS = Path(__file__).resolve().parents[1] / "docs" / "contracts.md"


def test_declared_package_schema_version_matches_compiler() -> None:
    text = CONTRACTS.read_text(encoding="utf-8")
    match = re.search(r"\| Package schema \(canonical records\) \|.*?\| \*\*(\d+)\*\* \|", text)
    assert match is not None
    assert int(match.group(1)) == SCHEMA_VERSION
    assert f"urn:neptune:schema:canonical:{SCHEMA_VERSION}" in text
