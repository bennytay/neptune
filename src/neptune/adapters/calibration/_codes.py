"""The adapter's id and the finding code of a name."""

from typing import Final

ADAPTER_ID: Final = "calibration"


def code(name: str) -> str:
    return f"{ADAPTER_ID}.{name}"
