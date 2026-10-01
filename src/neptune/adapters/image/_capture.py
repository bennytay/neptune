"""An image's ``capture`` and ``orientation``, read from the EXIF or TIFF tags that declare them.

The rules (ADR 0041 §4), each value citing the row or IFD it is read from:

- ``device_manufacturer`` and ``device_model`` are Make (271) and Model (272), verbatim;
  ``device_identifiers`` are BodySerialNumber (42033, ``exif.body_serial``) and DNG's
  CameraSerialNumber (50735, ``dng.camera_serial``).
- ``time`` is DateTimeOriginal (36867), counted as ADR 0023 §2 counts civil time: POSIX-style
  seconds of its own civil clock, timescale ``Unknown``. With OffsetTimeOriginal (36881) it is an
  exact instant (timescale ``posix``); with SubSecTimeOriginal (37521) the resolution is its
  digits'. One ``TimestampDomain`` (``DateTimeOriginal``, role ``sample``) per clock; a time read
  from several tags cites their IFD. DateTime (306) is when the file changed, not a capture, and
  GPS time is a separate clock: both stay in their rows.
- ``position`` is GPSLatitude and GPSLongitude with their hemispheres, degrees, minutes and
  seconds read exactly and rounded once to a float; GPSAltitude with GPSAltitudeRef (0, the
  default, above sea level; 1 below) in metres above mean sea level. EXIF states the units, so
  they are ``Known`` citing the GPS IFD. GPSMapDatum is text, not a registry code: ``crs`` is
  ``Unknown`` and the datum stays in its row.
- ``orientation`` is Orientation (274), 1 to 8, kept and never applied.

A tag that is absent is ``Unknown`` citing where it was looked for; blank text is ``Unknown``; a
value that does not parse is ``Unknown`` with ``image.value_unreadable``. A format with no place
for these tags (BMP, Netpbm) has them ``NotCovered``.
"""

import calendar
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from fractions import Fraction
from typing import Final

from neptune.adapters.image._context import Context
from neptune.adapters.image._emit import VALUE_UNREADABLE
from neptune.adapters.image._tiff import RATIONAL, Entry, Ifd
from neptune.model.ids import LogicalId
from neptune.model.knowledge import AssertionKind, Knowledge, Known, NotCovered, Unknown
from neptune.model.provenance import Locator, Provenance
from neptune.model.reference import TimestampDomain
from neptune.model.spatial import CrsCode, GeodeticPosition, HeightReference
from neptune.model.time import INT64_MAX, ClockRole, Epoch, Timescale, Timestamp
from neptune.model.units import Unit, unit_from_json
from neptune.model.world import Capture

ORIENTATION: Final = 274
MAKE: Final = 271
MODEL: Final = 272
DATE_TIME_ORIGINAL: Final = 36867
OFFSET_TIME_ORIGINAL: Final = 36881
SUB_SEC_TIME_ORIGINAL: Final = 37521
BODY_SERIAL: Final = 42033
CAMERA_SERIAL: Final = 50735
GPS_LATITUDE_REF, GPS_LATITUDE, GPS_LONGITUDE_REF, GPS_LONGITUDE = 1, 2, 3, 4
GPS_ALTITUDE_REF, GPS_ALTITUDE, GPS_MAP_DATUM = 5, 6, 18

_DEGREE: Final = unit_from_json("deg")
_METRE: Final = unit_from_json("m")
_CIVIL: Final = re.compile(r"(\d{4}):(\d{2}):(\d{2}) (\d{2}):(\d{2}):(\d{2})")
_OFFSET: Final = re.compile(r"([+-])(\d{2}):(\d{2})")
_INT64_MIN: Final = -INT64_MAX - 1


class _Unreadable(Exception):
    pass


@dataclass(frozen=True)
class Tags:
    """Where one image's tags are: its IFDs, nearest first, and their Exif and GPS IFDs.

    ``looked`` is cited by an absent tag: the IFD the image's tags start in.
    """

    ifds: tuple[Ifd, ...]

    @property
    def looked(self) -> tuple[Locator, ...]:
        return self.ifds[0].locator

    def child(self, name: str) -> Ifd | None:
        for ifd in self.ifds:
            found = ifd.child(name)
            if found is not None:
                return found
        return None

    def find(self, tag: int, ifds: Sequence[Ifd] | None = None) -> tuple[Entry, Ifd] | None:
        for ifd in self.ifds if ifds is None else ifds:
            entry = ifd.get(tag)
            if entry is not None:
                return entry, ifd
        return None


def not_covered() -> tuple[Capture, Knowledge[int]]:
    """A format with no place for capture metadata or orientation."""
    return Capture(NotCovered(), NotCovered(), NotCovered(), NotCovered(), ()), NotCovered()


def unknown() -> tuple[Capture, Knowledge[int]]:
    """A format that could declare them and a file that declares none: no EXIF block at all."""
    return Capture(Unknown(), Unknown(), Unknown(), Unknown(), ()), Unknown()


class CaptureReader:
    """Reads one image's capture from ``tags``; findings and domains go to ``ctx``."""

    def __init__(self, ctx: Context, tags: Tags) -> None:
        self.ctx = ctx
        self.tags = tags

    def prov(self, locator: Sequence[Locator]) -> Provenance:
        return self.ctx.out.provenance(locator, AssertionKind.STATED)

    def unreadable(self, locator: Sequence[Locator], message: str, tag: int) -> None:
        self.ctx.out.finding(VALUE_UNREADABLE, locator, message, {"tag": tag})

    def read(self) -> tuple[Capture, Knowledge[int]]:
        identifiers = [
            ident
            for ident in (
                self._identifier(BODY_SERIAL, "exif.body_serial", self._exif_first()),
                self._identifier(CAMERA_SERIAL, "dng.camera_serial", None),
            )
            if ident is not None
        ]
        identifiers.sort(key=lambda known: (known.value.namespace, known.value.value))
        capture = Capture(
            time=self._time(),
            position=self._position(),
            device_manufacturer=self._text(MAKE),
            device_model=self._text(MODEL),
            device_identifiers=tuple(identifiers),
        )
        return capture, self._orientation()

    def _exif_first(self) -> list[Ifd]:
        exif = self.tags.child("Exif")
        return [*([exif] if exif is not None else []), *self.tags.ifds]

    def _string(self, entry: Entry) -> Knowledge[str]:
        prov = self.prov(entry.locator)
        items = entry.items or ()
        text = items[0] if items else None
        if isinstance(text, str) and text.strip():
            return Known(text, prov)
        return Unknown(prov)

    def _text(self, tag: int) -> Knowledge[str]:
        found = self.tags.find(tag)
        return Unknown(self.prov(self.tags.looked)) if found is None else self._string(found[0])

    def _identifier(
        self, tag: int, namespace: str, ifds: Sequence[Ifd] | None
    ) -> Known[LogicalId] | None:
        found = self.tags.find(tag, ifds)
        if found is None:
            return None
        text = self._string(found[0])
        if not isinstance(text, Known):
            return None
        return Known(LogicalId(namespace, text.value), text.provenance)

    def _orientation(self) -> Knowledge[int]:
        found = self.tags.find(ORIENTATION)
        if found is None:
            return Unknown(self.prov(self.tags.looked))
        entry = found[0]
        items = entry.items or ()
        if len(items) == 1 and isinstance(items[0], int) and 1 <= items[0] <= 8:
            return Known(items[0], self.prov(entry.locator))
        self.unreadable(
            entry.locator, f"Orientation is {list(items)}, not one of 1 to 8", entry.tag
        )
        return Unknown(self.prov(entry.locator))

    # --- Time ------------------------------------------------------------------------------------

    def _time(self) -> Knowledge[Timestamp]:
        found = self.tags.find(DATE_TIME_ORIGINAL, self._exif_first())
        if found is None:
            exif = self.tags.child("Exif")
            return Unknown(self.prov(exif.locator if exif is not None else self.tags.looked))
        entry, holder = found
        text = self._string(entry)
        if not isinstance(text, Known):
            return Unknown(self.prov(entry.locator))
        try:
            seconds = _civil(text.value)
        except _Unreadable:
            self.unreadable(
                entry.locator, "DateTimeOriginal is not 'YYYY:MM:DD HH:MM:SS'", entry.tag
            )
            return Unknown(self.prov(entry.locator))
        digits = self._sub_seconds(holder)
        offset = self._offset(holder)
        used_one = digits is None and offset is None
        locator = entry.locator if used_one else holder.locator
        scale = 10 ** len(digits or "")
        ticks = seconds * scale + int(digits or "0") - (offset or 0) * scale
        if not _INT64_MIN <= ticks <= INT64_MAX:
            self.unreadable(locator, "DateTimeOriginal does not fit 64-bit ticks", entry.tag)
            return Unknown(self.prov(locator))
        out = self.ctx.out
        domain_id = out.record_id(TimestampDomain.kind, locator)
        out.add(
            TimestampDomain(
                id=domain_id,
                provenance=self.prov(locator),
                field="DateTimeOriginal",
                scope=(),
                role=Known(ClockRole.SAMPLE),
                resolution=Known(Fraction(1, scale)),
                epoch=Known(Epoch.UNIX),
                timescale=Unknown() if offset is None else Known(Timescale.POSIX),
                declared_monotonic=Unknown(),
            )
        )
        return Known(Timestamp(ticks, domain_id), self.prov(locator))

    def _sub_seconds(self, holder: Ifd) -> str | None:
        entry = holder.get(SUB_SEC_TIME_ORIGINAL)
        if entry is None:
            return None
        text = self._string(entry)
        if not isinstance(text, Known):
            return None
        digits = text.value.rstrip(" ")
        if not digits.isascii() or not digits.isdigit() or len(digits) > 9:
            self.unreadable(entry.locator, "SubSecTimeOriginal is not 1 to 9 digits", entry.tag)
            return None
        return digits

    def _offset(self, holder: Ifd) -> int | None:
        entry = holder.get(OFFSET_TIME_ORIGINAL)
        if entry is None:
            return None
        text = self._string(entry)
        if not isinstance(text, Known) or not text.value.strip(" :"):
            return None
        match = _OFFSET.fullmatch(text.value)
        if match is None or int(match[2]) > 23 or int(match[3]) > 59:
            self.unreadable(entry.locator, "OffsetTimeOriginal is not '+HH:MM'", entry.tag)
            return None
        sign = -1 if match[1] == "-" else 1
        return sign * (int(match[2]) * 3600 + int(match[3]) * 60)

    # --- Position --------------------------------------------------------------------------------

    def _position(self) -> Knowledge[GeodeticPosition]:
        gps = self.tags.child("GPS")
        if gps is None:
            return Unknown(self.prov(self.tags.looked))
        prov = self.prov(gps.locator)
        if gps.get(GPS_LATITUDE) is None and gps.get(GPS_LONGITUDE) is None:
            return Unknown(prov)
        try:
            latitude = _degrees(gps, GPS_LATITUDE, GPS_LATITUDE_REF, "N", "S", 90)
            longitude = _degrees(gps, GPS_LONGITUDE, GPS_LONGITUDE_REF, "E", "W", 180)
        except _Unreadable as exc:
            self.unreadable(gps.locator, f"the GPS position is unreadable: {exc}", GPS_LATITUDE)
            return Unknown(prov)
        height: Knowledge[float] = Unknown(prov)
        unit: Knowledge[Unit] = Unknown(prov)
        reference: Knowledge[HeightReference] = Unknown(prov)
        if gps.get(GPS_ALTITUDE) is not None:
            try:
                height = Known(_altitude(gps), prov)
                unit, reference = Known(_METRE, prov), Known(HeightReference.MEAN_SEA_LEVEL, prov)
            except _Unreadable as exc:
                self.unreadable(gps.locator, f"the GPS altitude is unreadable: {exc}", GPS_ALTITUDE)
        datum = gps.get(GPS_MAP_DATUM)
        crs: Knowledge[CrsCode] = Unknown(self.prov(datum.locator if datum else gps.locator))
        position = GeodeticPosition(
            latitude, longitude, height, crs, Known(_DEGREE, prov), unit, reference
        )
        return Known(position, prov)


def _civil(text: str) -> int:
    match = _CIVIL.fullmatch(text)
    if match is None:
        raise _Unreadable(text)
    year, month, day, hour, minute, second = (int(part) for part in match.groups())
    try:
        datetime(year, month, day, hour, minute, second)  # range checks only: no zone is implied
    except ValueError as exc:
        raise _Unreadable(text) from exc
    return calendar.timegm((year, month, day, hour, minute, second, 0, 0, 0))


def _rationals(entry: Entry) -> list[Fraction]:
    if entry.type != RATIONAL or not entry.items:
        raise _Unreadable(f"tag {entry.tag} is not unsigned rationals")
    values = []
    for item in entry.items:
        if not isinstance(item, tuple) or item[1] == 0:
            raise _Unreadable(f"tag {entry.tag} has a zero denominator")
        values.append(Fraction(item[0], item[1]))
    return values


def _reference(gps: Ifd, tag: int) -> str:
    entry = gps.get(tag)
    items = (entry.items or ()) if entry is not None else ()
    if not items or not isinstance(items[0], str):
        raise _Unreadable(f"tag {tag} (the reference) is missing or not text")
    return items[0]


def _degrees(gps: Ifd, tag: int, ref_tag: int, positive: str, negative: str, bound: int) -> float:
    entry = gps.get(tag)
    if entry is None:
        raise _Unreadable(f"tag {tag} is missing")
    parts = _rationals(entry)
    if not 1 <= len(parts) <= 3:
        raise _Unreadable(f"tag {tag} holds {len(parts)} rationals, not 1 to 3")
    if any(part >= 60 for part in parts[1:]):
        raise _Unreadable(f"tag {tag} has minutes or seconds of 60 or more")
    value = sum((part / 60**index for index, part in enumerate(parts)), Fraction(0))
    if value > bound:
        raise _Unreadable(f"tag {tag} is {float(value)} degrees, past {bound}")
    ref = _reference(gps, ref_tag)
    if ref not in (positive, negative):
        raise _Unreadable(f"tag {ref_tag} is {ref!r}, not {positive} or {negative}")
    return float(-value if ref == negative else value)


def _altitude(gps: Ifd) -> float:
    entry = gps.get(GPS_ALTITUDE)
    assert entry is not None
    parts = _rationals(entry)
    if len(parts) != 1:
        raise _Unreadable(f"GPSAltitude holds {len(parts)} rationals, not 1")
    ref_entry = gps.get(GPS_ALTITUDE_REF)
    ref = 0 if ref_entry is None else (ref_entry.items or (None,))[0]
    if ref not in (0, 1):
        raise _Unreadable(f"GPSAltitudeRef is {ref!r}, not 0 or 1")
    return float(-parts[0] if ref == 1 else parts[0])
