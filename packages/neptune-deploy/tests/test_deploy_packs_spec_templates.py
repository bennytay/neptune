"""Pack specs and the versioned template registry."""

import copy
import json
import shutil
from importlib import resources
from pathlib import Path
from typing import Any

import pytest

from deploy_pack_graphs import CIVIL, T0
from deploy_pack_support import ARM, FROM_T0, configuration, spec
from neptune_deploy.packs import (
    PackError,
    PackSpec,
    TemplateRegistry,
    builtin_registry,
    load_spec,
    read_spec,
    read_template,
)
from neptune_deploy.packs.snapshot import Interval, Stamp

TEMPLATES = Path(str(resources.files("neptune_deploy.packs") / "templates"))


def _spec_json() -> dict[str, Any]:
    document: dict[str, Any] = json.loads(json.dumps(spec(configuration()).to_json()))
    return document


def test_spec_round_trips_and_defaults_to_excluding_inference() -> None:
    document = _spec_json()
    assert read_spec(document) == spec(configuration())
    del document["inference"]
    assert read_spec(document).inference == "exclude"
    assert load_spec(json.dumps(document).encode()).inference == "exclude"


@pytest.mark.parametrize(
    ("path", "value", "pointer"),
    [
        (["schema"], "neptune-deploy.pack-spec/2", "/schema"),
        (["template", "version"], 0, "/template/version"),
        (["template", "version"], True, "/template/version"),
        (["template", "id"], "Configuration Lineage", "/template/id"),
        (["subject", "node_type"], "run", "/subject/node_type"),
        (["subject", "node_id"], "", "/subject/node_id"),
        (["snapshot"], "sha256:" + "0" * 64, "/snapshot"),
        (["inference"], "maybe", "/inference"),
        (["interval", "end"], {"domain_id": CIVIL, "ticks": T0}, "/interval"),
        (
            ["interval", "end"],
            {"domain_id": "rec:sha256:" + "1" * 64, "ticks": T0 + 1},
            "/interval",
        ),
        (["interval", "start", "ticks"], "0", "/interval/start/ticks"),
        (["extra"], 1, ""),
    ],
)
def test_malformed_specs_are_refused(path: list[str], value: Any, pointer: str) -> None:
    document = _spec_json()
    target: Any = document
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(PackError) as caught:
        read_spec(document)
    assert (caught.value.code, caught.value.pointer) == ("spec_malformed", pointer)


def test_a_spec_built_in_code_is_held_to_the_same_rules() -> None:
    backwards = Interval(Stamp(CIVIL, T0), Stamp(CIVIL, T0 - 1))
    with pytest.raises(PackError, match="ends at or before"):
        PackSpec("configuration-lineage", 1, ARM, backwards, configuration().id)
    with pytest.raises(PackError, match="inference"):
        PackSpec("configuration-lineage", 1, ARM, FROM_T0, configuration().id, "sometimes")


def test_shipped_templates_load_against_their_lock() -> None:
    registry = builtin_registry()
    keys = [t.key for t in registry.templates()]
    assert keys == ["configuration-lineage@1", "event-timeline@1"]
    lock = json.loads((TEMPLATES / "lock.json").read_text(encoding="utf-8"))
    assert {t.key: t.sha256 for t in registry.templates()} == lock


def _template(name: str = "configuration-lineage@1") -> dict[str, Any]:
    document: dict[str, Any] = json.loads((TEMPLATES / f"{name}.json").read_text("utf-8"))
    return document


def test_a_changed_template_version_is_refused_and_a_new_version_is_not() -> None:
    registry = builtin_registry()
    changed = _template()
    changed["sections"][0]["title"] = "Configuration in force (revised)"
    with pytest.raises(PackError) as caught:
        registry.with_template(read_template(changed))
    assert caught.value.code == "template_version_changed"
    changed["version"] = 2
    newer = registry.with_template(read_template(changed))
    assert newer.versions("configuration-lineage") == (1, 2)
    # Version 1 is untouched, and the original registry is unchanged.
    assert newer.get("configuration-lineage", 1) == registry.get("configuration-lineage", 1)
    assert registry.versions("configuration-lineage") == (1,)


def test_an_edited_shipped_template_fails_its_lock(tmp_path: Path) -> None:
    root = tmp_path / "templates"
    shutil.copytree(TEMPLATES, root)
    assert [t.key for t in TemplateRegistry.from_directory(root).templates()] == [
        "configuration-lineage@1",
        "event-timeline@1",
    ]
    edited = _template()
    edited["description"] += " Edited in place."
    (root / "configuration-lineage@1.json").write_text(json.dumps(edited), encoding="utf-8")
    with pytest.raises(PackError, match="never changes") as caught:
        TemplateRegistry.from_directory(root)
    assert caught.value.code == "template_lock_mismatch"


def test_a_template_file_without_a_lock_entry_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "templates"
    shutil.copytree(TEMPLATES, root)
    extra = _template()
    extra["version"] = 2
    (root / "configuration-lineage@2.json").write_text(json.dumps(extra), encoding="utf-8")
    with pytest.raises(PackError, match="differ"):
        TemplateRegistry.from_directory(root)
    (root / "configuration-lineage@2.json").unlink()
    (root / "lock.json").write_text("[]", encoding="utf-8")
    with pytest.raises(PackError, match="not an object"):
        TemplateRegistry.from_directory(root)


def test_a_file_named_for_another_version_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "templates"
    shutil.copytree(TEMPLATES, root)
    lock = json.loads((root / "lock.json").read_text("utf-8"))
    (root / "configuration-lineage@1.json").rename(root / "configuration-lineage@3.json")
    lock["configuration-lineage@3"] = lock.pop("configuration-lineage@1")
    (root / "lock.json").write_text(json.dumps(lock), encoding="utf-8")
    with pytest.raises(PackError, match="holds configuration-lineage@1"):
        TemplateRegistry.from_directory(root)


@pytest.mark.parametrize(
    ("path", "value", "pointer"),
    [
        (["schema"], "neptune-deploy.pack-template/0", "/schema"),
        (["sections"], [], "/sections"),
        (["subject_types"], ["machine", "machine"], "/subject_types"),
        (["subject_types"], ["robot"], "/subject_types/0"),
        (["sections", 0, "kind"], "chart", "/sections/0/kind"),
        (["sections", 0, "about"], [], "/sections/0/about"),
        (
            ["sections", 0, "about", 0],
            [{"predicate": "located_at", "direction": "up"}],
            "/sections/0/about/0/0/direction",
        ),
        (["sections", 0, "predicates"], {}, "/sections/0/predicates"),
        (
            ["sections", 0, "predicates", "has_configuration"],
            "probably",
            "/sections/0/predicates/has_configuration",
        ),
        (["sections", 0, "subject_types"], ["fleet"], "/sections/0/subject_types/0"),
        (["sections", 1, "id"], "configuration-in-force", "/sections/1"),
        (["sections", 0, "about", 1], [], "/sections/0/about/1"),
    ],
)
def test_malformed_templates_are_refused(path: list[Any], value: Any, pointer: str) -> None:
    document = copy.deepcopy(_template())
    target: Any = document
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(PackError) as caught:
        read_template(document)
    assert (caught.value.code, caught.value.pointer) == ("template_malformed", pointer)


def test_an_unknown_template_names_the_registered_ones() -> None:
    with pytest.raises(PackError, match=r"configuration-lineage@1, event-timeline@1") as caught:
        builtin_registry().get("incident-timeline", 1)
    assert caught.value.code == "template_unknown"
