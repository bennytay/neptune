"""Generate the ArduPilot DataFlash fixtures: a multicopter log, a rover (boat frame) log, an old
log with ``TimeMS`` and no units, and a few damaged copies.

Run ``uv run python tests/fixtures/ardupilot/make_dataflash_fixtures.py`` to rewrite every fixture,
then ``... --oracle`` to read each one with the official ``pymavlink`` (fetched by ``uv`` for that
run only, never a dependency) and record what it reads in ``oracle.json``.
``tests/unit/adapters/test_dataflash_fixtures.py`` checks that every committed file is what
``build()`` gives and that ``oracle.json`` agrees with the adapter.

The files are written by hand from the DataFlash format as ArduPilot's logger writes it (a FMT
record per message type, then FMTU, UNIT and MULT records for the declared units, then the data),
never by ArduPilot, so no flight, name or identifier is real. ``TimeUS`` is microseconds since boot.
"""

import json
import struct
import subprocess
import sys
from pathlib import Path
from typing import Final

HERE: Final = Path(__file__).parent
HEAD: Final = b"\xa3\x95"
SIZES: Final = {
    "a": 64, "b": 1, "B": 1, "h": 2, "H": 2, "i": 4, "I": 4, "f": 4, "d": 8, "n": 4, "N": 16,
    "Z": 64, "c": 2, "C": 2, "e": 4, "E": 4, "L": 4, "M": 1, "q": 8, "Q": 8,
}  # fmt: skip
CODES: Final = {
    "a": "32h", "b": "b", "B": "B", "h": "h", "H": "H", "i": "i", "I": "I", "f": "f", "d": "d",
    "n": "4s", "N": "16s", "Z": "64s", "c": "h", "C": "H", "e": "i", "E": "I", "L": "i", "M": "B",
    "q": "q", "Q": "Q",
}  # fmt: skip


class Log:
    """Records in the order the logger writes them."""

    def __init__(self) -> None:
        self.types: dict[str, tuple[int, str]] = {}
        self.out: list[bytes] = []
        self.declare("FMT", "BBnNZ", "Type,Length,Name,Format,Columns", number=128)

    def declare(self, name: str, chars: str, labels: str, number: int | None = None) -> None:
        number = number if number is not None else 129 + len(self.types) - 1
        length = 3 + sum(SIZES[c] for c in chars)
        self.types[name] = (number, chars)
        payload = struct.pack(
            "<BB4s16s64s", number, length, name.encode(), chars.encode(), labels.encode()
        )
        self.out.append(self.record(128, payload))

    @staticmethod
    def record(number: int, payload: bytes) -> bytes:
        return HEAD + bytes([number]) + payload

    def add(self, name: str, *values: object) -> None:
        number, chars = self.types[name]
        self.out.append(self.record(number, struct.pack("<" + "".join(CODES[c] for c in chars), *values)))

    def bytes(self) -> bytes:
        return b"".join(self.out)


UNITS: Final = {"-": "", "s": "s", "d": "deg", "m": "m", "n": "m/s", "i": "ms", "w": "week", "z": "Hz"}
MULTS: Final = {"-": 0.0, "0": 1.0, "B": 1e-2, "F": 1e-6, "G": 1e-7}


def header(log: Log, types: dict[str, tuple[str, str]], units: bool = True) -> None:
    """FMT records for every type, then the unit tables and one FMTU per type."""
    for name, (chars, labels) in types.items():
        log.declare(name, chars, labels)
    if units:
        log.declare("FMTU", "QBNN", "TimeUS,FmtType,UnitIds,MultIds")
        log.declare("UNIT", "QbZ", "TimeUS,Id,Label")
        log.declare("MULT", "Qbd", "TimeUS,Id,Mult")
        for ident, label in UNITS.items():
            log.add("UNIT", 0, ord(ident), label.encode())
        for ident, mult in MULTS.items():
            log.add("MULT", 0, ord(ident), mult)


def fmtu(log: Log, name: str, unit_ids: str, mult_ids: str) -> None:
    log.add("FMTU", 0, log.types[name][0], unit_ids.encode(), mult_ids.encode())


def text(value: str, size: int) -> bytes:
    return value.encode()[:size]


def copter() -> bytes:
    log = Log()
    header(
        log,
        {
            "PARM": ("QNff", "TimeUS,Name,Value,Default"),
            "MSG": ("QZ", "TimeUS,Message"),
            "ATT": ("QccccCC", "TimeUS,DesRoll,Roll,DesPitch,Pitch,DesYaw,Yaw"),
            "GPS": ("QBIHBcLLefffB", "TimeUS,Status,GMS,GWk,NSats,HDop,Lat,Lng,Alt,Spd,GCrs,VZ,U"),
            "MODE": ("QMBB", "TimeUS,Mode,ModeNum,Rsn"),
            "ARR": ("Qa", "TimeUS,Samples"),
            "RCOU": ("QHHHH", "TimeUS,C1,C2,C3,C4"),
        },
    )
    fmtu(log, "ATT", "sdddddd", "FBBBBBB")
    fmtu(log, "GPS", "s-iw--ddmndn-", "F-00-BGGB000-")
    fmtu(log, "MODE", "s---", "F---")
    fmtu(log, "RCOU", "szzzz", "F0000")
    log.add("MSG", 100, text("ArduCopter V4.5.1 (12345678)", 64))
    log.add("MSG", 101, text("Frame: QUAD/X", 64))
    log.add("PARM", 200, text("ATC_RAT_RLL_P", 16), 0.135, 0.135)
    log.add("PARM", 201, text("FRAME_CLASS", 16), 1.0, 1.0)
    log.add("PARM", 202, text("SYSID_THISMAV", 16), 1.0, 1.0)
    for k in range(6):
        t = 1_000_000 + 100_000 * k
        log.add("ATT", t, 100 * k, 98 * k, -50 * k, -49 * k, 9000 + k, 9001 + k)
        if k % 2 == 0:
            log.add(
                "GPS", t + 10, 3, 400_000 + 200 * k, 2310 + k, 12, 85, 473_977_420 + k,
                85_255_000 - k, 52_050 + 10 * k, 0.5 + k, 90.0 + k, -0.25, 1,
            )  # fmt: skip
        if k == 1:
            log.add("MODE", t + 20, 5, 5, 1)
            log.add("RCOU", t + 30, 1500, 1501, 1502, 1503)
        if k == 3:
            log.add("ARR", t + 40, *range(k, k + 32))
            log.add("MODE", t + 50, 6, 6, 2)
    return log.bytes()


def rover() -> bytes:
    """ArduRover on a boat frame (FRAME_CLASS 2): steering, wheel and GPS records, one text with
    bytes that are not UTF-8, and one time past 2^63 - 1."""
    log = Log()
    header(
        log,
        {
            "PARM": ("QNff", "TimeUS,Name,Value,Default"),
            "MSG": ("QZ", "TimeUS,Message"),
            "STER": ("Qffff", "TimeUS,SteerIn,SteerOut,DesTurnRate,TurnRate"),
            "WENC": ("Qffff", "TimeUS,Dist0,Spd0,Dist1,Spd1"),
            "GPS": ("QBIHBcLLefffB", "TimeUS,Status,GMS,GWk,NSats,HDop,Lat,Lng,Alt,Spd,GCrs,VZ,U"),
            "MODE": ("QMBB", "TimeUS,Mode,ModeNum,Rsn"),
            "NOTE": ("QN", "TimeUS,Tag"),
        },
    )
    fmtu(log, "STER", "s----", "F----")
    fmtu(log, "WENC", "smnmn", "F0000")
    log.add("MSG", 50, text("ArduRover V4.5.1 (abcdef12)", 64))
    log.add("MSG", 51, text("Frame: BOAT", 64))
    log.add("PARM", 60, text("FRAME_CLASS", 16), 2.0, 0.0)
    log.add("PARM", 61, text("WP_RADIUS", 16), 2.0, 2.0)
    for k in range(5):
        t = 2_000_000 + 50_000 * k
        log.add("STER", t, 0.1 * k, 0.12 * k, 5.0 * k, 4.5 * k)
        log.add("WENC", t + 5, 0.4 * k, 0.8 * k, 0.39 * k, 0.78 * k)
        if k % 2 == 1:
            log.add(
                "GPS", t + 10, 3, 500_000 + 100 * k, 2310, 9, 90, -337_000_000 + k, 1_510_000_000,
                1000 + k, 1.5, 270.0, 0.0, 1,
            )  # fmt: skip
    log.add("MODE", 2_300_000, 4, 4, 1)
    log.add("MSG", 2_400_000, b"bad \xff\xfe bytes")
    log.add("NOTE", 2**63 + 5, b"late")
    return log.bytes()


def legacy() -> bytes:
    """An old log: millisecond clock and no unit records."""
    log = Log()
    header(log, {"ATT": ("IccccCC", "TimeMS,DesRoll,Roll,DesPitch,Pitch,DesYaw,Yaw")}, units=False)
    for k in range(3):
        log.add("ATT", 4_000 + 20 * k, 10 * k, 9 * k, -10 * k, -9 * k, 100, 101)
    return log.bytes()


def build() -> dict[str, bytes]:
    whole = copter()
    return {
        "copter.bin": whole,
        "rover.bin": rover(),
        "legacy_timems.bin": legacy(),
        "copter_truncated.bin": whole[:-20],
    }


# --- The official reader as an oracle -----------------------------------------------------------


def oracle() -> str:
    """What pymavlink reads from each fixture, as JSON (run through ``uv run --with pymavlink``)."""
    code = f"""
import json
from pathlib import Path
from pymavlink import DFReader

out = {{}}
for path in sorted(Path({str(HERE)!r}).glob("*.bin")):
    log = DFReader.DFReader_binary(str(path), zero_time_base=True)
    messages = []
    while True:
        m = log.recv_match()
        if m is None:
            break
        fields = {{}}
        for name in m.get_fieldnames():
            value = getattr(m, name)
            if isinstance(value, bytes):
                value = value.decode("latin1")
            elif hasattr(value, "tolist"):
                value = value.tolist()
            fields[name] = value
        messages.append([m.get_type(), fields])
    out[path.name] = {{
        "messages": messages,
        "unit_lookup": getattr(log, "unit_lookup", {{}}),
        "mult_lookup": {{k: v for k, v in getattr(log, "mult_lookup", {{}}).items()}},
        "formats": {{f.name: [f.type, f.len, f.format, list(f.columns)] for f in log.formats.values()}},
    }}
print(json.dumps(out, indent=1, sort_keys=True))
"""
    result = subprocess.run(
        ["uv", "run", "--no-project", "--with", "pymavlink", "python", "-c", code],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def main() -> None:
    if sys.argv[1:] == ["--oracle"]:
        (HERE / "oracle.json").write_text(oracle())
        return
    for name, content in build().items():
        (HERE / name).write_bytes(content)


if __name__ == "__main__":
    main()
