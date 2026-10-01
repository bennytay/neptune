"""Page labels as a document declares them (ISO 32000-1 §12.4.2), or nothing.

The catalog's ``/PageLabels`` number tree maps the first page of each range to a label dictionary:
a numbering style ``/S`` (``D`` decimal, ``R``/``r`` roman, ``A``/``a`` letters), a prefix ``/P``
and a first number ``/St`` (1 by default). pypdf's own reading falls back to ``1, 2, 3`` when the
tree is malformed, which would turn a broken declaration into labels it never made, so the adapter
reads the tree itself and refuses one it cannot read (``LabelsUnreadable``).
"""

from typing import Final

from pypdf.generic import TextStringObject

from ._objects import array, dictionary, entry, integer, name

MAX_VISITS: Final = 100_000
MAX_DEPTH: Final = 32
MAX_WRITTEN: Final = 100_000  # past this, a roman or letter numeral is a memory bomb, not a label
_STYLES: Final = frozenset({"D", "R", "r", "A", "a"})
_ROMAN: Final = (
    (1000, "M"),
    (900, "CM"),
    (500, "D"),
    (400, "CD"),
    (100, "C"),
    (90, "XC"),
    (50, "L"),
    (40, "XL"),
    (10, "X"),
    (9, "IX"),
    (5, "V"),
    (4, "IV"),
    (1, "I"),
)


class LabelsUnreadable(Exception):
    """The page label tree is not what the specification describes."""


def _roman(value: int) -> str:
    parts: list[str] = []
    for amount, numeral in _ROMAN:
        count, value = divmod(value, amount)
        parts.append(numeral * count)
    return "".join(parts)


def numeral(style: str | None, value: int) -> str:
    """``value`` (from 1) written in a numbering style; no style writes nothing."""
    if style is None:
        return ""
    if style == "D":
        return str(value)
    if value > MAX_WRITTEN:
        raise LabelsUnreadable(f"a {style} numeral for {value} is past what the adapter writes")
    if style in ("R", "r"):
        text = _roman(value)
        return text if style == "R" else text.lower()
    letter = chr(ord("A") + (value - 1) % 26) * ((value - 1) // 26 + 1)
    return letter if style == "A" else letter.lower()


def _entries(tree: object) -> list[tuple[int, object]]:
    found: list[tuple[int, object]] = []
    stack: list[tuple[object, int]] = [(tree, 0)]
    visits = 0
    while stack:
        node_raw, depth = stack.pop()
        visits += 1
        node = dictionary(node_raw)
        if node is None or depth > MAX_DEPTH or visits > MAX_VISITS:
            raise LabelsUnreadable("the page label tree is malformed or too large")
        nums = array(entry(node, "/Nums"))
        if nums is not None:
            if len(nums) % 2:
                raise LabelsUnreadable("a /Nums array has an odd length")
            for index in range(0, len(nums), 2):
                key = integer(nums[index])
                if key is None or key < 0:
                    raise LabelsUnreadable("a page label key is not a page index")
                found.append((key, nums[index + 1]))
        kids = array(entry(node, "/Kids"))
        if nums is None and kids is None:
            raise LabelsUnreadable("a page label node has neither /Nums nor /Kids")
        stack.extend((kid, depth + 1) for kid in reversed(kids or []))
    return sorted(found, key=lambda pair: pair[0])


def page_labels(tree: object, count: int) -> list[str | None]:
    """Each page's declared label; ``None`` for a page before the first range."""
    ranges: list[tuple[int, str | None, str, int]] = []
    for key, value in _entries(tree):
        label = dictionary(value)
        if label is None:
            raise LabelsUnreadable("a page label is not a dictionary")
        style = name(entry(label, "/S"))
        if style is not None and style not in _STYLES:
            raise LabelsUnreadable(f"no numbering style {style}")
        prefix_raw = entry(label, "/P")
        if prefix_raw is not None and not isinstance(prefix_raw, TextStringObject):
            raise LabelsUnreadable("a label prefix is not a text string")
        start_raw = entry(label, "/St")
        start = 1 if start_raw is None else integer(start_raw)
        if start is None or start < 1:
            raise LabelsUnreadable("a label's first number is not a positive integer")
        if ranges and ranges[-1][0] == key:
            raise LabelsUnreadable("two label ranges start on one page")
        ranges.append((key, style, str(prefix_raw or ""), start))
    if not ranges:
        raise LabelsUnreadable("the page label tree declares no range")
    labels: list[str | None] = []
    current = -1
    for index in range(count):
        while current + 1 < len(ranges) and ranges[current + 1][0] <= index:
            current += 1
        if current < 0:
            labels.append(None)
            continue
        first, style, prefix, start = ranges[current]
        labels.append(prefix + numeral(style, start + index - first))
    return labels
