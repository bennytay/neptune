"""Generate the software-identity fixtures (``tests/fixtures/software/``) byte for byte.

Run ``uv run python tests/fixtures/software/make_software_fixtures.py`` after changing one. Every
file is small, synthetic and deterministic: no real weights, no clock, no ``git`` at run time.
Binary layouts follow their specifications (ELF gABI and the GNU build-id note, systemd's
package-metadata note, ESP-IDF's ``esp_app_desc_t``, MCUboot's ``image_header``, safetensors,
ONNX's ``ModelProto``, ``torch.save``'s zip layout). With ``--check``, the generator also asks
official readers that are installed (``readelf``, ``onnx``, ``safetensors``) to read what it
wrote, as oracles; they are never a dependency of the tests.

A git directory cannot be committed inside a repository, so ``git/`` holds the files of one
under plain names; the tests that need a ``.git`` tree build it from them.
"""

import base64
import hashlib
import io
import json
import pickle
import shutil
import struct
import subprocess
import sys
import zipfile
import zlib
from pathlib import Path
from typing import Final

HERE: Final = Path(__file__).parent

SHA_MAIN: Final = "8f3c2a1d9e7b6c5a4f3e2d1c0b9a8f7e6d5c4b3a"
SHA_DEV: Final = "17d0c3b2a1f0e9d8c7b6a5f4e3d2c1b0a9f8e7d6"
SHA_TAG_OBJECT: Final = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
SHA_TAG_COMMIT: Final = "0f1e2d3c4b5a69788796a5b4c3d2e1f00f1e2d3c"
SHA_IK_SOLVER: Final = "3c9e1b7a5d2f4e6081a3c5e7f9b1d3f5a7c9e1b3"
BUILD_ID: Final = bytes.fromhex("5e1f00d2c3b4a5968778695a4b3c2d1e0f1a2b3c")
IMAGE_DIGEST: Final = "sha256:" + hashlib.sha256(b"amr-runtime arm64 manifest").hexdigest()
IMAGE_DIGEST_AMD64: Final = "sha256:" + hashlib.sha256(b"amr-runtime amd64 manifest").hexdigest()
NAV_DIGEST: Final = hashlib.sha256(b"nav2-runtime manifest").hexdigest()
POLICY_SHA256: Final = hashlib.sha256(b"pick-policy weights").hexdigest()
POLICY_SHA512: Final = hashlib.sha512(b"pick-policy weights").hexdigest()
ESP_ELF_SHA256: Final = hashlib.sha256(b"gripper_fw.elf").digest()


def _json(value: object) -> bytes:
    return (json.dumps(value, indent=2) + "\n").encode()


# --- git ---------------------------------------------------------------------------------------


def git_files() -> dict[str, bytes]:
    packed = (
        "# pack-refs with: peeled fully-peeled sorted \n"
        f"{SHA_MAIN} refs/heads/main\n"
        f"{SHA_DEV} refs/remotes/origin/dev\n"
        f"{SHA_TAG_OBJECT} refs/tags/v1.4.2\n"
        f"^{SHA_TAG_COMMIT}\n"
    )
    return {
        "git/HEAD": b"ref: refs/heads/main\n",
        "git/HEAD_detached": f"{SHA_MAIN}\n".encode(),
        "git/ORIG_HEAD": f"{SHA_DEV}\n".encode(),
        "git/packed-refs": packed.encode(),
        # git before 1.6 wrote no header, so a tag's object may be a tag object.
        "git/packed-refs_unpeeled": (
            f"{SHA_MAIN} refs/heads/main\n{SHA_TAG_OBJECT} refs/tags/v1.0\n".encode()
        ),
        "git/packed-refs_corrupt": (
            f"# pack-refs with: peeled \n{SHA_MAIN} refs/heads/main\nnot a ref\n{SHA_DEV[:12]}"
        ).encode(),
        # One line of hex that is a checksum, not a ref: its name says so, its bytes cannot.
        "git/release.sha256": (hashlib.sha256(b"release").hexdigest() + "\n").encode(),
    }


# --- Build manifests ---------------------------------------------------------------------------

PACKAGE_XML: Final = b"""<?xml version="1.0"?>
<?xml-model href="http://download.ros.org/schema/package_format3.xsd" schematypens="http://www.w3.org/2001/XMLSchema"?>
<package format="3">
  <name>arm_controller</name>
  <version>2.1.0</version>
  <description>Joint trajectory controller for a six-axis arm.</description>
  <maintainer email="robotics@example.com">Robotics Team</maintainer>
  <license>Apache-2.0</license>
  <buildtool_depend>ament_cmake</buildtool_depend>
  <depend>rclcpp</depend>
  <export>
    <build_type>ament_cmake</build_type>
  </export>
</package>
"""

PACKAGE_ENTITY: Final = b"""<?xml version="1.0"?>
<!DOCTYPE package [
  <!ENTITY a "aaaaaaaaaa">
  <!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">
]>
<package format="2"><name>&b;</name><version>1.0.0</version></package>
"""

PYPROJECT: Final = b"""[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "grasp-planner"
version = "0.7.3"
description = "Grasp planning for a parallel-jaw gripper"
requires-python = ">=3.11"
dependencies = ["numpy>=2"]
"""

CMAKE: Final = b"""cmake_minimum_required(VERSION 3.16)
# The lidar driver's top-level project. ( a parenthesis in a comment
project(lidar_driver
  VERSION 1.8.0
  DESCRIPTION "Driver for a spinning lidar (UDP)"
  LANGUAGES CXX)

add_library(lidar_driver src/driver.cpp)
"""

SETUP_PY: Final = b'''"""Perception utilities."""
from setuptools import find_packages, setup

setup(
    name="perception_utils",
    version="0.9.0",
    packages=find_packages(),
)
'''


def manifest_files() -> dict[str, bytes]:
    return {
        "manifests/package.xml": PACKAGE_XML,
        "manifests/package_bad_version.xml": PACKAGE_XML.replace(b"2.1.0", b"2.1"),
        "manifests/package_entity.xml": PACKAGE_ENTITY,
        "manifests/package_truncated.xml": PACKAGE_XML[: PACKAGE_XML.index(b"</version>") + 4],
        "manifests/pyproject.toml": PYPROJECT,
        "manifests/pyproject_dynamic.toml": PYPROJECT.replace(
            b'version = "0.7.3"', b'dynamic = ["version"]'
        ),
        "manifests/pyproject_conflict.toml": PYPROJECT
        + b'\n[tool.poetry]\nname = "grasp-planner"\nversion = "0.7.4"\n',
        "manifests/Cargo.toml": (
            b'[package]\nname = "motor-driver"\nversion = "0.3.1"\nedition = "2021"\n\n'
            b'[dependencies]\nserialport = "4"\n'
        ),
        "manifests/Cargo_member.toml": (
            b'[package]\nname = "motor-driver"\nversion.workspace = true\nedition = "2021"\n'
        ),
        "manifests/Cargo_workspace.toml": b'[workspace]\nmembers = ["motor-driver"]\n',
        "manifests/CMakeLists.txt": CMAKE,
        "manifests/CMakeLists_variable.txt": (
            b"cmake_minimum_required(VERSION 3.16)\n"
            b"project(${DRIVER_NAME} VERSION ${DRIVER_VERSION})\n"
        ),
        "manifests/CMakeLists_unterminated.txt": CMAKE.replace(b'(UDP)"', b"(UDP)"),
        "manifests/setup_py": SETUP_PY,
        "manifests/setup_dynamic_py": SETUP_PY.replace(b'"0.9.0"', b"read_version()"),
        "manifests/setup_broken_py": b"from setuptools import setup\nsetup(name='x', version='1'\n",
    }


# --- Lockfiles ---------------------------------------------------------------------------------

NUMPY_SDIST: Final = "https://files.pythonhosted.org/numpy-2.1.0.tar.gz"
POETRY_HEADER: Final = (
    "# This file is automatically @generated by Poetry 1.8.3 and should not be changed by hand."
)

UV_LOCK: Final = f"""version = 1
revision = 2
requires-python = ">=3.11"

[[package]]
name = "grasp-planner"
source = {{ editable = "." }}
dependencies = [
    {{ name = "ik-solver" }},
    {{ name = "numpy" }},
]

[[package]]
name = "ik-solver"
version = "0.2.0"
source = {{ git = "https://github.com/example/ik-solver?rev=v0.2.0#{SHA_IK_SOLVER}" }}

[[package]]
name = "numpy"
version = "2.1.0"
source = {{ registry = "https://pypi.org/simple" }}
sdist = {{ url = "{NUMPY_SDIST}", hash = "sha256:{"1" * 64}", size = 18874 }}
""".encode()

POETRY_LOCK: Final = f"""{POETRY_HEADER}

[[package]]
name = "ik-solver"
version = "0.2.0"
description = "Inverse kinematics"
optional = false
python-versions = ">=3.11"
files = []

[package.source]
type = "git"
url = "https://github.com/example/ik-solver"
reference = "v0.2.0"
resolved_reference = "{SHA_IK_SOLVER}"

[[package]]
name = "pyserial"
version = "3.5"
description = "Serial port access"
optional = false
python-versions = "*"
files = [
    {{file = "pyserial-3.5.tar.gz", hash = "sha256:{"2" * 64}"}},
]

[metadata]
lock-version = "2.0"
python-versions = "^3.11"
content-hash = "{"3" * 64}"
""".encode()

CARGO_LOCK: Final = f"""# This file is automatically @generated by Cargo.
# It is not intended for manual editing.
version = 4

[[package]]
name = "motor-driver"
version = "0.3.1"
dependencies = [
 "serialport",
 "trajectory",
]

[[package]]
name = "serialport"
version = "4.5.1"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "{"4" * 64}"

[[package]]
name = "trajectory"
version = "0.1.0"
source = "git+https://github.com/example/trajectory?branch=main#{SHA_MAIN}"
""".encode()

NPM_LOCK: Final = {
    "name": "operator-console",
    "version": "1.3.0",
    "lockfileVersion": 3,
    "requires": True,
    "packages": {
        "": {"name": "operator-console", "version": "1.3.0", "workspaces": ["packages/map"]},
        "node_modules/@example/map": {"resolved": "packages/map", "link": True},
        "node_modules/roslib": {
            "version": "1.4.1",
            "resolved": "https://registry.npmjs.org/roslib/-/roslib-1.4.1.tgz",
            "integrity": "sha512-" + base64.b64encode(hashlib.sha512(b"roslib").digest()).decode(),
        },
        "node_modules/teleop": {
            "version": "0.5.0",
            "resolved": f"git+ssh://git@github.com/example/teleop.git#{SHA_DEV}",
        },
        "packages/map": {"name": "@example/map", "version": "0.1.0"},
    },
}

NPM_LOCK_V1: Final = {
    "name": "legacy-console",
    "version": "0.4.0",
    "lockfileVersion": 1,
    "requires": True,
    "dependencies": {
        "roslib": {
            "version": "1.1.0",
            "requires": {"eventemitter2": "^6"},
            "dependencies": {"eventemitter2": {"version": "6.4.9"}},
        },
        "teleop": {"version": f"git+ssh://git@github.com/example/teleop.git#{SHA_DEV}"},
    },
}


def lockfile_files() -> dict[str, bytes]:
    duplicate = _json(NPM_LOCK).replace(
        b'"version": "1.3.0",', b'"version": "1.3.0", "version": "9",', 1
    )
    return {
        "lockfiles/uv.lock": UV_LOCK,
        "lockfiles/uv_truncated.lock": UV_LOCK[: UV_LOCK.index(b'version = "2.1.0"') + 12],
        "lockfiles/poetry.lock": POETRY_LOCK,
        "lockfiles/Cargo.lock": CARGO_LOCK,
        "lockfiles/Cargo_bad.lock": CARGO_LOCK.replace(b'version = "4.5.1"', b'version = "4.5"'),
        "lockfiles/package-lock.json": _json(NPM_LOCK),
        "lockfiles/package-lock_v1.json": _json(NPM_LOCK_V1),
        "lockfiles/package-lock_duplicate.json": duplicate,
    }


# --- Firmware ----------------------------------------------------------------------------------


def _note(name: bytes, kind: int, desc: bytes, order: str = "<") -> bytes:
    """One ELF note, name and descriptor padded to 4 bytes."""

    def pad(data: bytes) -> bytes:
        return data + b"\x00" * (-len(data) % 4)

    return struct.pack(order + "III", len(name), len(desc), kind) + pad(name) + pad(desc)


PACKAGE_NOTE: Final = json.dumps(
    {"type": "deb", "name": "nav-node", "version": "1.2.0-1", "architecture": "arm64"},
    separators=(",", ":"),
).encode()


def elf64(notes: list[tuple[bytes, bytes]], kind: int = 3) -> bytes:
    """A little-endian ELF64 (aarch64) with one SHT_NOTE section per ``(section name, note)``."""
    names = b"\x00"
    sections: list[tuple[int, int, int, int]] = []  # name offset, type, offset, size
    body = b""
    offset = 64
    for section, note in notes:
        sections.append((len(names), 7, offset + len(body), len(note)))
        names += section + b"\x00"
        body += note
    shstrtab = len(names)
    names += b".shstrtab\x00"
    sections.append((shstrtab, 3, offset + len(body), len(names)))
    body += names
    body += b"\x00" * (-len(body) % 8)
    shoff = offset + len(body)
    header = b"\x7fELF" + bytes([2, 1, 1, 0]) + b"\x00" * 8
    header += struct.pack(
        "<HHIQQQIHHHHHH",
        kind,
        0xB7,
        1,
        0,
        0,
        shoff,
        0,
        64,
        56,
        0,
        64,
        len(sections) + 1,
        len(sections),
    )
    table = b"\x00" * 64
    for name, sh_type, sh_offset, size in sections:
        table += struct.pack("<IIQQQQIIQQ", name, sh_type, 0, 0, sh_offset, size, 0, 0, 4, 0)
    return header + body + table


def elf32_big_endian() -> bytes:
    """A big-endian ELF32 (MIPS) executable without section headers: its note is a PT_NOTE."""
    note = _note(b"GNU\x00", 3, BUILD_ID[::-1], ">")
    phoff, note_at = 52, 52 + 32
    header = b"\x7fELF" + bytes([1, 2, 1, 0]) + b"\x00" * 8
    header += struct.pack(">HHIIIIIHHHHHH", 2, 0x08, 1, 0, phoff, 0, 0, 52, 32, 1, 40, 0, 0)
    phdr = struct.pack(">IIIIIIII", 4, note_at, 0, 0, len(note), len(note), 4, 4)
    return header + phdr + note


def esp_image() -> bytes:
    image = bytearray(512)
    image[0:4] = bytes([0xE9, 1, 2, 0x20])
    struct.pack_into("<I", image, 4, 0x40080000)
    image[8] = 0xEE
    struct.pack_into("<H", image, 12, 9)  # chip id: ESP32-S3
    struct.pack_into("<II", image, 24, 0x3C000020, 480)  # first segment: load address, length
    struct.pack_into("<II", image, 32, 0xABCD5432, 1)  # app descriptor magic, secure version
    for offset, text in (
        (48, b"v2.3.1-robot"),
        (80, b"gripper_fw"),
        (112, b"12:00:00"),
        (128, b"Sep 30 2026"),
        (144, b"v5.1.2"),
    ):
        image[offset : offset + len(text)] = text
    image[176:208] = ESP_ELF_SHA256
    return bytes(image)


def mcuboot_image(declared: int = 64, present: int = 64) -> bytes:
    header = struct.pack("<IIHHII", 0x96F3B83D, 0, 32, 0, declared, 0)
    header += struct.pack("<BBHI", 1, 4, 0, 12) + b"\x00" * 4
    tlv = struct.pack("<HH", 0x6907, 4)
    return header + bytes(range(present)) + (tlv if present == declared else b"")


def px4_firmware() -> bytes:
    image = base64.b64encode(zlib.compress(b"\x00" * 64, 9)).decode()
    return _json(
        {
            "board_id": 53,
            "magic": "PX4FWv1",
            "description": "Firmware for the PX4_FMU_V6X board",
            "image": image,
            "build_time": 1790762400,
            "summary": "PX4_FMU_V6X",
            "version": "0.1",
            "image_size": 64,
            "image_maxsize": 2080768,
            "git_identity": f"v1.15.0-12-g{SHA_MAIN[:7]}",
            "git_hash": SHA_MAIN,
            "board_revision": 0,
        }
    )


def apj_firmware() -> bytes:
    image = base64.b64encode(zlib.compress(b"\x00" * 64, 9)).decode()
    return _json(
        {
            "board_id": 140,
            "magic": "APJFWv1",
            "description": "Firmware for a STM32H743xx board",
            "image": image,
            "summary": "CubeOrange",
            "version": "0.1",
            "image_size": 64,
            "git_identity": SHA_DEV[:8],
            "board_revision": 0,
            "USBID": "0x2DAE/0x1016",
        }
    )


def firmware_files() -> dict[str, bytes]:
    app = elf64(
        [
            (b".note.gnu.build-id", _note(b"GNU\x00", 3, BUILD_ID)),
            (b".note.package", _note(b"FDO\x00", 0xCAFE1A7E, PACKAGE_NOTE + b"\x00")),
        ]
    )
    shoff = struct.unpack_from("<Q", app, 40)[0]
    return {
        "firmware/app.elf": app,
        "firmware/app32be.elf": elf32_big_endian(),
        "firmware/stripped.elf": elf64([]),
        "firmware/truncated.elf": app[: shoff + 70],
        "firmware/object.o": elf64(
            [(b".note.gnu.build-id", _note(b"GNU\x00", 3, BUILD_ID))], kind=1
        ),
        "firmware/esp_app.bin": esp_image(),
        "firmware/esp_truncated.bin": esp_image()[:100],
        "firmware/mcuboot.bin": mcuboot_image(),
        "firmware/mcuboot_truncated.bin": mcuboot_image(declared=64, present=20),
        "firmware/firmware.px4": px4_firmware(),
        "firmware/firmware.apj": apj_firmware(),
    }


# --- Checkpoints -------------------------------------------------------------------------------


def safetensors(tensors: dict[str, tuple[str, list[int], int]], data: bytes) -> bytes:
    header: dict[str, object] = {"__metadata__": {"format": "pt"}}
    offset = 0
    for name, (dtype, shape, size) in tensors.items():
        header[name] = {"dtype": dtype, "shape": shape, "data_offsets": [offset, offset + size]}
        offset += size
    raw = json.dumps(header, separators=(",", ":")).encode()
    raw += b" " * (-len(raw) % 8)
    return struct.pack("<Q", len(raw)) + raw + data


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        out.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(out)


def _field(number: int, payload: bytes | int) -> bytes:
    if isinstance(payload, int):
        return _varint(number << 3) + _varint(payload)
    return _varint(number << 3 | 2) + _varint(len(payload)) + payload


def onnx_model(model_version: int | None = 3) -> bytes:
    """A ModelProto: ir_version 9, producer, model_version, a one-node Identity graph, opset 17."""
    value_info = _field(1, b"obs") + _field(2, _field(1, _field(1, 1)))  # name, float tensor type
    node = _field(1, b"obs") + _field(2, b"action") + _field(4, b"Identity")
    graph = _field(1, node) + _field(2, b"policy") + _field(11, value_info)
    graph += _field(12, value_info.replace(b"obs", b"action").replace(b"\x0a\x03", b"\x0a\x06"))
    model = _field(1, 9) + _field(2, b"neptune-fixtures") + _field(3, b"1.0")
    if model_version is not None:
        model += _field(5, model_version)
    return model + _field(7, graph) + _field(8, _field(2, 17))


def pytorch_archive() -> bytes:
    buffer = io.BytesIO()
    members = [
        ("policy/data.pkl", pickle.dumps({"policy": "fixture"}, protocol=2)),
        ("policy/byteorder", b"little"),
        ("policy/data/0", b"\x00" * 16),
        ("policy/version", b"3\n"),
        ("policy/.data/serialization_id", b"1234567890123456789012345678901234567890"),
    ]
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
        for name, data in members:
            archive.writestr(zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0)), data)
    return buffer.getvalue()


def checkpoint_files() -> dict[str, bytes]:
    tensors = {"actor.weight": ("F32", [2, 2], 16), "actor.bias": ("F32", [2], 8)}
    policy = safetensors(tensors, b"\x00" * 24)
    onnx = onnx_model()
    torch = pytorch_archive()
    bad_header = struct.pack("<Q", 40) + b'{"actor.weight": {"dtype": "F32", "sha'
    return {
        "checkpoints/policy.safetensors": policy,
        "checkpoints/policy_truncated.safetensors": policy[:-10],
        "checkpoints/policy_bad_header.safetensors": bad_header + b"}" * 8,
        "checkpoints/policy.onnx": onnx,
        "checkpoints/policy_noversion.onnx": onnx_model(model_version=None),
        "checkpoints/policy_truncated.onnx": onnx[: len(onnx) - 30],
        "checkpoints/policy.pt": torch,
        "checkpoints/policy_truncated.pt": torch[: torch.index(b"PK\x01\x02") + 10],
    }


# --- SBOMs and image indexes -------------------------------------------------------------------

CYCLONEDX: Final = {
    "bomFormat": "CycloneDX",
    "specVersion": "1.5",
    "serialNumber": "urn:uuid:3e671687-395b-41f5-a30f-a58921a69b79",
    "version": 1,
    "metadata": {
        "component": {"type": "application", "name": "warehouse-amr-stack", "version": "4.2.0"}
    },
    "components": [
        {
            "type": "machine-learning-model",
            "name": "pick-policy",
            "version": "2026.09.1",
            "hashes": [
                {"alg": "SHA-512", "content": POLICY_SHA512},
                {"alg": "SHA-256", "content": POLICY_SHA256},
            ],
        },
        {
            "type": "container",
            "name": "nav2-runtime",
            "version": "humble",
            "purl": f"pkg:oci/nav2-runtime@sha256%3A{NAV_DIGEST}?repository_url=ghcr.io/example",
        },
        {"type": "firmware", "name": "motor-controller", "version": "0x010E"},
        {
            "type": "library",
            "name": "eigen",
            "version": "3.4.0",
            "components": [{"type": "library", "name": "eigen-blas"}],
        },
        {"type": "container", "name": "unpinned-sidecar"},
    ],
}

SPDX: Final = {
    "spdxVersion": "SPDX-2.3",
    "dataLicense": "CC0-1.0",
    "SPDXID": "SPDXRef-DOCUMENT",
    "name": "amr-image",
    "documentNamespace": "https://example.com/spdx/amr-image",
    "creationInfo": {"created": "2026-09-30T10:00:00Z", "creators": ["Tool: example-1.0"]},
    "packages": [
        {
            "SPDXID": "SPDXRef-image",
            "name": "amr-image",
            "versionInfo": "1.2.0",
            "primaryPackagePurpose": "CONTAINER",
            "downloadLocation": "NOASSERTION",
            "externalRefs": [
                {
                    "referenceCategory": "PACKAGE-MANAGER",
                    "referenceType": "purl",
                    "referenceLocator": "pkg:docker/example/amr-image@"
                    + IMAGE_DIGEST.replace(":", "%3A"),
                }
            ],
        },
        {
            "SPDXID": "SPDXRef-lidar",
            "name": "lidar-firmware",
            "versionInfo": "3.0.7",
            "primaryPackagePurpose": "FIRMWARE",
            "downloadLocation": "NOASSERTION",
        },
        {
            "SPDXID": "SPDXRef-libyaml",
            "name": "libyaml",
            "versionInfo": "NOASSERTION",
            "downloadLocation": "NOASSERTION",
        },
    ],
}

OCI_INDEX: Final = {
    "schemaVersion": 2,
    "mediaType": "application/vnd.oci.image.index.v1+json",
    "manifests": [
        {
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "digest": IMAGE_DIGEST,
            "size": 1024,
            "platform": {"architecture": "arm64", "os": "linux"},
            "annotations": {
                "org.opencontainers.image.ref.name": "amr-runtime",
                "org.opencontainers.image.version": "1.2.0",
                "org.opencontainers.image.revision": SHA_MAIN,
            },
        },
        {
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "digest": IMAGE_DIGEST_AMD64,
            "size": 1024,
            "platform": {"architecture": "amd64", "os": "linux"},
        },
    ],
}


def sbom_files() -> dict[str, bytes]:
    bad_digest = json.loads(json.dumps(OCI_INDEX))
    bad_digest["manifests"][1]["digest"] = "md5:" + "0" * 32
    return {
        "sbom/robot.cdx.json": _json(CYCLONEDX),
        "sbom/robot.spdx.json": _json(SPDX),
        "sbom/index.json": _json(OCI_INDEX),
        "sbom/index_bad_digest.json": _json(bad_digest),
        "sbom/duplicate.cdx.json": _json(CYCLONEDX).replace(
            b'"version": 1,', b'"version": 1,\n  "version": 2,', 1
        ),
    }


def build() -> dict[str, bytes]:
    files = {
        **git_files(),
        **manifest_files(),
        **lockfile_files(),
        **firmware_files(),
        **checkpoint_files(),
        **sbom_files(),
    }
    assert all(len(data) < 512 * 1024 for data in files.values())
    return files


def _oracles(files: dict[str, bytes], root: Path) -> None:
    """Ask installed official readers to read what was written (``--check``)."""
    if shutil.which("readelf"):
        notes = subprocess.run(
            ["readelf", "-n", str(root / "firmware/app.elf")],
            capture_output=True,
            check=True,
            text=True,
        ).stdout
        assert BUILD_ID.hex() in notes, notes
    try:
        import onnx  # type: ignore[import-not-found]

        model = onnx.load_from_string(files["checkpoints/policy.onnx"])
        assert model.model_version == 3 and model.ir_version == 9, model
    except ImportError:
        pass
    try:
        from safetensors import safe_open  # type: ignore[import-not-found]

        with safe_open(str(root / "checkpoints/policy.safetensors"), framework="numpy") as f:
            assert sorted(f.keys()) == ["actor.bias", "actor.weight"]
    except ImportError:
        pass


if __name__ == "__main__":
    written = build()
    for relative, data in written.items():
        path = HERE / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    if "--check" in sys.argv:
        _oracles(written, HERE)
