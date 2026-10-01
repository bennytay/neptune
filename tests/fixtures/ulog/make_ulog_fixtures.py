"""Generate the PX4 ULog fixtures: two small logs (a multicopter and a ground rover), one with
appended data, and a few damaged copies.

Run ``uv run python tests/fixtures/ulog/make_ulog_fixtures.py`` to rewrite every fixture, then
``uv run python tests/fixtures/ulog/make_ulog_fixtures.py --oracle`` to read each one with the
official ``pyulog`` (fetched by ``uv`` for that run only, never a dependency) and record what it
reads in ``oracle.json``. ``tests/unit/adapters/test_ulog_fixtures.py`` checks that every committed
file is what ``build()`` gives and that ``oracle.json`` agrees with the adapter.

All timestamps are microseconds since boot, as ULog writes them. Data messages leave out the
trailing ``_padding`` fields of their format, as PX4's logger does. The files are written by hand
from the format specification (https://docs.px4.io/main/en/dev_log/ulog_file_format.html), never
by PX4, so no flight data, name or identifier is real.
"""

import struct
import subprocess
import sys
from pathlib import Path
from typing import Final

HERE: Final = Path(__file__).parent
MAGIC: Final = b"ULog\x01\x12\x35"
SYNC: Final = bytes([0x2F, 0x73, 0x13, 0x20, 0x25, 0x0C, 0xBB, 0x12])
BOOT: Final = 5_000_000  # the log starts 5 s after boot


def msg(kind: str, payload: bytes) -> bytes:
    return struct.pack("<HB", len(payload), ord(kind)) + payload


def header(version: int = 1, start: int = BOOT) -> bytes:
    return MAGIC + bytes([version]) + struct.pack("<Q", start)


def flag_bits(incompat0: int = 0, appended: tuple[int, int, int] = (0, 0, 0)) -> bytes:
    return msg("B", bytes(8) + bytes([incompat0]) + bytes(7) + struct.pack("<3Q", *appended))


def fmt(text: str) -> bytes:
    return msg("F", text.encode())


def key(declared: str, value: bytes, prefix: bytes = b"") -> bytes:
    return prefix + bytes([len(declared)]) + declared.encode() + value


def info(declared: str, value: bytes) -> bytes:
    return msg("I", key(declared, value))


def multi_info(declared: str, value: bytes, continued: int) -> bytes:
    return msg("M", key(declared, value, bytes([continued])))


def parameter(declared: str, value: bytes) -> bytes:
    return msg("P", key(declared, value))


def default_parameter(declared: str, value: bytes, defaults: int) -> bytes:
    return msg("Q", key(declared, value, bytes([defaults])))


def subscribe(multi: int, msg_id: int, name: str) -> bytes:
    return msg("A", struct.pack("<BH", multi, msg_id) + name.encode())


def data(msg_id: int, body: bytes) -> bytes:
    return msg("D", struct.pack("<H", msg_id) + body)


def logged(level: int, ts: int, text: str) -> bytes:
    return msg("L", struct.pack("<BQ", ord(str(level)), ts) + text.encode())


def tagged(level: int, tag: int, ts: int, text: str) -> bytes:
    return msg("C", struct.pack("<BHQ", ord(str(level)), tag, ts) + text.encode())


def sync() -> bytes:
    return msg("S", SYNC)


def dropout(milliseconds: int) -> bytes:
    return msg("O", struct.pack("<H", milliseconds))


def f32(value: float) -> float:
    (narrowed,) = struct.unpack("<f", struct.pack("<f", value))
    return float(narrowed)


# --- The multicopter ----------------------------------------------------------------------------

ATTITUDE: Final = (
    "vehicle_attitude:uint64_t timestamp;float[4] q;float rollspeed;float pitchspeed;"
    "float yawspeed;uint8_t[4] _padding0;"
)
STATUS: Final = (
    "vehicle_status:uint64_t timestamp;uint8_t nav_state;uint8_t arming_state;bool failsafe;"
    "uint8_t[5] _padding0;"
)
GPS: Final = (
    "vehicle_gps_position:uint64_t timestamp;uint64_t time_utc_usec;int32_t lat;int32_t lon;"
    "float eph;uint8_t fix_type;uint8_t[3] _padding0;"
)
ESC_REPORT: Final = (
    "esc_report:uint64_t timestamp;uint32_t esc_errorcount;int32_t esc_rpm;float esc_voltage;"
)
ESC_STATUS: Final = (
    "esc_status:uint64_t timestamp;uint8_t esc_count;esc_report[2] esc;uint8_t[7] _padding0;"
)
UNUSED: Final = "airspeed:uint64_t timestamp;float indicated_airspeed_m_s;uint8_t[4] _padding0;"


def attitude(ts: int, k: int) -> bytes:
    q = struct.pack("<4f", 1.0, 0.01 * k, -0.02 * k, 0.0)
    return struct.pack("<Q", ts) + q + struct.pack("<3f", 0.1 * k, -0.1 * k, 0.5)


def copter() -> bytes:
    out = [header(), flag_bits()]
    out += [fmt(ATTITUDE), fmt(STATUS), fmt(GPS), fmt(ESC_REPORT), fmt(ESC_STATUS), fmt(UNUSED)]
    out += [
        info("char[3] sys_name", b"PX4"),
        info("char[16] sys_uuid", b"0123456789abcdef"),
        info("char[13] ver_sw", b"v1.14.0-fixtu"),
        info("uint32_t ver_sw_release", struct.pack("<I", 0x010E0000)),
        info("char[10] vehicle", b"multicopte"),
        multi_info("char[5] perf", b"abcde", 1),
        multi_info("char[5] perf", b"fghij", 0),
        parameter("float MC_ROLL_P", struct.pack("<f", 6.5)),
        parameter("int32_t SYS_AUTOSTART", struct.pack("<i", 4001)),
        parameter("int32_t MAV_SYS_ID", struct.pack("<i", 1)),
        default_parameter("float MC_ROLL_P", struct.pack("<f", 6.5), 1),
    ]
    out += [
        subscribe(0, 0, "vehicle_attitude"),
        subscribe(0, 1, "vehicle_status"),
        subscribe(0, 2, "vehicle_gps_position"),
        subscribe(1, 3, "vehicle_gps_position"),
        subscribe(0, 4, "esc_status"),
        sync(),
    ]
    for k in range(8):
        ts = BOOT + 10_000 * k
        out.append(data(0, attitude(ts, k)))
        if k % 4 == 0:
            out.append(data(1, struct.pack("<QBB?", ts, 4 + k // 4, 2, k > 4)))
        if k % 2 == 0:
            gps = struct.pack(
                "<QQiifB", ts, 1_790_000_000_000_000 + ts, 473_977_420 + k, 85_255_000, 0.9, 3
            )
            out.append(data(2, gps))
        if k == 3:
            out.append(sync())
            out.append(logged(6, ts + 1, "Takeoff detected"))
            out.append(tagged(4, 7, ts + 2, "Low battery"))
            out.append(dropout(14))
            out.append(parameter("float MC_ROLL_P", struct.pack("<f", 7.25)))
        if k == 5:
            # the nested array is two esc_report: timestamp, errorcount, rpm, voltage each
            esc = struct.pack("<QB", ts, 2) + struct.pack("<QIif", ts, 0, 1200, 15.5)
            esc += struct.pack("<QIif", ts, 1, 1210, 15.25)
            out.append(data(4, esc))
            out.append(data(3, struct.pack("<QQiifB", ts, 0, 473_977_000, 85_255_000, 2.5, 2)))
    out.append(logged(7, BOOT + 90_000, "Landing complete"))
    return b"".join(out)


def copter_appended() -> bytes:
    """The multicopter log with a second section after the original end, as PX4 appends one
    (for example after a crash): flag bit 0 set and the section's offset in the flag bits."""
    base = copter()
    offset = len(base)
    tail = [
        sync(),
        subscribe(0, 5, "airspeed"),
        data(5, struct.pack("<Qf", BOOT + 100_000, f32(12.5))),
        logged(3, BOOT + 100_001, "Recovered after reboot"),
        parameter("int32_t SYS_AUTOSTART", struct.pack("<i", 4002)),
    ]
    patched = base[:16] + flag_bits(1, (offset, 0, 0)) + base[16 + len(flag_bits()) :]
    return patched + b"".join(tail)


# --- The ground rover ---------------------------------------------------------------------------

THROTTLE: Final = (
    "rover_throttle_setpoint:uint64_t timestamp;float throttle_body_x;float speed_body_x;"
)
STEERING: Final = (
    "rover_steering_setpoint:uint64_t timestamp;float normalized_steering_angle;float yaw_rate;"
)
WHEELS: Final = "wheel_encoders:uint64_t timestamp;int32_t[2] encoder_position;float[2] speed;uint8_t[4] _padding0;"
RATES: Final = "rover_rate_status:uint64_t timestamp;char[8] source;float measured_yaw_rate;uint8_t[4] _padding0;"


def rover() -> bytes:
    out = [header(1, 12_000_000), flag_bits()]
    out += [fmt(THROTTLE), fmt(STEERING), fmt(WHEELS), fmt(RATES)]
    out += [
        info("char[3] sys_name", b"PX4"),
        info("char[16] sys_uuid", b"rover-0123456789"),
        info("char[13] ver_sw", b"v1.15.0-rover"),
        info("char[5] vehicle", b"rover"),
        parameter("int32_t CA_AIRFRAME", struct.pack("<i", 6)),
        parameter("int32_t RD_WHEEL_TRACK", struct.pack("<i", 1)),
        parameter("float RD_MAX_SPEED", struct.pack("<f", 3.0)),
    ]
    out += [
        subscribe(0, 0, "rover_throttle_setpoint"),
        subscribe(0, 1, "rover_steering_setpoint"),
        subscribe(0, 2, "wheel_encoders"),
        subscribe(0, 3, "rover_rate_status"),
    ]
    for k in range(6):
        ts = 12_000_000 + 20_000 * k
        out.append(data(0, struct.pack("<Qff", ts, 0.25 * k, 0.5 * k)))
        out.append(data(1, struct.pack("<Qff", ts + 5, -0.1 * k, 0.05 * k)))
        out.append(data(2, struct.pack("<Q2i2f", ts + 7, 100 * k, 98 * k, 0.5 * k, 0.49 * k)))
    out.append(data(3, struct.pack("<Q8sf", 12_200_000, b"gyro_z\0\0", 0.125)))
    out.append(data(3, struct.pack("<Q8sf", 12_200_010, b"\xff\xfeconfu", 0.25)))
    out.append(logged(6, 12_100_000, "Rover armed"))
    out.append(sync())
    return b"".join(out)


def build() -> dict[str, bytes]:
    whole = copter()
    mid_message = len(whole) - 20  # inside the last logged message
    return {
        "copter.ulg": whole,
        "copter_appended.ulg": copter_appended(),
        "rover.ulg": rover(),
        "copter_truncated.ulg": whole[:mid_message],
        "empty_after_header.ulg": header(),
    }


# --- The official reader as an oracle -----------------------------------------------------------


def oracle() -> str:
    """What pyulog reads from each fixture, as JSON (run through ``uv run --with pyulog``)."""
    code = f"""
import contextlib, io, json, sys
from pathlib import Path
from pyulog import ULog

out = {{}}
for path in sorted(Path({str(HERE)!r}).glob("*.ulg")):
    if path.name in ("copter_truncated.ulg", "empty_after_header.ulg"):
        continue
    with contextlib.redirect_stdout(io.StringIO()):
        log = ULog(str(path))
    topics = {{}}
    for d in log.data_list:
        fields = {{}}
        for name, values in d.data.items():
            if name.startswith("_padding"):
                continue
            fields[name] = values.tolist()
        topics[f"{{d.name}}:{{d.multi_id}}"] = fields
    out[path.name] = {{
        "start_timestamp": log.start_timestamp,
        "file_version": log.file_version if hasattr(log, "file_version") else None,
        "info": {{k: (v if not isinstance(v, bytes) else v.decode("latin1")) for k, v in log.msg_info_dict.items()}},
        "initial_parameters": log.initial_parameters,
        "changed_parameters": [[t, k, v] for t, k, v in log.changed_parameters],
        "logged": [[m.timestamp, m.log_level, m.message] for m in log.logged_messages],
        "dropouts": [[d.timestamp, d.duration] for d in log.dropouts],
        "topics": topics,
    }}
print(json.dumps(out, indent=1, sort_keys=True))
"""
    result = subprocess.run(
        ["uv", "run", "--no-project", "--with", "pyulog", "python", "-c", code],
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
