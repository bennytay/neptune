"""Structured packet findings (ADR 0003 §8): every reason a packet document is refused.

A packet that breaks the contract is never half-read. ``decode`` turns every problem into a
``PacketFinding`` with a stable ``code``, where it applies (``at``: a JSON pointer into the packet
document) and a deterministic message. Constructors raise ``PacketError`` with the same codes, so
a packet built in Python and a packet read from bytes are held to one rule.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from neptune.model.jsonvalue import JsonObject


class PacketFindingCode(StrEnum):
    # The document.
    TOO_LARGE = "too_large"  # beyond MAX_PACKET_BYTES
    SYNTAX = "syntax"  # not JSON, a duplicate key, or a NaN / Infinity constant
    SHAPE = "shape"  # a missing, extra or wrongly typed member, or a malformed upstream value
    UNSUPPORTED_VERSION = "unsupported_version"  # packet_version is not PACKET_VERSION
    ID_MISMATCH = "id_mismatch"  # a packet or item id that does not hash its content
    # Values and meaning.
    BAD_VALUE = "bad_value"  # a number, id or text outside its domain
    DUPLICATE = "duplicate"  # an item, ref or entry given twice
    ORDER = "order"  # a list out of its canonical order
    ASSERTION_MISMATCH = "assertion_mismatch"  # confidence or model disagree with assertion_kind
    INFERENCE_EXCLUDED = "inference_excluded"  # an inferred item in a packet that excludes them
    NOT_AS_OF = "not_as_of"  # something not known, or not current, at the packet's snapshot
    DANGLING_REFERENCE = "dangling_reference"  # names a claim the packet does not carry
    BUDGET = "budget"  # usage that exceeds a limit or does not match the items


class PacketError(ValueError):
    """A packet value that breaks the contract; ``code`` is its ``PacketFindingCode``."""

    def __init__(self, code: PacketFindingCode, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class PacketFinding:
    code: PacketFindingCode
    at: str
    message: str

    def to_json(self) -> JsonObject:
        return {"at": self.at, "code": str(self.code), "message": self.message}


@dataclass(frozen=True)
class PacketRefused:
    """A packet document that was not accepted, and why. Decoding stops at the first finding:
    later members of a broken document are not trustworthy enough to report on."""

    findings: tuple[PacketFinding, ...]

    def to_json(self) -> JsonObject:
        return {"findings": [finding.to_json() for finding in self.findings]}
