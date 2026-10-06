"""Rendering a published contract version's JSON Schema as a MyST page.

Every definition (nested schema resources included) becomes a labelled section listing its type,
its properties with their types and whether each is required, and every other keyword verbatim, so
the page loses nothing the file says; the file itself is attached for download. ``$ref``s become
cross-references. Output depends only on the schema and its ``version.json``: same input, same page.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

Schema = Mapping[str, Any]

# Keywords the summary line or the property list already shows; anything else is listed verbatim.
_SHOWN = frozenset(
    {
        "$defs",
        "$id",
        "$ref",
        "$schema",
        "$comment",
        "title",
        "description",
        "type",
        "properties",
        "required",
        "items",
        "anyOf",
        "oneOf",
        "allOf",
        "const",
        "enum",
    }
)
_STRUCTURAL = frozenset({"items", "anyOf", "oneOf", "allOf"})
_SUMMARISED = frozenset({"$ref", "type", "const", "enum", "description", "title", *_STRUCTURAL})
_MARKDOWN_SPECIAL = re.compile(r"([\\`*_\[\]<>|{}#$])")


class SchemaError(ValueError):
    """The schema is not one this renderer can label unambiguously."""


def escape(text: str) -> str:
    """``text`` with Markdown's special characters escaped, one line per paragraph kept."""
    return _MARKDOWN_SPECIAL.sub(r"\\\1", text)


def code(text: str) -> str:
    """``text`` as an inline code span, whatever backticks it contains."""
    runs = [len(run) for run in re.findall(r"`+", text)]
    fence = "`" * (max(runs, default=0) + 1)
    pad = " " if text.startswith("`") or text.endswith("`") or not text.strip() else ""
    return f"{fence}{pad}{text}{pad}{fence}"


def _summarised(node: object) -> bool:
    """Whether ``Page.summary`` shows everything ``node`` says (descriptions aside)."""
    if not isinstance(node, dict) or not set(node) <= _SUMMARISED:
        return False
    for key in _STRUCTURAL & set(node):
        subs = node[key] if isinstance(node[key], list) else [node[key]]
        if not all(_summarised(sub) for sub in subs):
            return False
    return True


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(", ", ": "))


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


@dataclass(frozen=True)
class Definition:
    """One ``$defs`` entry: its path of names from the root and the resource its refs resolve in."""

    path: tuple[str, ...]
    schema: Schema
    resource: tuple[str, ...]


def definitions(root: Schema) -> list[Definition]:
    """Every definition in document order, depth first; a definition with ``$id`` is a resource."""
    out: list[Definition] = []

    def visit(node: Schema, path: tuple[str, ...], resource: tuple[str, ...]) -> None:
        for name, child in (node.get("$defs") or {}).items():
            if not isinstance(child, dict):
                raise SchemaError(f"$defs entry {'/'.join((*path, name))} is not an object")
            child_path = (*path, name)
            child_resource = child_path if "$id" in child else resource
            out.append(Definition(child_path, child, child_resource))
            visit(child, child_path, child_resource)

    visit(root, (), ())
    return out


class Page:
    """The page of one schema version; ``label`` prefixes every Sphinx label it defines."""

    def __init__(self, root: Schema, label: str) -> None:
        self.root = root
        self.label = label
        self.defs = definitions(root)
        self._by_path = {d.path: d for d in self.defs}
        self._resources: dict[str, tuple[str, ...]] = {}
        if isinstance(root.get("$id"), str):
            self._resources[root["$id"]] = ()
        for d in self.defs:
            if isinstance(d.schema.get("$id"), str):
                self._resources[d.schema["$id"]] = d.path
        labels = [self.label_of(d.path) for d in self.defs]
        if len(set(labels)) != len(labels):
            raise SchemaError(f"two definitions share a label under {label}")

    def label_of(self, path: Sequence[str]) -> str:
        return "--".join((self.label, *(_slug(name) or "-" for name in path)))

    def _target(self, ref: str, resource: tuple[str, ...]) -> tuple[str, ...] | None:
        base, _, pointer = ref.partition("#")
        if base:
            if base not in self._resources:
                return None
            resource = self._resources[base]
        parts = [p.replace("~1", "/").replace("~0", "~") for p in pointer.split("/")[1:]]
        if len(parts) % 2 or any(key != "$defs" for key in parts[::2]):
            return None
        path = (*resource, *parts[1::2])
        return path if path in self._by_path else None

    def ref(self, ref: str, resource: tuple[str, ...]) -> str:
        target = self._target(ref, resource)
        if target is None:
            return code(ref)
        return f"{{ref}}`{escape(' / '.join(target))} <{self.label_of(target)}>`"

    def summary(self, node: Schema, resource: tuple[str, ...]) -> str:
        """A one-line description of the values ``node`` accepts."""
        parts: list[str] = []
        if "$ref" in node:
            parts.append(self.ref(str(node["$ref"]), resource))
        if "const" in node:
            parts.append(f"constant {code(_json(node['const']))}")
        if "enum" in node:
            parts.append("one of " + ", ".join(code(_json(v)) for v in node["enum"]))
        for keyword, joiner in (
            ("anyOf", " or "),
            ("oneOf", " or exactly one of "),
            ("allOf", " and "),
        ):
            if keyword in node:
                inner = [self.summary(sub, resource) for sub in node[keyword]]
                parts.append("(" + joiner.join(inner) + ")" if len(inner) > 1 else "".join(inner))
        types = node.get("type")
        if types is not None:
            names = [types] if isinstance(types, str) else list(types)
            words: list[str] = []
            for name in names:
                if name == "array" and isinstance(node.get("items"), dict):
                    words.append(f"array of {self.summary(node['items'], resource)}")
                else:
                    words.append(str(name))
            parts.append(" or ".join(words))
        return "; ".join(parts) if parts else "any value"

    def keywords(self, node: Schema, indent: str) -> Iterator[str]:
        """The keywords the summary does not show, verbatim; a structural keyword too when one of
        its subschemas says more than the summary can."""
        for key in sorted(node):
            shown = key in _SHOWN
            if key in _STRUCTURAL:
                subs = node[key] if isinstance(node[key], list) else [node[key]]
                shown = all(_summarised(sub) for sub in subs)
            if not shown:
                yield f"{indent}- {code(key)}: {code(_json(node[key]))}"

    def _properties(self, node: Schema, resource: tuple[str, ...], indent: str) -> Iterator[str]:
        required = set(node.get("required") or ())
        properties = node.get("properties") or {}
        for name, prop in properties.items():
            flag = "required" if name in required else "optional"
            line = f"{indent}- {code(name)} ({flag}): {self.summary(prop, resource)}"
            if isinstance(prop, dict) and isinstance(prop.get("description"), str):
                line += f". {escape(prop['description'])}"
            yield line
            if isinstance(prop, dict):
                yield from self.keywords(prop, indent + "  ")
                if "properties" in prop:
                    yield from self._properties(prop, resource, indent + "  ")
        for name in sorted(required - set(properties)):
            yield f"{indent}- {code(name)} (required; not described by `properties`)"

    def section(self, definition: Definition) -> list[str]:
        node = definition.schema
        resource = definition.resource
        lines = [
            f"({self.label_of(definition.path)})=",
            f"### {escape(' / '.join(definition.path))}",
            "",
        ]
        if isinstance(node.get("description"), str):
            lines += [escape(node["description"]), ""]
        if isinstance(node.get("title"), str):
            lines += [f"Title: {escape(node['title'])}", ""]
        if "$id" in node:
            lines += [f"Schema resource {code(str(node['$id']))}.", ""]
        lines.append(f"Accepts: {self.summary(node, resource)}.")
        lines.append("")
        body = [*self._properties(node, resource, ""), *self.keywords(node, "")]
        if node.get("properties"):
            body.insert(0, "Properties:")
            body.insert(1, "")
        return [*lines, *body, ""] if body else lines


def render(
    contract: str,
    version: str,
    schema: Schema,
    meta: Mapping[str, Any],
    *,
    download: str,
    owner: str,
) -> str:
    """The page for ``contract`` at ``version``; ``download`` is the schema file beside the page."""
    page = Page(schema, f"schema-{_slug(contract)}-{_slug(version)}")
    title = schema.get("title")
    out = [
        f"# {contract} {version}",
        "",
        f"{escape(title)}." if isinstance(title, str) else "",
        "",
    ]
    if isinstance(schema.get("description"), str):
        out += [escape(schema["description"]), ""]
    facts = [
        ("Owner", code(owner)),
        ("Status", code(str(meta.get("status", "unknown")))),
        ("Owner version", code(str(meta.get("owner_version", "unknown")))),
        ("Schema `$id`", code(str(schema.get("$id", "none")))),
        ("Dialect", code(str(schema.get("$schema", "none")))),
        ("SHA-256", code(str(meta.get("schema_sha256", "unknown")))),
        ("Golden documents", str(len(meta.get("goldens") or {}))),
    ]
    out += [f"- {name}: {value}" for name, value in facts]
    out += ["", f"The schema file: {{download}}`schema.json <{download}>`.", ""]
    out += ["## Root", "", f"A document is valid when it matches: {page.summary(schema, ())}.", ""]
    root_keywords = list(page.keywords(schema, ""))
    if root_keywords:
        out += [*root_keywords, ""]
    out += ["## Definitions", ""]
    if not page.defs:
        out += ["This version defines no `$defs`.", ""]
    for definition in page.defs:
        out += page.section(definition)
    return "\n".join(out).rstrip("\n") + "\n"


def version_key(name: str) -> tuple[int, ...]:
    """Sort key for a ``v<major>.<minor>.<patch>`` directory name."""
    return tuple(int(part) for part in name.removeprefix("v").split("."))
