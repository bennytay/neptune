"""Hand-eye calibration results, as the tools that compute them write them (ADR 0073 §1, §2).

Three shapes, each told by the keys its tool writes and never by a name:

- ROS ``easy_handeye`` YAML. Releases from 0.3 write ``parameters`` (``eye_on_hand``,
  ``robot_base_frame``, ``robot_effector_frame``, ``tracking_base_frame``,
  ``tracking_marker_frame`` and the MoveIt group) and ``transformation`` (``x``, ``y``, ``z``,
  ``qx``, ``qy``, ``qz``, ``qw``); earlier ones write the same keys flat, with only the robot frame
  the mode uses.
- ROS 2 ``easy_handeye2``'s ``.calib``: the ``HandeyeCalibration`` message as YAML, ``parameters``
  (``calibration_type`` ``eye_in_hand`` or ``eye_on_base`` and the same frames) and ``transform``
  (``translation`` ``x``, ``y``, ``z``; ``rotation`` ``x``, ``y``, ``z``, ``w``).
- MoveIt Calibration's "Save camera pose": a ``<launch>`` holding one ``tf2_ros``
  ``static_transform_publisher`` node named ``camera_link_broadcaster`` whose ``args`` are
  ``x y z qx qy qz qw frame_id child_frame_id``.

Each tool publishes its result as a tf transform whose parent is the robot frame its mode names
(the effector for eye-in-hand, the base for eye-on-base, MoveIt's ``frame_id``) and whose child is
the camera's frame; a tf transform is the child's pose in the parent. Numbers are kept as written,
in the file's order; a quaternion is never normalised.
"""

import math
from dataclasses import dataclass
from typing import Final

from neptune.adapters.calibration._items import Item, Kind, xml_scalar
from neptune.adapters.calibration._params import single_number
from neptune.model.configuration import ScalarType
from neptune.model.frames import MAX_TEXT_LENGTH, QuaternionOrder
from neptune.model.scalars import NonFinite

FRAME_KEYS: Final = ("robot_effector_frame", "robot_base_frame")
TRACKING: Final = "tracking_base_frame"
EASY_TRANSFORM: Final = "transformation"
EASY2_TRANSFORM: Final = "transform"
EYE_ON_HAND: Final = "eye_on_hand"
CALIBRATION_TYPE: Final = "calibration_type"
EYE_IN_HAND, EYE_ON_BASE = "eye_in_hand", "eye_on_base"
MOVEIT_NODE: Final = (
    ("pkg", "tf2_ros"),
    ("type", "static_transform_publisher"),
    ("name", "camera_link_broadcaster"),
)
# A quaternion is a rotation within this much of norm one; MoveIt prints six significant digits.
UNIT_TOLERANCE: Final = 1e-4
_ORDERS: Final[dict[tuple[str, ...], QuaternionOrder]] = {
    ("qx", "qy", "qz", "qw"): QuaternionOrder.XYZW,
    ("qw", "qx", "qy", "qz"): QuaternionOrder.WXYZ,
    ("x", "y", "z", "w"): QuaternionOrder.XYZW,
    ("w", "x", "y", "z"): QuaternionOrder.WXYZ,
}


@dataclass(frozen=True)
class HandEye:
    """Where a hand-eye result declares its mode, frames and transform.

    ``holder`` holds the mode and frame keys (``parameters``, or the root of the flat form);
    ``transform`` is the transform's mapping, or MoveIt's node, whose ``args`` hold everything.
    ``key`` is the root key the transform is written under (``""`` for MoveIt's node).
    """

    holder: Item
    transform: Item
    key: str
    launch: bool = False

    @property
    def tracking(self) -> Item | None:
        return None if self.launch else self.holder.child(TRACKING)


@dataclass(frozen=True)
class Declared:
    """A hand-eye transform as declared: frames, translation, rotation and its order."""

    parent: str
    child: str
    translation: tuple[float, float, float]
    rotation: tuple[float, float, float, float]
    order: QuaternionOrder

    @property
    def norm(self) -> float:
        return math.sqrt(math.fsum(value * value for value in self.rotation))

    @property
    def unit(self) -> bool:
        return abs(self.norm - 1.0) <= UNIT_TOLERANCE


@dataclass(frozen=True)
class Unread:
    """Why a hand-eye transform is not emitted: ``frame`` (the parent or child is not named) or
    ``values`` (the numbers are not seven finite ones in a named order)."""

    reason: str
    message: str


# --- Recognising -------------------------------------------------------------------------------


def _mapping(item: Item | None) -> Item | None:
    return item if item is not None and item.kind is Kind.MAPPING else None


def easy_handeye(root: Item) -> HandEye | None:
    """ROS easy_handeye: ``parameters`` (from 0.3) or flat keys, and ``transformation``."""
    transform = _mapping(root.child(EASY_TRANSFORM))
    if transform is None:
        return None
    nested = _mapping(root.child("parameters"))
    for holder in (nested, root):
        if holder is None:
            continue
        if (
            holder.child(EYE_ON_HAND) is not None
            and holder.child(TRACKING) is not None
            and any(holder.child(key) is not None for key in FRAME_KEYS)
        ):
            return HandEye(holder, transform, EASY_TRANSFORM)
    return None


def easy_handeye2(root: Item) -> HandEye | None:
    """ROS 2 easy_handeye2: ``parameters`` with ``calibration_type``, and ``transform``."""
    holder = _mapping(root.child("parameters"))
    transform = _mapping(root.child(EASY2_TRANSFORM))
    if holder is None or transform is None:
        return None
    if holder.child(CALIBRATION_TYPE) is None or holder.child(TRACKING) is None:
        return None
    if not any(holder.child(key) is not None for key in FRAME_KEYS):
        return None
    return HandEye(holder, transform, EASY2_TRANSFORM)


def moveit_launch(root: Item) -> HandEye | None:
    """MoveIt Calibration's saved camera pose: a launch of its one static transform node."""
    if root.name != "launch" or len(root.children) != 1:
        return None
    (node,) = root.children
    if node.name != "node" or node.children or node.attribute("args") is None:
        return None
    if any(node.attribute(key) != value for key, value in MOVEIT_NODE):
        return None
    return HandEye(root, node, "", launch=True)


# --- Reading -----------------------------------------------------------------------------------


def _text(item: Item | None) -> str | None:
    if item is None or item.kind is not Kind.SCALAR or len(item.readings) != 1:
        return None
    reading = item.readings[0]
    if reading.type is not ScalarType.STRING or not isinstance(reading.value, str):
        return None
    return reading.value or None


def _flag(item: Item | None) -> bool | None:
    """A boolean every reading agrees on (``yes`` is text in YAML 1.2), else ``None``."""
    if item is None or item.kind is not Kind.SCALAR or len(item.readings) != 1:
        return None
    reading = item.readings[0]
    value = reading.value
    return value if reading.type is ScalarType.BOOL and isinstance(value, bool) else None


def read(hand_eye: HandEye) -> Declared | Unread:
    """The transform a hand-eye result declares, or why it is not one."""
    declared = _read_launch(hand_eye.transform) if hand_eye.launch else _read_yaml(hand_eye)
    if isinstance(declared, Declared):
        for name in (declared.parent, declared.child):
            if len(name) > MAX_TEXT_LENGTH:
                return Unread("frame", f"a frame name is longer than {MAX_TEXT_LENGTH} characters")
    return declared


def _read_yaml(hand_eye: HandEye) -> Declared | Unread:
    parent = _parent(hand_eye)
    if isinstance(parent, Unread):
        return parent
    child = _text(hand_eye.tracking)
    if child is None:
        return Unread("frame", f"{TRACKING} is not a frame name, so the camera's frame is unnamed")
    if child == parent:
        return Unread("frame", f"the robot frame and {TRACKING} are one frame, {child!r}")
    numbers = _numbers(hand_eye)
    if isinstance(numbers, Unread):
        return numbers
    translation, rotation, order = numbers
    return Declared(parent, child, translation, rotation, order)


def _parent(hand_eye: HandEye) -> str | Unread:
    holder = hand_eye.holder
    if hand_eye.key == EASY_TRANSFORM:
        flag = _flag(holder.child(EYE_ON_HAND))
        if flag is None:
            return Unread("frame", f"{EYE_ON_HAND} is not true or false, so the mode is unknown")
        in_hand = flag
    else:
        mode = _text(holder.child(CALIBRATION_TYPE))
        if mode not in (EYE_IN_HAND, EYE_ON_BASE):
            return Unread(
                "frame",
                f"{CALIBRATION_TYPE} is not {EYE_IN_HAND} or {EYE_ON_BASE}, so the mode is unknown",
            )
        in_hand = mode == EYE_IN_HAND
    key = FRAME_KEYS[0] if in_hand else FRAME_KEYS[1]
    parent = _text(holder.child(key))
    if parent is None:
        mode_name = "eye-in-hand" if in_hand else "eye-on-base"
        return Unread("frame", f"a {mode_name} result needs {key}, which is not a frame name")
    return parent


def _numbers(
    hand_eye: HandEye,
) -> tuple[tuple[float, float, float], tuple[float, float, float, float], QuaternionOrder] | Unread:
    mapping = hand_eye.transform
    if hand_eye.key == EASY_TRANSFORM:
        moved, turned = mapping, mapping
        axes, components = ("x", "y", "z"), ("qx", "qy", "qz", "qw")
    else:
        moved_item = _mapping(mapping.child("translation"))
        turned_item = _mapping(mapping.child("rotation"))
        if moved_item is None or turned_item is None:
            return Unread("values", "transform does not hold a translation and a rotation mapping")
        moved, turned = moved_item, turned_item
        axes, components = ("x", "y", "z"), ("x", "y", "z", "w")
    translation = _finite(moved, axes)
    rotation = _finite(turned, components)
    if isinstance(translation, Unread):
        return translation
    if isinstance(rotation, Unread):
        return rotation
    written = tuple(c.name for c in turned.children if c.name in components)
    order = _ORDERS.get(written)
    if order is None:
        return Unread(
            "values",
            f"the quaternion's components are written {', '.join(written)}: neither x, y, z, w"
            " nor w, x, y, z, and they are never reordered",
        )
    by_name = dict(zip(components, rotation, strict=True))
    in_order = tuple(by_name[name] for name in written)
    x, y, z = translation
    q0, q1, q2, q3 = in_order
    return (x, y, z), (q0, q1, q2, q3), order


def _finite(mapping: Item, names: tuple[str, ...]) -> tuple[float, ...] | Unread:
    values: list[float] = []
    for name in names:
        items = [c for c in mapping.children if c.name == name]
        if len(items) != 1:
            state = "is missing" if not items else "is written more than once"
            return Unread("values", f"{mapping.name}/{name} {state}")
        number = single_number(items[0])
        if isinstance(number, NonFinite):
            return Unread("values", f"{mapping.name}/{name} is not finite ({number})")
        if not isinstance(number, float):
            return Unread("values", f"{mapping.name}/{name} is not a number")
        values.append(number)
    return tuple(values)


def _read_launch(node: Item) -> Declared | Unread:
    args = (node.attribute("args") or "").split()
    if len(args) != 9:
        return Unread(
            "values",
            f"args holds {len(args)} values, not static_transform_publisher's nine"
            " (x y z qx qy qz qw frame_id child_frame_id)",
        )
    numbers: list[float] = []
    for token in args[:7]:
        scalar = xml_scalar(token)
        value = scalar.value
        if scalar.type not in (ScalarType.INT, ScalarType.FLOAT) or isinstance(value, bool):
            return Unread("values", f"args value {token!r} is not a number")
        if isinstance(value, NonFinite) or not isinstance(value, int | float):
            return Unread("values", f"args value {token!r} is not finite")
        numbers.append(float(value))
    parent, child = args[7], args[8]
    if parent == child:
        return Unread("frame", f"frame_id and child_frame_id are one frame, {parent!r}")
    x, y, z, qx, qy, qz, qw = numbers
    return Declared(parent, child, (x, y, z), (qx, qy, qz, qw), QuaternionOrder.XYZW)
