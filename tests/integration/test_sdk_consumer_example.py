"""MVL-12's acceptance: a robotics codebase integrates ingestion in under ten lines, typed.

``tests/fixtures/sdk/sdk_consumer.py`` is that codebase. It is run here as a program of its own
(no CLI, no shell), its integration is counted, and ``mypy --strict`` checks it, and a misuse of
the SDK, the way a consumer's CI would.
"""

import ast
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Final

import pytest

from neptune.sdk import Neptune

pytestmark = pytest.mark.integration

REPOSITORY: Final = Path(__file__).parents[2]
FIXTURES: Final = REPOSITORY / "tests" / "fixtures"
CONSUMER: Final = FIXTURES / "sdk" / "sdk_consumer.py"


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    return Path(shutil.copytree(FIXTURES / "text", tmp_path / "site-notes"))


def consume(*args: Path) -> subprocess.CompletedProcess[str]:
    command = [sys.executable, str(CONSUMER), *(str(arg) for arg in args)]
    return subprocess.run(command, capture_output=True, text=True, check=False, timeout=300)


def mypy_strict(path: Path) -> subprocess.CompletedProcess[str]:
    command = [sys.executable, "-m", "mypy", "--strict", str(path)]
    return subprocess.run(
        command, capture_output=True, text=True, check=False, timeout=300, cwd=REPOSITORY
    )


def test_the_integration_is_under_ten_lines() -> None:
    tree = ast.parse(CONSUMER.read_text(encoding="utf-8"))
    (function,) = [
        n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "ingest_run"
    ]
    body = function.body[1:]  # after the docstring
    assert isinstance(function.body[0], ast.Expr)
    lines = (body[-1].end_lineno or 0) - body[0].lineno + 1
    imports = [n for n in tree.body if isinstance(n, ast.ImportFrom) and n.module == "neptune.sdk"]
    assert lines + 1 + len(imports) < 10  # the def line, the body and the import


def test_the_consumer_ingests_without_the_cli(corpus: Path, tmp_path: Path) -> None:
    ran = consume(corpus, tmp_path / "out", tmp_path / "home")
    assert ran.returncode == 0, ran.stderr
    summary = json.loads(ran.stdout)
    expected = Neptune(tmp_path / "other").ingest(corpus, tmp_path / "expected")
    assert summary["receipt"] == expected.receipt
    assert summary["chunks"] == {"text": sum(len(s.chunks) for s in expected.cache.sources)}
    assert summary["by_line"] != expected.receipt  # another config: another lineage
    assert summary["committed"] > 0
    assert summary["findings"] == ["text.invalid_utf8", "text.invalid_utf8"]


def test_the_consumer_branches_on_a_stable_error_code(tmp_path: Path) -> None:
    ran = consume(tmp_path / "missing", tmp_path / "out", tmp_path / "home")
    assert ran.returncode == 2
    assert ran.stderr.startswith("invalid_source: ")


def test_mypy_strict_passes_on_the_consumer() -> None:
    checked = mypy_strict(CONSUMER)
    assert checked.returncode == 0, checked.stdout
    assert "Success" in checked.stdout


def test_mypy_strict_catches_a_misuse_of_the_sdk(tmp_path: Path) -> None:
    misuse = tmp_path / "misuse.py"
    misuse.write_text(
        "from pathlib import Path\n"
        "from neptune.sdk import Neptune\n"
        "\n"
        "\n"
        "def receipt_upper(run: Path) -> str:\n"
        "    result = Neptune().ingest(run, 42)\n"
        "    return result.receipt.upper()\n",
        encoding="utf-8",
    )
    checked = mypy_strict(misuse)
    assert checked.returncode == 1
    assert "[arg-type]" in checked.stdout  # 42 is no path
    assert "[union-attr]" in checked.stdout  # no receipt unless a package was committed
