"""Strict readers for the JSON documents the pack compiler takes: every refusal is a ``PackError``
naming the JSON pointer of what it refused."""

import json
import re
from collections.abc import Mapping, Sequence
from typing import Final

from neptune.model.jsonvalue import JsonObject, JsonValue
from neptune_deploy.packs.errors import PackError

TOKEN: Final = re.compile(r"[a-z][a-z0-9_.\-]*")
SHA256: Final = re.compile(r"sha256:[0-9a-f]{64}")
RECORD_ID: Final = re.compile(r"rec:sha256:[0-9a-f]{64}")
CLAIM_ID: Final = re.compile(r"claim:sha256:[0-9a-f]{64}")
FINDING_ID: Final = re.compile(r"finding:sha256:[0-9a-f]{64}")
INT64_MAX: Final = 2**63 - 1


def parse_document(data: bytes, code: str, max_bytes: int) -> JsonValue:
    """JSON bytes as a value: UTF-8, no duplicate key, no NaN or Infinity, bounded size, depth."""
    if len(data) > max_bytes:
        raise PackError(code, f"document is {len(data)} bytes, over the {max_bytes} byte limit")

    def pairs(items: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
        result = dict(items)
        if len(result) != len(items):
            raise PackError(code, "duplicate object key")
        return result

    def constant(token: str) -> JsonValue:
        raise PackError(code, f"{token} is not JSON")

    try:
        value: JsonValue = json.loads(
            data.decode("utf-8"), object_pairs_hook=pairs, parse_constant=constant
        )
    except RecursionError as exc:
        raise PackError(code, "document is nested too deeply") from exc
    except ValueError as exc:  # UnicodeDecodeError, JSONDecodeError
        if isinstance(exc, PackError):
            raise
        raise PackError(code, f"not JSON: {exc}") from exc
    return value


class Reader:
    """Typed access to one document; ``code`` is the error code its refusals carry."""

    def __init__(self, code: str) -> None:
        self.code = code

    def fail(self, message: str, pointer: str) -> PackError:
        return PackError(self.code, message, pointer)

    def obj(
        self,
        value: JsonValue,
        pointer: str,
        required: Sequence[str],
        optional: Sequence[str] = (),
    ) -> JsonObject:
        if not isinstance(value, Mapping):
            raise self.fail("expected an object", pointer)
        missing = [key for key in required if key not in value]
        if missing:
            raise self.fail(f"missing {', '.join(sorted(missing))}", pointer)
        extra = sorted(set(value) - set(required) - set(optional))
        if extra:
            raise self.fail(f"unexpected {', '.join(extra)}", pointer)
        return value

    def array(self, value: JsonValue, pointer: str) -> Sequence[JsonValue]:
        if isinstance(value, str) or not isinstance(value, Sequence):
            raise self.fail("expected an array", pointer)
        return value

    def string(self, value: JsonValue, pointer: str, pattern: re.Pattern[str] | None = None) -> str:
        if not isinstance(value, str):
            raise self.fail("expected a string", pointer)
        if pattern is not None and not pattern.fullmatch(value):
            raise self.fail(f"{value!r} does not match {pattern.pattern}", pointer)
        return value

    def text(self, value: JsonValue, pointer: str) -> str:
        """A non-empty string."""
        text = self.string(value, pointer)
        if not text:
            raise self.fail("expected a non-empty string", pointer)
        return text

    def integer(self, value: JsonValue, pointer: str, minimum: int | None = None) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise self.fail("expected an integer", pointer)
        if minimum is not None and value < minimum:
            raise self.fail(f"{value} is below {minimum}", pointer)
        if not -INT64_MAX - 1 <= value <= INT64_MAX:
            raise self.fail(f"{value} is outside the 64-bit range", pointer)
        return value

    def choice(self, value: JsonValue, pointer: str, choices: Sequence[str]) -> str:
        text = self.string(value, pointer)
        if text not in choices:
            raise self.fail(f"{text!r} is not one of {', '.join(choices)}", pointer)
        return text


def child(pointer: str, key: str | int) -> str:
    """The JSON pointer of ``key`` under ``pointer`` (RFC 6901 escaping)."""
    return f"{pointer}/{str(key).replace('~', '~0').replace('/', '~1')}"
