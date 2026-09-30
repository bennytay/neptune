"""The value space of canonical JSON (ADR 0002).

There is no ``None``: missingness is ``Knowledge`` (ADR 0004), never ``null``.
"""

from collections.abc import Mapping, Sequence
from typing import TypeAlias

JsonValue: TypeAlias = "str | int | float | bool | Sequence[JsonValue] | Mapping[str, JsonValue]"
JsonObject: TypeAlias = Mapping[str, JsonValue]
