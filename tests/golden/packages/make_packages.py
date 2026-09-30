"""Package the four worked examples and keep their manifest and receipt as golden files.

Run ``make examples`` (or ``uv run python tests/golden/packages/make_packages.py``) after the
examples change. ``tests/integration/test_example_packages.py`` checks the committed files are
exactly what packaging the committed examples gives. The records themselves are the golden files
under ``tests/fixtures/model/``; only the package's own documents are kept here.
"""

from pathlib import Path
from typing import Any, Final

from neptune.identity import canonical_json
from neptune.model.kinds import RECORD_KINDS
from neptune.store.package import MANIFEST, RECEIPT, RECEIPT_TEXT, package_files

HERE: Final = Path(__file__).parent
EXAMPLES: Final = HERE.parents[1] / "fixtures" / "model"
NAMES: Final = ("drone", "quadruped", "manipulator", "mobile_robot")
DOCUMENTS: Final = (MANIFEST, RECEIPT, RECEIPT_TEXT)


def example_records(name: str) -> list[Any]:
    """Every record of a worked example, read from its committed tables."""
    records: list[Any] = []
    for path in sorted((EXAMPLES / name / "records").glob("*.jsonl")):
        _, read = RECORD_KINDS[path.stem]
        records += [read(canonical_json.loads(line)) for line in path.read_bytes().splitlines()]
    return records


def build() -> dict[str, bytes]:
    """The golden documents, by path relative to this directory."""
    files: dict[str, bytes] = {}
    for name in NAMES:
        package = package_files(example_records(name))
        for document in DOCUMENTS:
            files[f"{name}/{document}"] = package[document]
    return files


if __name__ == "__main__":
    for relative, data in build().items():
        path = HERE / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
