"""The CRS of a GeoJSON file: what it states, what RFC 7946 defaults, and what nobody can say.

Never invented (ADR 0057 §3):

- A root ``crs`` member (GeoJSON 2008, removed by RFC 7946) is stated as written. Its ``name`` is
  read as an OGC URN, an OGC HTTP URI or ``AUTHORITY:CODE``, taking the authority and the code
  verbatim; a ``link`` is never followed, a ``null`` says no CRS can be assumed, and anything else
  is not recognised: those are ``Unknown``. Two different CRSs are ``Ambiguous``.
- With no ``crs`` member the file is RFC 7946's and its CRS is that RFC's default (WGS 84,
  longitude then latitude: ``OGC:CRS84``), cited at the root ``type``, *only* if nothing
  contradicts it. A position outside longitude and latitude's range, a ``crs`` member on a feature
  or geometry, or a file that breaks off before a ``crs`` member could be ruled out, means the file
  is not known to be RFC 7946's: ``Unknown`` and a finding.
"""

import re
from dataclasses import dataclass
from typing import Final

from neptune.adapters.contract import AdapterConfig, ContractError, SourceReader
from neptune.adapters.geojson._common import (
    DEFAULT_CRS,
    GEOGRAPHIC,
    cite,
    finding,
    observed,
    stated,
)
from neptune.model.finding import IngestFinding
from neptune.model.jsonvalue import JsonObject
from neptune.model.knowledge import Ambiguous, Candidate, Knowledge, Known, Unknown
from neptune.model.spatial import MAX_TEXT_LENGTH, CrsCode

_URN: Final = re.compile(r"urn:ogc:def:crs:([A-Za-z][\w.-]*):[\w.-]*:([\w.-]+)", re.ASCII)
_URI: Final = re.compile(
    r"https?://www\.opengis\.net/def/crs/([A-Za-z][\w.-]*)/[\w.-]+/([\w.-]+)", re.ASCII
)
_SHORT: Final = re.compile(r"([A-Za-z][\w.-]*):([\w.-]+)", re.ASCII)


@dataclass(frozen=True)
class Reading:
    authority: str
    code: str
    start: int  # the bytes that state it
    end: int


@dataclass(frozen=True)
class Decision:
    kind: str  # stated, default, ambiguous or unknown
    readings: tuple[Reading, ...]
    cite: tuple[int, int]  # the bytes an Unknown cites
    geographic: bool

    def to_json(self) -> JsonObject:
        return {
            "cite": list(self.cite),
            "geographic": self.geographic,
            "kind": self.kind,
            "readings": [
                {"authority": r.authority, "code": r.code, "end": r.end, "start": r.start}
                for r in self.readings
            ],
        }


def decision_from_json(data: JsonObject) -> Decision:
    raw = data["readings"]
    cited = data["cite"]
    if not isinstance(raw, list) or not isinstance(cited, list) or len(cited) != 2:
        raise ContractError("a crs decision holds readings and a citation")
    readings = []
    for item in raw:
        if not isinstance(item, dict):
            raise ContractError("a crs reading is an object")
        authority, code = item["authority"], item["code"]
        start, end = item["start"], item["end"]
        if not (isinstance(authority, str) and isinstance(code, str)):
            raise ContractError("a crs reading names an authority and a code")
        if not (isinstance(start, int) and isinstance(end, int)):
            raise ContractError("a crs reading cites bytes")
        readings.append(Reading(authority, code, start, end))
    kind, geographic = data["kind"], data["geographic"]
    if not isinstance(kind, str) or not isinstance(geographic, bool):
        raise ContractError("a crs decision has a kind and a geographic flag")
    first, second = cited
    if not (isinstance(first, int) and isinstance(second, int)):
        raise ContractError("a crs decision cites bytes")
    return Decision(kind, tuple(readings), (first, second), geographic)


def _code(authority: str, code: str) -> tuple[str, str] | None:
    if len(authority) > MAX_TEXT_LENGTH or len(code) > MAX_TEXT_LENGTH:
        return None
    return authority, code


def parse_name(name: str) -> tuple[str, str] | None:
    """An authority and a code, verbatim, from a CRS name; ``None`` if it is none of the forms."""
    for pattern in (_URN, _URI, _SHORT):
        match = pattern.fullmatch(name)
        if match:
            return _code(match.group(1), match.group(2))
    return None


def parse_member(value: object) -> tuple[tuple[str, str] | None, str]:
    """What a ``crs`` member's value states: a code, or why it states none."""
    if value is None:
        return None, "null: GeoJSON 2008 says no CRS can be assumed"
    if not isinstance(value, dict):
        return None, "not a CRS object"
    kind, properties = value.get("type"), value.get("properties")
    if not isinstance(properties, dict):
        return None, "a CRS object without properties"
    if kind == "name" and isinstance(properties.get("name"), str):
        found = parse_name(str(properties["name"]))
        if found is not None:
            return found, ""
        return None, "a name that is no OGC URN, OGC URI or AUTHORITY:CODE"
    if kind == "EPSG":
        number = properties.get("code")
        if isinstance(number, int) and not isinstance(number, bool) and number >= 0:
            return ("EPSG", str(number)), ""
        return None, "an EPSG object without an integer code"
    if kind == "link":
        return None, "a link, which is never followed"
    return None, "a CRS object of an unrecognised type"


@dataclass(frozen=True)
class RootMember:
    name: str
    start: int
    end: int
    value: object


def decide(
    source: SourceReader,
    config: AdapterConfig,
    members: list[RootMember],
    *,
    contradicted: str,
    whole: tuple[int, int],
) -> tuple[Decision, list[IngestFinding]]:
    """The file's CRS from its root members. ``contradicted`` is why the RFC 7946 default cannot
    be taken (``""`` if nothing says so); ``whole`` the bytes to cite when there is nothing."""
    crs = [m for m in members if m.name == "crs"]
    kind = next((m for m in members if m.name == "type"), None)
    found: list[IngestFinding] = []

    def unknown(why: str, at: tuple[int, int]) -> tuple[Decision, list[IngestFinding]]:
        found.append(
            finding(
                config,
                "crs_unknown",
                cite(source, *at),
                f"the CRS is Unknown: {why}",
                {"reason": why},
            )
        )
        return Decision("unknown", (), at, False), found

    if crs:
        readings: list[Reading] = []
        reasons: list[str] = []
        for member in crs:
            code, why = parse_member(member.value)
            if code is None:
                reasons.append(why)
            elif all((r.authority, r.code) != code for r in readings):
                readings.append(Reading(code[0], code[1], member.start, member.end))
        at = (crs[0].start, crs[0].end)
        if len(readings) >= 2:
            names = [f"{r.authority}:{r.code}" for r in readings]
            found.append(
                finding(
                    config,
                    "crs_ambiguous",
                    cite(source, *at),
                    f"the file names {len(readings)} different CRSs: {', '.join(names)}",
                    {"candidates": list(names)},
                )
            )
            return Decision("ambiguous", tuple(readings), at, False), found
        if readings:
            only = readings[0]
            found.append(
                finding(
                    config,
                    "crs_legacy",
                    cite(source, only.start, only.end),
                    f"the file states CRS {only.authority}:{only.code} in a crs member, which"
                    " RFC 7946 removed",
                    {"authority": only.authority, "code": only.code},
                )
            )
            geographic = (only.authority, only.code) in GEOGRAPHIC
            return Decision("stated", (only,), at, geographic), found
        return unknown(reasons[0], at)
    if contradicted:
        at = (kind.start, kind.end) if kind else whole
        return unknown(contradicted, at)
    if kind is None:
        return unknown("the root states no type to cite RFC 7946's default at", whole)
    reading = Reading(DEFAULT_CRS[0], DEFAULT_CRS[1], kind.start, kind.end)
    found.append(
        finding(
            config,
            "crs_defaulted",
            cite(source, kind.start, kind.end),
            "the file has no crs member: its CRS is RFC 7946's default, OGC:CRS84 (WGS 84,"
            " longitude then latitude)",
            {"authority": reading.authority, "code": reading.code},
        )
    )
    return Decision("default", (reading,), (kind.start, kind.end), True), found


def knowledge(
    decision: Decision, source: SourceReader, config: AdapterConfig
) -> Knowledge[CrsCode]:
    """The CRS state a decision gives every record of the file."""
    match decision.kind:
        case "stated" | "default":
            (reading,) = decision.readings
            evidence = cite(source, reading.start, reading.end)
            return Known(CrsCode(reading.authority, reading.code), stated(evidence, config))
        case "ambiguous":
            return Ambiguous(
                tuple(
                    Candidate(
                        CrsCode(r.authority, r.code),
                        stated(cite(source, r.start, r.end), config),
                    )
                    for r in decision.readings
                )
            )
        case _:
            return Unknown(observed(cite(source, *decision.cite), config))
