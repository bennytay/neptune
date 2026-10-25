"""The site's mapping file, ``static/neptune.mappings.yaml``, and its templates (spec §12).

The mappers hold no site's ids, paths or columns: they read them from here. A site without the
file maps only ``layout.yaml``; every other table or document is logged as unmapped.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from typing import TYPE_CHECKING, Any

import yaml

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

MAPPINGS_FILE = "neptune.mappings.yaml"
_FIELD = re.compile(r"\{([^{}]+)\}")


@dataclass(frozen=True)
class Mapping:
    """One site's declared mapping: tables, documents, extractions, feeds, types and tags."""

    raw: dict[str, Any] = field(default_factory=dict)

    def entries(self, section: str) -> list[dict[str, Any]]:
        return list(self.raw.get(section) or [])

    def match(self, section: str, path: str) -> dict[str, Any] | None:
        """The first ``section`` entry whose ``file`` glob matches ``path`` (under ``static/``)."""
        for entry in self.entries(section):
            if fnmatchcase(path, str(entry["file"])):
                return entry
        return None

    def feed(self, source: str) -> dict[str, Any] | None:
        for entry in self.entries("feeds"):
            if str(entry["source"]) == source:
                return entry
        return None

    @property
    def tags(self) -> dict[str, Any]:
        return dict(self.raw.get("tags") or {})

    def entity_type(self, text: str) -> str:
        """``|type``: the first ``types`` rule whose word ``text`` contains; else ``text``."""
        lowered = text.lower()
        for rule in self.entries("types"):
            word = rule.get("contains")
            if word is None or str(word).lower() in lowered:
                return str(rule["type"])
        return slug(lowered)


def load_mapping(site_dir: Path) -> Mapping:
    """``<site_dir>/static/neptune.mappings.yaml``, or an empty mapping when there is none."""
    path = site_dir / "static" / MAPPINGS_FILE
    if not path.exists():
        return Mapping()
    return Mapping(yaml.safe_load(path.read_text(encoding="utf-8")) or {})


def slug(text: str) -> str:
    return text.replace(" ", "-")


def tag_ref(tag: str, mapping: Mapping, register: Sequence[tuple[str, str]]) -> str:
    """``|tag``: ``CV-2`` -> ``<type>:cv-2``, typed by the register row whose name holds the tag."""
    for name, asset_type in register:
        if tag in name.split():
            return f"{mapping.entity_type(asset_type)}:{tag.lower()}"
    return f"{mapping.tags.get('default', 'machine')}:{tag.lower()}"


def render(
    template: Any,
    lookup: Callable[[str], str],
    mapping: Mapping,
    register: Sequence[tuple[str, str]] = (),
) -> str:
    """Fill each ``{name|filter|...}`` in ``template`` from ``lookup``, filters left to right."""

    def one(match: re.Match[str]) -> str:
        name, *steps = match.group(1).split("|")
        text = lookup(name.strip())
        for step in steps:
            kind, _, arg = step.strip().partition(":")
            if kind == "lower":
                text = text.lower()
            elif kind == "slug":
                text = slug(text)
            elif kind == "until":
                text = text.split(arg, 1)[0]
            elif kind == "strip":
                text = text.strip(arg or None)
            elif kind == "type":
                text = mapping.entity_type(text)
            elif kind == "tag":
                text = tag_ref(text.strip(), mapping, register)
            else:
                raise ValueError(f"unknown template filter {kind!r} in {template!r}")
        return text

    return _FIELD.sub(one, str(template))
