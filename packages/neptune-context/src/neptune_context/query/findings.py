"""Structured query findings (ADR 0002 §7): every reason a query is refused, never an exception.

A finding names a stable ``code``, where it applies (``at``: a JSON pointer into the query's
canonical JSON, or ``line N`` for a textual-form syntax error) and a deterministic message. Any
finding refuses the query; there are no warnings in this version.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from neptune.model.jsonvalue import JsonObject


class FindingCode(StrEnum):
    # The document itself.
    TOO_LARGE = "too_large"  # beyond MAX_DOCUMENT_BYTES
    SYNTAX = "syntax"  # not JSON, or a textual line that does not parse
    SHAPE = "shape"  # a missing, extra or wrongly typed member
    UNSUPPORTED_VERSION = "unsupported_version"  # query_version is not QUERY_VERSION
    DUPLICATE = "duplicate"  # a set member or a single clause given twice
    # Values.
    OUT_OF_RANGE = "out_of_range"  # a number or a list length outside its bounds
    EMPTY = "empty"  # a set that must name something names nothing
    BAD_IDENTIFIER = "bad_identifier"  # not a declared id, record id or claim id
    UNKNOWN_KIND = "unknown_kind"  # neither a Memory node type nor a Ledger thread kind
    UNKNOWN_PREDICATE = "unknown_predicate"  # not in the pinned graph-schema vocabulary
    BAD_CLOCK = "bad_clock"  # a civil time that is not absolute, or a bad resolution
    BAD_INTERVAL = "bad_interval"  # during start >= end
    BAD_UNIT = "bad_unit"  # not a canonical length unit symbol
    BAD_REGION = "bad_region"  # non-finite coordinate, min >= max, radius <= 0
    BAD_TEXT = "bad_text"  # blank, too long or carrying control characters
    # Meaning.
    EMPTY_QUERY = "empty_query"  # selects nothing: no subject, region, site, text or explain
    SAME_AS_WITHOUT_ID = "same_as_without_id"  # same_as depth on a kind-wide selector
    GRAPH_WITHOUT_ANCHOR = "graph_without_anchor"  # graph hops from no declared subject or site
    CROSS_CLOCK_WITHOUT_MAPPING = "cross_clock_without_mapping"
    DANGLING_CLOCK_BRIDGE = "dangling_clock_bridge"  # a bridge that joins no clock in play
    CROSS_FRAME_WITHOUT_TRANSFORM = "cross_frame_without_transform"
    DANGLING_FRAME_BRIDGE = "dangling_frame_bridge"  # a bridge that joins no region's frame
    MIXED_REGION_UNITS = "mixed_region_units"  # regions declared in different units
    DIFF_MIXED_AXES = "diff_mixed_axes"  # one transaction point and one instant
    DIFF_NOT_ORDERED = "diff_not_ordered"  # before is not earlier than after on one axis
    DIFF_BEYOND_AS_OF = "diff_beyond_as_of"  # a transaction later than the query's snapshot


@dataclass(frozen=True)
class QueryFinding:
    code: FindingCode
    at: str
    message: str

    def to_json(self) -> JsonObject:
        return {"at": self.at, "code": str(self.code), "message": self.message}


@dataclass(frozen=True)
class Refused:
    """A query that was not accepted, and every reason why, in a deterministic order."""

    findings: tuple[QueryFinding, ...]

    def to_json(self) -> JsonObject:
        return {"findings": [finding.to_json() for finding in self.findings]}
