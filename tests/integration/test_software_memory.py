"""The software adapter's peak memory on hostile inputs at its size limits (ADR 0040 §11).

The adapter declares 512 MiB. Each case builds a file the adapter would read (under its document
or script cap) that makes a naive reader hold one object per byte: one draft per lockfile entry,
one syntax-tree node per token, one finding per bad line. They run in a process of their own
(``measure_peak_memory.py``) so the peak is the input's. Measured peaks, interpreter and imports
included, are 45-250 MiB; the bound here leaves room for the machine, not for a regression.
"""

import json
import subprocess
import sys
from pathlib import Path
from typing import Final

import pytest

SCRIPT: Final = Path(__file__).parents[1] / "fixtures" / "software" / "measure_peak_memory.py"
PEAK_MIB: Final = 320  # of the 512 declared
CASES: Final = {
    "cargo": "too_many_items",
    "cargo_empty_tables": "too_many_items",
    "poetry_named_tables": "too_many_items",
    "setup_nested_operators": "malformed",
    "uv": "too_many_items",
    "cmake": None,
    "cmake_arguments": None,
    "setup_calls": None,
    "setup_expression": None,
    "packed_refs": "too_many_items",
    "packed_tags": "too_many_items",
    "package_xml_names": "too_many_entries",
    "npm_lock": "too_many_items",
    "npm_lock_junk": "too_many_entries",
    "cyclonedx": "too_many_items",
    "safetensors_junk": "too_many_entries",
    "elf_notes": "too_many_entries",
}


@pytest.mark.slow
@pytest.mark.parametrize("case", sorted(CASES))
def test_a_hostile_file_at_the_size_limit_stays_inside_the_declared_memory(case: str) -> None:
    if not Path("/proc/self/status").exists():
        pytest.skip("the measurement reads VmHWM from /proc")
    done = subprocess.run([sys.executable, str(SCRIPT), case], capture_output=True, timeout=300)
    assert done.returncode == 0, done.stderr.decode()
    measured = json.loads(done.stdout)
    assert measured["peak_mib"] < PEAK_MIB, measured
    if CASES[case] is not None:
        assert CASES[case] in measured["codes"], measured  # it was refused or cut, not read whole
