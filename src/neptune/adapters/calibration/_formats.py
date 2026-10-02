"""Which calibration format a document is, told by the keys it must hold (ADR 0055 §2).

A format is claimed only where its required keys are all there, never from a name or one common
word: a ``camera_matrix`` alone is in a hundred file kinds. ROS ``camera_info`` and OpenCV's
FileStorage both write ``camera_matrix``; OpenCV's marks itself (a ``%YAML:1.0`` header, an
``opencv-matrix`` tag or ``type_id``), so a document that carries the mark is OpenCV's and one
that does not is ROS's. Nothing is guessed from the values.
"""

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from neptune.adapters.calibration._items import Item, Kind
from neptune.model.provenance import Locator


class CalibrationFormat(StrEnum):
    ROS_CAMERA_INFO = "ros_camera_info"  # camera_calibration_parsers YAML
    ROS_CAMERA_INFO_MESSAGE = "ros_camera_info_message"  # sensor_msgs/CameraInfo as YAML or JSON
    KALIBR = "kalibr"  # camchain, camchain-imucam and imu YAML
    OPENCV_YAML = "opencv_yaml"  # cv::FileStorage YAML
    OPENCV_XML = "opencv_xml"  # cv::FileStorage XML


# ROS camera_info (camera_calibration_parsers): camera_matrix{rows, cols, data} and one of these.
ROS_COMPANIONS: Final = (
    "distortion_model",
    "distortion_coefficients",
    "projection_matrix",
    "image_width",
)
# sensor_msgs/CameraInfo's own field names.
ROS_MESSAGE_KEYS: Final = ("K", "P", "distortion_model")
KALIBR_CAMERA: Final = re.compile(r"cam[0-9]+")
KALIBR_IMU: Final = re.compile(r"imu[0-9]+")
KALIBR_CAMERA_KEYS: Final = ("camera_model", "intrinsics")
KALIBR_IMU_KEYS: Final = ("accelerometer_noise_density", "gyroscope_noise_density")
# What an OpenCV file must name for it to be a camera calibration: tutorial, stereo and
# Autoware-style names.
OPENCV_KEYS: Final = frozenset(
    (
        "camera_matrix",
        "cameraMatrix",
        "CameraMat",
        "CameraExtrinsicMat",
        "distortion_coefficients",
        "distCoeffs",
        "dist_coeffs",
        "DistCoeff",
        "M1",
        "M2",
    )
)
# Kalibr's extrinsics, with the frame the key names besides the entry's own (its documented
# meaning: ``T_cam_imu`` maps IMU coordinates into the camera's).
KALIBR_IMU_FRAME: Final = "T_cam_imu"
KALIBR_PREVIOUS_CAMERA: Final = "T_cn_cnm1"


@dataclass(frozen=True)
class Entry:
    """One calibrated subject of a document: the item that declares it and what it is named."""

    item: Item
    subject: str | None  # a declared name, or None
    subject_where: Locator | None = None  # where the name is written
    camera: bool = False  # a Kalibr camera entry: it may declare extrinsics


@dataclass(frozen=True)
class Recognised:
    format: CalibrationFormat
    entries: tuple[Entry, ...]
    unread: tuple[str, ...] = ()  # root keys of the document no entry covers


def _has_matrix(item: Item | None) -> bool:
    return item is not None and item.kind is Kind.MAPPING and item.child("data") is not None


def _text(item: Item | None) -> str | None:
    """A scalar's declared text, if it is a string."""
    if item is None or item.kind is not Kind.SCALAR or len(item.readings) != 1:
        return None
    return item.text


def recognise(root: Item, *, opencv: bool = False, xml: bool = False) -> Recognised | None:
    """The format a document root is, or ``None``. ``opencv``: the text begins with the
    ``%YAML:1.0`` line OpenCV writes, or (``xml``) the root element is ``opencv_storage``."""
    if root.kind is not Kind.MAPPING:
        return None
    entries: list[Entry] = []
    for child in root.children:
        if child.kind is not Kind.MAPPING:
            continue
        if KALIBR_CAMERA.fullmatch(child.name) and all(child.child(k) for k in KALIBR_CAMERA_KEYS):
            entries.append(Entry(child, child.name, child.where, camera=True))
        elif KALIBR_IMU.fullmatch(child.name) and any(child.child(k) for k in KALIBR_IMU_KEYS):
            entries.append(Entry(child, child.name, child.where))
    if entries:
        covered = {entry.item.name for entry in entries}
        unread = tuple(c.name for c in root.children if c.name not in covered)
        return Recognised(CalibrationFormat.KALIBR, tuple(entries), unread)
    names = {child.name for child in root.children}
    if all(root.child(k) is not None for k in KALIBR_IMU_KEYS):  # Kalibr's own imu.yaml: flat
        return Recognised(CalibrationFormat.KALIBR, (Entry(root, None),))
    if opencv or any(child.is_matrix for child in root.children):
        if names & OPENCV_KEYS:
            fmt = CalibrationFormat.OPENCV_XML if xml else CalibrationFormat.OPENCV_YAML
            return Recognised(fmt, (Entry(root, None),))
        return None
    if _has_matrix(root.child("camera_matrix")) and any(k in names for k in ROS_COMPANIONS):
        named = root.child("camera_name")
        where = named.where if named is not None else None
        return Recognised(CalibrationFormat.ROS_CAMERA_INFO, (Entry(root, _text(named), where),))
    if all(k in names for k in ROS_MESSAGE_KEYS):
        frame = root.child("header")
        named = frame.child("frame_id") if frame is not None else None
        where = named.where if named is not None else None
        return Recognised(
            CalibrationFormat.ROS_CAMERA_INFO_MESSAGE, (Entry(root, _text(named), where),)
        )
    return None
