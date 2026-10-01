"""The manifest's schema, version 1: typed declarations read strictly from a tree (ADR 0047 §1).

A manifest declares what the evidence in a folder does not say, or says ambiguously, and never
restates what it does say: no source content is copied into it.

- ``neptune: 1``: the schema version, required. A version this reader does not know is refused.
- ``machines``, ``sites``, ``tasks``, ``software``: entities by a declared ``id``, each with
  optional ``name`` and ``description`` and ``aliases`` (other identifiers the evidence uses for
  it, by namespace: the only way two names become one entity, since Neptune never merges
  identities itself). A machine may name its ``embodiment``; software its ``version``.
- ``runs``: sessions by ``name``, each holding root-relative ``paths`` (a file, or a directory
  meaning everything below it) and naming the ``machine``, ``site``, ``task`` and ``software`` it
  involved. Each is a declared session for grouping (ADR 0036 §6).
- ``sources``: rules by ``path`` (a file or a directory) or ``glob`` (the ignore-rule syntax,
  anchored at the root), choosing the ``adapter`` for what they match and its ``options``.
- ``adapters``: options for every source an adapter reads, by adapter id.
- ``grouping``: ``gap_seconds`` for session grouping.

Unknown keys, wrong types, dangling references and repeated ids are ``ManifestError``s naming the
JSON pointer (and line) of the value: a manifest is used whole or not at all. ``json_schema()`` is
the same schema for editors (``docs/schema/manifest.schema.json``).
"""

import os
import re
import sys
from collections.abc import Callable, Mapping
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from pathlib import Path
from typing import Final, TypeVar

from neptune.discovery.ignore import IgnoreError, IgnoreRule, parse_rule
from neptune.manifest.reader import ManifestError, Map, Node, Scalar, Seq, read_tree
from neptune.model.jsonvalue import JsonObject, JsonValue
from neptune.model.source import LocalPath, RawLocalPath

SCHEMA_VERSION: Final = 1
SCHEMA_ID: Final = "https://neptune.dev/schema/manifest/v1.json"

_ID: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:\-]{0,127}")
_OPTION: Final = re.compile(r"[a-z][a-z0-9_]{0,63}")
_MAX_TEXT: Final = 1000
EMBODIMENTS: Final = (
    "manipulator",
    "mobile_base",
    "mobile_manipulator",
    "legged",
    "humanoid",
    "aerial",
    "marine",
    "ground_vehicle",
    "fleet",
    "other",
)

T = TypeVar("T")


def _pointer(parent: str, key: str | int) -> str:
    token = str(key).replace("~", "~0").replace("/", "~1")
    return f"{parent}/{token}"


def _fail(pointer: str, node: Node | None, problem: str) -> ManifestError:
    line = f"line {node.line} " if node is not None and node.line is not None else ""
    return ManifestError(f"{line}({pointer or '/'}): {problem}")


def _map(
    node: Node, pointer: str, keys: AbstractSet[str], required: AbstractSet[str] = frozenset()
) -> dict[str, Node]:
    if not isinstance(node, Map):
        raise _fail(pointer, node, "expected a mapping")
    items = dict(node.items)
    if unknown := sorted(set(items) - keys):
        raise _fail(pointer, node, f"unknown keys {unknown}; known: {sorted(keys)}")
    if missing := sorted(required - set(items)):
        raise _fail(pointer, node, f"missing keys {missing}")
    return items


def _seq(node: Node, pointer: str) -> tuple[Node, ...]:
    """A list; an empty value (``key:`` with nothing after it) is an empty list."""
    if isinstance(node, Scalar) and node.value is None:
        return ()
    if not isinstance(node, Seq):
        raise _fail(pointer, node, "expected a list")
    return node.items


def _text(node: Node, pointer: str) -> str:
    """Text: a quoted or JSON string, or a plain YAML scalar's text as written."""
    if isinstance(node, Scalar):
        if isinstance(node.value, str):
            value = node.value
        elif node.text and node.value is not None:
            value = node.text
        else:
            raise _fail(pointer, node, "expected text")
        if not value.strip():
            raise _fail(pointer, node, "expected non-empty text")
        if len(value) > _MAX_TEXT:
            raise _fail(pointer, node, f"longer than {_MAX_TEXT} characters")
        return value
    raise _fail(pointer, node, "expected text")


def _id(node: Node, pointer: str) -> str:
    value = _text(node, pointer)
    if not _ID.fullmatch(value):
        raise _fail(
            pointer,
            node,
            f"{value!r} is not an id: letters, digits and . _ : - (at most 128), "
            "starting with a letter or digit",
        )
    return value


def _int(node: Node, pointer: str, low: int, high: int) -> int:
    if (
        not isinstance(node, Scalar)
        or isinstance(node.value, bool)
        or not isinstance(node.value, int)
    ):
        raise _fail(pointer, node, "expected a whole number")
    if not low <= node.value <= high:
        raise _fail(pointer, node, f"expected a whole number from {low} to {high}")
    return node.value


def _json(node: Node, pointer: str) -> JsonValue:
    """An option value as JSON: text, a number, a boolean, or lists and mappings of them."""
    if isinstance(node, Map):
        return {key: _json(value, _pointer(pointer, key)) for key, value in node.items}
    if isinstance(node, Seq):
        return [_json(item, _pointer(pointer, n)) for n, item in enumerate(node.items)]
    if node.value is None:
        raise _fail(pointer, node, "an option needs a value (null is not one)")
    if isinstance(node.value, float) and node.value != node.value:  # pragma: no cover
        raise _fail(pointer, node, "NaN is not a value")
    return node.value


def _path(node: Node, pointer: str) -> str:
    value = _text(node, pointer)
    if value.startswith("/"):
        raise _fail(pointer, node, f"{value!r} is absolute; paths are relative to the folder")
    value = value.removesuffix("/")  # a directory may be written with its slash
    try:
        LocalPath(value)
    except (ValueError, TypeError) as exc:
        raise _fail(pointer, node, f"{value!r} is not a root-relative path: {exc}") from None
    return value


def _glob(node: Node, pointer: str) -> IgnoreRule:
    value = _text(node, pointer)
    if value.startswith("/"):
        raise _fail(pointer, node, f"{value!r} is absolute; globs are relative to the folder")
    if value.startswith("!"):
        raise _fail(pointer, node, "negation (!) is not supported: a glob only ever selects")
    try:
        rule = parse_rule(("/" + value).encode("utf-8"), "manifest")
    except IgnoreError as exc:
        raise _fail(pointer, node, f"{value!r} is not a glob: {exc}") from None
    if rule is None or rule.dir_only:
        raise _fail(pointer, node, f"{value!r} must match files; end it with /** for a directory")
    return rule


def _aliases(node: Node | None, pointer: str) -> tuple[tuple[str, str], ...]:
    """``{namespace: value}`` or ``{namespace: [values]}``, as sorted ``(namespace, value)``."""
    if node is None:
        return ()
    if not isinstance(node, Map):
        raise _fail(pointer, node, "expected a mapping of namespace to identifier(s)")
    pairs: set[tuple[str, str]] = set()
    for namespace, value in node.items:
        here = _pointer(pointer, namespace)
        if not _ID.fullmatch(namespace):
            raise _fail(here, node, f"{namespace!r} is not a namespace id")
        values = (
            [_text(v, _pointer(here, n)) for n, v in enumerate(value.items)]
            if isinstance(value, Seq)
            else [_text(value, here)]
        )
        if not values:
            raise _fail(here, value, "expected at least one identifier")
        pairs.update((namespace, v) for v in values)
    return tuple(sorted(pairs))


def _optional(
    items: Mapping[str, Node], key: str, pointer: str, read: Callable[[Node, str], T]
) -> T | None:
    return read(items[key], _pointer(pointer, key)) if key in items else None


# --- Declarations ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Entity:
    """A machine, site, task or software item: its declared id and what is said of it.

    ``extra`` holds the kind's own field (a machine's ``embodiment``, software's ``version``).
    ``pointer`` is where it is declared: every value the job takes from it cites that place.
    """

    section: str
    id: str
    name: str | None
    description: str | None
    aliases: tuple[tuple[str, str], ...]
    extra: tuple[tuple[str, str], ...]
    pointer: str

    def to_json(self) -> JsonObject:
        out: dict[str, JsonValue] = {"id": self.id}
        if self.name is not None:
            out["name"] = self.name
        if self.description is not None:
            out["description"] = self.description
        if self.aliases:  # as written: a namespace to its identifiers, sorted
            grouped: dict[str, list[JsonValue]] = {}
            for namespace, value in self.aliases:
                grouped.setdefault(namespace, []).append(value)
            out["aliases"] = dict(grouped)
        out.update(dict(self.extra))
        return out


_ENTITY_KEYS: Final[Mapping[str, tuple[str, ...]]] = {
    "machines": ("embodiment",),
    "sites": (),
    "tasks": (),
    "software": ("version",),
}


@dataclass(frozen=True)
class RunDecl:
    """A session the user declares: its name, what it holds, and what it involved."""

    name: str
    paths: tuple[str, ...]
    machine: str | None
    site: str | None
    task: str | None
    software: tuple[str, ...]
    description: str | None
    pointer: str

    def to_json(self) -> JsonObject:
        out: dict[str, JsonValue] = {"name": self.name, "paths": list(self.paths)}
        for key in ("machine", "site", "task", "description"):
            if (value := getattr(self, key)) is not None:
                out[key] = value
        if self.software:
            out["software"] = list(self.software)
        return out


@dataclass(frozen=True)
class SourceRule:
    """Which adapter reads what a ``path`` or ``glob`` matches, with which ``options``."""

    path: str | None
    glob: IgnoreRule | None
    adapter: str
    options: JsonObject
    pointer: str

    def matches(self, location: LocalPath | RawLocalPath) -> bool:
        raw = location.raw
        if self.glob is not None:
            return self.glob.matches(tuple(raw.split(b"/")), is_dir=False)
        assert self.path is not None
        own = self.path.encode("utf-8")
        return raw == own or raw.startswith(own + b"/")

    @property
    def pattern(self) -> str:
        return self.path if self.path is not None else self.glob_text

    @property
    def glob_text(self) -> str:
        assert self.glob is not None
        return os.fsdecode(self.glob.pattern)[1:]

    def to_json(self) -> JsonObject:
        out: dict[str, JsonValue] = {"adapter": self.adapter}
        if self.path is not None:
            out["path"] = self.path
        else:
            out["glob"] = self.glob_text
        if self.options:
            out["options"] = self.options
        return out


@dataclass(frozen=True)
class Manifest:
    """A manifest, read and checked: every declaration in the order written."""

    entities: tuple[Entity, ...]
    runs: tuple[RunDecl, ...]
    sources: tuple[SourceRule, ...]
    adapters: tuple[tuple[str, JsonObject], ...]
    gap_seconds: int | None
    version: int = SCHEMA_VERSION

    def section(self, name: str) -> tuple[Entity, ...]:
        return tuple(e for e in self.entities if e.section == name)

    def adapter_options(self) -> dict[str, JsonObject]:
        return dict(self.adapters)

    def to_json(self) -> JsonObject:
        """The declarations as canonical JSON: what the manifest transform's config records.
        It is itself a valid manifest, which reads back to the same declarations."""
        out: dict[str, JsonValue] = {"neptune": self.version}
        for section in _ENTITY_KEYS:
            if entries := self.section(section):
                out[section] = [entity.to_json() for entity in entries]
        if self.runs:
            out["runs"] = [run.to_json() for run in self.runs]
        if self.sources:
            out["sources"] = [rule.to_json() for rule in self.sources]
        if self.adapters:
            out["adapters"] = {key: {"options": value} for key, value in self.adapters}
        if self.gap_seconds is not None:
            out["grouping"] = {"gap_seconds": self.gap_seconds}
        return out


_TOP: Final = {"neptune", *_ENTITY_KEYS, "runs", "sources", "adapters", "grouping"}


def parse_manifest(data: bytes, *, json_syntax: bool = False) -> Manifest:
    """The manifest ``data`` holds; ``ManifestError`` if it is not a valid version-1 manifest."""
    tree = read_tree(data, json_syntax=json_syntax)
    if isinstance(tree, Map) and not tree.items:
        raise _fail("", tree, "an empty manifest: write at least 'neptune: 1'")
    top = _map(tree, "", _TOP, {"neptune"})
    version = top["neptune"]
    if (
        not isinstance(version, Scalar)
        or version.value != SCHEMA_VERSION
        or isinstance(version.value, bool)
    ):
        raise _fail("/neptune", version, f"this Neptune reads manifest version {SCHEMA_VERSION}")
    entities: list[Entity] = []
    for section, extra_keys in _ENTITY_KEYS.items():
        entities += _entities(top.get(section), f"/{section}", section, extra_keys)
    known = {(e.section, e.id) for e in entities}
    runs = _runs(top.get("runs"), known)
    sources = _sources(top.get("sources"))
    adapters = _adapters(top.get("adapters"))
    gap: int | None = None
    if "grouping" in top:
        grouping = _map(top["grouping"], "/grouping", {"gap_seconds"})
        gap = _optional(grouping, "gap_seconds", "/grouping", lambda n, p: _int(n, p, 0, 86_400))
    return Manifest(tuple(entities), runs, sources, adapters, gap)


def _entities(
    node: Node | None, pointer: str, section: str, extra_keys: tuple[str, ...]
) -> list[Entity]:
    if node is None:
        return []
    out: list[Entity] = []
    seen: set[str] = set()
    keys = {"id", "name", "description", "aliases", *extra_keys}
    for n, item in enumerate(_seq(node, pointer)):
        here = _pointer(pointer, n)
        fields = _map(item, here, keys, {"id"})
        ident = _id(fields["id"], _pointer(here, "id"))
        if ident in seen:
            raise _fail(
                _pointer(here, "id"), fields["id"], f"{section} {ident!r} is declared twice"
            )
        seen.add(ident)
        extra = tuple(
            (key, _text(fields[key], _pointer(here, key))) for key in extra_keys if key in fields
        )
        out.append(
            Entity(
                section,
                ident,
                _optional(fields, "name", here, _text),
                _optional(fields, "description", here, _text),
                _aliases(fields.get("aliases"), _pointer(here, "aliases")),
                extra,
                here,
            )
        )
    return out


def _runs(node: Node | None, known: set[tuple[str, str]]) -> tuple[RunDecl, ...]:
    if node is None:
        return ()
    out: list[RunDecl] = []
    names: set[str] = set()
    keys = {"name", "paths", "machine", "site", "task", "software", "description"}
    for n, item in enumerate(_seq(node, "/runs")):
        here = _pointer("/runs", n)
        fields = _map(item, here, keys, {"name", "paths"})
        name = _text(fields["name"], _pointer(here, "name"))
        if name in names:
            raise _fail(_pointer(here, "name"), fields["name"], f"run {name!r} is declared twice")
        names.add(name)
        where = _pointer(here, "paths")
        paths = [_path(p, _pointer(where, k)) for k, p in enumerate(_seq(fields["paths"], where))]
        if not paths:
            raise _fail(where, fields["paths"], "a run holds at least one path")
        if len(set(paths)) != len(paths):
            raise _fail(where, fields["paths"], "a path is listed twice")

        def ref(key: str, section: str, value: Node, at: str) -> str:
            ident = _id(value, at)
            if (section, ident) not in known:
                raise _fail(at, value, f"no {section} entry has id {ident!r}")
            return ident

        refs = {
            key: ref(key, section, fields[key], _pointer(here, key))
            for key, section in (("machine", "machines"), ("site", "sites"), ("task", "tasks"))
            if key in fields
        }
        software: tuple[str, ...] = ()
        if "software" in fields:
            at = _pointer(here, "software")
            listed = _seq(fields["software"], at)
            software = tuple(
                ref("software", "software", v, _pointer(at, k)) for k, v in enumerate(listed)
            )
        out.append(
            RunDecl(
                name,
                tuple(sorted(paths)),
                refs.get("machine"),
                refs.get("site"),
                refs.get("task"),
                software,
                _optional(fields, "description", here, _text),
                here,
            )
        )
    return tuple(out)


def _options(node: Node | None, pointer: str) -> JsonObject:
    if node is None:
        return {}
    if not isinstance(node, Map):
        raise _fail(pointer, node, "expected a mapping of option names to values")
    out: dict[str, JsonValue] = {}
    for key, value in node.items:
        if not _OPTION.fullmatch(key):
            raise _fail(_pointer(pointer, key), value, f"{key!r} is not an option name")
        out[key] = _json(value, _pointer(pointer, key))
    return out


def _sources(node: Node | None) -> tuple[SourceRule, ...]:
    if node is None:
        return ()
    out: list[SourceRule] = []
    for n, item in enumerate(_seq(node, "/sources")):
        here = _pointer("/sources", n)
        fields = _map(item, here, {"path", "glob", "adapter", "options"}, {"adapter"})
        if ("path" in fields) == ("glob" in fields):
            raise _fail(here, item, "a source rule has exactly one of 'path' and 'glob'")
        path = _optional(fields, "path", here, _path)
        glob = _optional(fields, "glob", here, _glob)
        adapter = _id(fields["adapter"], _pointer(here, "adapter"))
        options = _options(fields.get("options"), _pointer(here, "options"))
        out.append(SourceRule(path, glob, adapter, options, here))
    return tuple(out)


def _adapters(node: Node | None) -> tuple[tuple[str, JsonObject], ...]:
    if node is None or (isinstance(node, Scalar) and node.value is None):
        return ()
    if not isinstance(node, Map):
        raise _fail("/adapters", node, "expected a mapping of adapter ids")
    out: list[tuple[str, JsonObject]] = []
    for adapter, value in node.items:
        here = _pointer("/adapters", adapter)
        if not _ID.fullmatch(adapter):
            raise _fail(here, value, f"{adapter!r} is not an adapter id")
        fields = _map(value, here, {"options"}, {"options"})
        out.append((adapter, _options(fields["options"], _pointer(here, "options"))))
    return tuple(sorted(out))


# --- The editor's schema -----------------------------------------------------------------------


def json_schema() -> JsonObject:
    """The JSON Schema (draft 2020-12) of a version-1 manifest, for editors and validators.

    It states the shapes; ``parse_manifest`` also checks what a schema cannot (references
    between entries, repeated ids, glob syntax) and is the authority.
    """
    text: JsonObject = {"type": "string", "minLength": 1, "maxLength": _MAX_TEXT}
    ident: JsonObject = {"type": "string", "pattern": f"^{_ID.pattern}$"}
    path: JsonObject = {
        "type": "string",
        "minLength": 1,
        "pattern": "^(?!/)(?!.*(^|/)\\.\\.?(/|$)).+$",
        "description": "Root-relative, '/'-separated: a file, or a directory and all below it.",
    }
    aliases: JsonObject = {
        "type": "object",
        "description": "Other identifiers the evidence uses for this entity, by namespace.",
        "propertyNames": {"pattern": f"^{_ID.pattern}$"},
        "additionalProperties": {"oneOf": [text, {"type": "array", "items": text, "minItems": 1}]},
    }

    def entity(description: str, **extra: JsonValue) -> JsonObject:
        properties: dict[str, JsonValue] = {
            "id": ident,
            "name": text,
            "description": text,
            "aliases": {"$ref": "#/$defs/aliases"},
            **extra,
        }
        return {
            "type": "object",
            "description": description,
            "required": ["id"],
            "additionalProperties": False,
            "properties": properties,
        }

    options: JsonObject = {
        "type": "object",
        "description": "An adapter's options by name (see the adapter's descriptor).",
        "propertyNames": {"pattern": f"^{_OPTION.pattern}$"},
    }

    def nullable_list(item: JsonValue) -> JsonObject:
        return {"oneOf": [{"type": "array", "items": item}, {"type": "null"}]}

    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": SCHEMA_ID,
        "title": "Neptune manifest",
        "description": (
            "An optional neptune.yaml (or .json) at the root of a folder: declarations the "
            "evidence does not make. Everything it declares is stated, with the manifest as its "
            "source (docs/manifest.md)."
        ),
        "type": "object",
        "required": ["neptune"],
        "additionalProperties": False,
        "properties": {
            "neptune": {"const": SCHEMA_VERSION, "description": "The manifest schema version."},
            "machines": nullable_list(
                entity(
                    "A robot or vehicle.",
                    embodiment={**text, "examples": list(EMBODIMENTS)},
                )
            ),
            "sites": nullable_list(entity("A place where runs happened.")),
            "tasks": nullable_list(entity("What a run was for.")),
            "software": nullable_list(entity("Software a run used.", version=text)),
            "runs": nullable_list(
                {
                    "type": "object",
                    "description": "A declared session (ADR 0036 §6).",
                    "required": ["name", "paths"],
                    "additionalProperties": False,
                    "properties": {
                        "name": text,
                        "paths": {"type": "array", "items": path, "minItems": 1},
                        "machine": ident,
                        "site": ident,
                        "task": ident,
                        "software": {"type": "array", "items": ident},
                        "description": text,
                    },
                }
            ),
            "sources": nullable_list(
                {
                    "type": "object",
                    "description": "Which adapter reads the files a path or glob matches.",
                    "required": ["adapter"],
                    "oneOf": [{"required": ["path"]}, {"required": ["glob"]}],
                    "additionalProperties": False,
                    "properties": {
                        "path": path,
                        "glob": {
                            "type": "string",
                            "minLength": 1,
                            "description": "Ignore-rule glob syntax, anchored at the root.",
                        },
                        "adapter": ident,
                        "options": options,
                    },
                }
            ),
            "adapters": {
                "oneOf": [
                    {
                        "type": "object",
                        "propertyNames": {"pattern": f"^{_ID.pattern}$"},
                        "additionalProperties": {
                            "type": "object",
                            "required": ["options"],
                            "additionalProperties": False,
                            "properties": {"options": options},
                        },
                    },
                    {"type": "null"},
                ]
            },
            "grouping": {
                "type": "object",
                "additionalProperties": False,
                "properties": {"gap_seconds": {"type": "integer", "minimum": 0, "maximum": 86_400}},
            },
        },
        "$defs": {"aliases": aliases},
    }


def main(argv: list[str]) -> int:
    """``python -m neptune.manifest.schema <file>``: write the editor's schema."""
    import json

    if len(argv) != 1:
        sys.stderr.write("usage: python -m neptune.manifest.schema <output.json>\n")
        return 2
    text = json.dumps(json_schema(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    Path(argv[0]).write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
