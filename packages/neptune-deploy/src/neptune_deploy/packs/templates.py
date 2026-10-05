"""Pack templates and their versioned registry (ADR 0013 §4).

A template is JSON (``neptune-deploy.pack-template/1``) naming which claims a pack shows and how
they are grouped. It holds no code: a new report is a new template file. ::

    {
      "schema": "neptune-deploy.pack-template/1",
      "id": "configuration-lineage", "version": 1,
      "title": "Configuration lineage", "description": "…",
      "subject_types": ["deployment", "machine", "site"],
      "sections": [
        {"id": "in-force", "title": "Configuration in force", "description": "…",
         "kind": "states",                      # claims | states | timeline
         "subject_types": ["machine"],          # optional; default: the template's
         "same_event": ["same_as"],             # optional, timeline sections only
         "about": [[], [{"predicate": "located_at", "direction": "in"}]],
         "predicates": {"has_configuration": "known",
                        "configuration_candidate": "ambiguous",
                        "configuration_unknown": "unknown"}}
      ]
    }

``about`` lists paths from the pack subject; each hop follows a claim of ``predicate`` outward (the
subject's claim, to its object node) or inward (a claim whose object is the node, to its subject).
``shared`` goes from a node to every other node stating the same object under the predicate (an
incident to the timeline entries evidenced by the same record). ``[]`` is the subject itself.
``same_event`` (timeline sections) names predicates whose claims make two event nodes one event
(two records of one incident), so their times are compared as one event's. ``predicates``
selects the claims about the nodes reached and says which missingness state each one expresses:
Memory states ``Ambiguous`` and ``Unknown`` as predicates (``*_candidate``, ``*_unknown``), never
as blank objects.

The shipped templates live in ``packs/templates/<id>@<version>.json``, and ``lock.json`` pins the
sha256 of each one's canonical JSON. Loading refuses a file whose hash is not its lock entry, so an
existing version never changes: a change is a new version, and a pack names the template hash it
was compiled with.
"""

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from importlib import resources
from importlib.resources.abc import Traversable
from typing import Final

from neptune.identity import canonical_json
from neptune.identity.hashing import content_id
from neptune.model.jsonvalue import JsonObject, JsonValue
from neptune_deploy.packs._read import TOKEN, Reader, child, parse_document
from neptune_deploy.packs.errors import PackError
from neptune_deploy.packs.spec import SUBJECT_TYPES

TEMPLATE_SCHEMA: Final = "neptune-deploy.pack-template/1"
SECTION_KINDS: Final = ("claims", "states", "timeline")
KNOWLEDGE_ROLES: Final = ("ambiguous", "known", "unknown")
DIRECTIONS: Final = ("in", "out", "shared")
MAX_TEMPLATE_BYTES: Final = 1024 * 1024
LOCK_FILE: Final = "lock.json"

_R: Final = Reader("template_malformed")


@dataclass(frozen=True)
class Hop:
    predicate: str
    direction: str  # "out": subject -> object; "in": object -> subject; "shared": same object

    def to_json(self) -> JsonObject:
        return {"direction": self.direction, "predicate": self.predicate}


@dataclass(frozen=True)
class SectionTemplate:
    id: str
    title: str
    description: str
    kind: str
    subject_types: tuple[str, ...]
    about: tuple[tuple[Hop, ...], ...]
    predicates: Mapping[str, str]  # predicate -> knowledge role
    same_event: tuple[str, ...] = ()  # timeline sections: predicates joining nodes into one event

    def to_json(self) -> JsonObject:
        out: JsonObject = {
            "about": [[hop.to_json() for hop in path] for path in self.about],
            "description": self.description,
            "id": self.id,
            "kind": self.kind,
            "predicates": dict(self.predicates),
            "subject_types": list(self.subject_types),
            "title": self.title,
        }
        if self.same_event:
            out = {**out, "same_event": list(self.same_event)}
        return out


@dataclass(frozen=True)
class Template:
    id: str
    version: int
    title: str
    description: str
    subject_types: tuple[str, ...]
    sections: tuple[SectionTemplate, ...]
    sha256: str  # of the template document's canonical JSON

    @property
    def key(self) -> str:
        return f"{self.id}@{self.version}"


def read_template(document: JsonValue) -> Template:
    """A template from its parsed document; refuses anything outside the format."""
    doc = _R.obj(
        document,
        "",
        ("description", "id", "schema", "sections", "subject_types", "title", "version"),
    )
    if doc["schema"] != TEMPLATE_SCHEMA:
        raise _R.fail(f"schema is not {TEMPLATE_SCHEMA}", "/schema")
    subject_types = _subject_types(doc["subject_types"], "/subject_types", SUBJECT_TYPES)
    sections: list[SectionTemplate] = []
    for i, item in enumerate(_R.array(doc["sections"], "/sections")):
        section = _section(item, child("/sections", i), subject_types)
        if any(other.id == section.id for other in sections):
            raise _R.fail(f"section id {section.id!r} is repeated", child("/sections", i))
        sections.append(section)
    if not sections:
        raise _R.fail("a template has at least one section", "/sections")
    try:
        digest = content_id(canonical_json.dumps(document))
    except canonical_json.CanonicalJsonError as exc:
        raise _R.fail(f"not canonical JSON: {exc}", "") from exc
    return Template(
        id=_R.string(doc["id"], "/id", TOKEN),
        version=_R.integer(doc["version"], "/version", 1),
        title=_R.text(doc["title"], "/title"),
        description=_R.text(doc["description"], "/description"),
        subject_types=subject_types,
        sections=tuple(sections),
        sha256=digest,
    )


def load_template(data: bytes) -> Template:
    return read_template(parse_document(data, "template_malformed", MAX_TEMPLATE_BYTES))


def _subject_types(value: JsonValue, pointer: str, allowed: Iterable[str]) -> tuple[str, ...]:
    allowed = tuple(allowed)
    types = [
        _R.choice(item, child(pointer, i), allowed)
        for i, item in enumerate(_R.array(value, pointer))
    ]
    if not types or len(set(types)) != len(types):
        raise _R.fail("subject types are a non-empty list without repeats", pointer)
    return tuple(sorted(types))


def _section(value: JsonValue, pointer: str, template_types: tuple[str, ...]) -> SectionTemplate:
    section = _R.obj(
        value,
        pointer,
        ("about", "description", "id", "kind", "predicates", "title"),
        ("same_event", "subject_types"),
    )
    subject_types = template_types
    if "subject_types" in section:
        subject_types = _subject_types(
            section["subject_types"], child(pointer, "subject_types"), template_types
        )
    paths: list[tuple[Hop, ...]] = []
    at = child(pointer, "about")
    for i, path in enumerate(_R.array(section["about"], at)):
        hops: list[Hop] = []
        for j, hop in enumerate(_R.array(path, child(at, i))):
            here = child(child(at, i), j)
            step = _R.obj(hop, here, ("direction", "predicate"))
            hops.append(
                Hop(
                    _R.string(step["predicate"], child(here, "predicate"), TOKEN),
                    _R.choice(step["direction"], child(here, "direction"), DIRECTIONS),
                )
            )
        if tuple(hops) in paths:
            raise _R.fail("a path is repeated", child(at, i))
        paths.append(tuple(hops))
    if not paths:
        raise _R.fail("a section is about at least one path", at)
    at = child(pointer, "predicates")
    predicates = section["predicates"]
    if not isinstance(predicates, Mapping) or not predicates:
        raise _R.fail("predicates is a non-empty object", at)
    roles = {
        _R.string(name, child(at, name), TOKEN): _R.choice(role, child(at, name), KNOWLEDGE_ROLES)
        for name, role in predicates.items()
    }
    kind = _R.choice(section["kind"], child(pointer, "kind"), SECTION_KINDS)
    same_event: tuple[str, ...] = ()
    if "same_event" in section:
        at = child(pointer, "same_event")
        if kind != "timeline":
            raise _R.fail("same_event is for timeline sections", at)
        names = [
            _R.string(item, child(at, i), TOKEN)
            for i, item in enumerate(_R.array(section["same_event"], at))
        ]
        if not names or len(set(names)) != len(names):
            raise _R.fail("same_event is a non-empty list without repeats", at)
        same_event = tuple(sorted(names))
    return SectionTemplate(
        id=_R.string(section["id"], child(pointer, "id"), TOKEN),
        title=_R.text(section["title"], child(pointer, "title")),
        description=_R.text(section["description"], child(pointer, "description")),
        kind=kind,
        subject_types=subject_types,
        about=tuple(paths),
        predicates={name: roles[name] for name in sorted(roles)},
        same_event=same_event,
    )


class TemplateRegistry:
    """Templates by ``(id, version)``. A registered version never changes."""

    def __init__(self, templates: Iterable[Template] = ()) -> None:
        self._templates: dict[tuple[str, int], Template] = {}
        for template in templates:
            self._add(template)

    def _add(self, template: Template) -> None:
        held = self._templates.get((template.id, template.version))
        if held is not None and held.sha256 != template.sha256:
            raise PackError(
                "template_version_changed",
                f"{template.key} is already registered with {held.sha256}; a changed template"
                " is a new version",
            )
        self._templates[(template.id, template.version)] = template

    def with_template(self, template: Template) -> "TemplateRegistry":
        """A registry that also holds ``template``; refuses a changed existing version."""
        registry = TemplateRegistry(self._templates.values())
        registry._add(template)
        return registry

    def get(self, template_id: str, version: int) -> Template:
        template = self._templates.get((template_id, version))
        if template is None:
            known = ", ".join(sorted(t.key for t in self._templates.values())) or "none"
            raise PackError(
                "template_unknown", f"no template {template_id}@{version} (registered: {known})"
            )
        return template

    def versions(self, template_id: str) -> tuple[int, ...]:
        return tuple(sorted(v for (i, v) in self._templates if i == template_id))

    def templates(self) -> tuple[Template, ...]:
        return tuple(self._templates[key] for key in sorted(self._templates))

    @classmethod
    def builtin(cls) -> "TemplateRegistry":
        """The shipped templates, each checked against ``lock.json``."""
        return cls.from_directory(resources.files("neptune_deploy.packs") / "templates")

    @classmethod
    def from_directory(cls, root: Traversable) -> "TemplateRegistry":
        """Every ``<id>@<version>.json`` under ``root``, each checked against ``root/lock.json``:
        a file whose hash is not its lock entry, or a file and an entry without each other, is
        refused."""
        try:
            lock_value = json.loads((root / LOCK_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise PackError("template_lock_mismatch", f"lock.json is unreadable: {exc}") from exc
        if not isinstance(lock_value, dict) or not all(
            isinstance(v, str) for v in lock_value.values()
        ):
            raise PackError("template_lock_mismatch", "lock.json is not an object of hashes")
        lock: dict[str, str] = dict(lock_value)
        files = sorted(
            entry.name
            for entry in root.iterdir()
            if entry.name.endswith(".json") and entry.name != LOCK_FILE
        )
        if sorted(name.removesuffix(".json") for name in files) != sorted(lock):
            raise PackError(
                "template_lock_mismatch",
                f"template files {files} and lock entries {sorted(lock)} differ",
            )
        registry = cls()
        for name in files:
            template = load_template((root / name).read_bytes())
            if template.key != name.removesuffix(".json"):
                raise PackError("template_lock_mismatch", f"{name} holds {template.key}")
            if lock[template.key] != template.sha256:
                raise PackError(
                    "template_lock_mismatch",
                    f"{name} hashes to {template.sha256}, not its locked {lock[template.key]}:"
                    " an existing template version never changes; add a new version",
                )
            registry._add(template)
        return registry
