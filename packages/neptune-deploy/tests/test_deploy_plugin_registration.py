"""Deploy reaches the compiler only through its entry points, and the schema it pins (ADR 0001)."""

import re
from importlib.metadata import distribution
from pathlib import Path

import neptune_deploy
from neptune.adapters.contract import ABI_VERSION, Adapter
from neptune.adapters.registry import AdapterRegistry
from neptune.model.kinds import RECORD_KINDS
from neptune.model.lifecycle import LIFECYCLE_KINDS
from neptune.model.record import SCHEMA_VERSION
from neptune_deploy.adapters.lifecycle import LifecycleAdapter

PACKAGE = Path(__file__).resolve().parents[1]
ADAPTERS_GROUP = "neptune.adapters"
SOURCES_GROUP = "neptune.sources"
LIFECYCLE = frozenset(kind.kind for kind in LIFECYCLE_KINDS)


def _entry_points(group: str) -> dict[str, str]:
    eps = distribution("neptune-deploy").entry_points
    return {ep.name: ep.value for ep in eps if ep.group == group}


def registered_adapters() -> list[Adapter]:
    eps = distribution("neptune-deploy").entry_points
    return [ep.load()() for ep in sorted(eps, key=lambda ep: ep.name) if ep.group == ADAPTERS_GROUP]


def test_deploy_registers_its_adapters_and_nothing_else() -> None:
    groups = {ep.group for ep in distribution("neptune-deploy").entry_points}
    assert groups <= {ADAPTERS_GROUP, SOURCES_GROUP}
    assert _entry_points(ADAPTERS_GROUP) == {
        "deploy_lifecycle": "neptune_deploy.adapters.lifecycle:LifecycleAdapter"
    }
    assert _entry_points(SOURCES_GROUP) == {  # read-only connectors (ADR 0006, 0007)
        "deploy_azure_blob": "neptune_deploy.sources.object_store:azure_source",
        "deploy_foxglove": "neptune_deploy.sources.foxglove:foxglove_source",  # ADR 0007
        "deploy_gcs": "neptune_deploy.sources.object_store:gcs_source",
        "deploy_s3": "neptune_deploy.sources.object_store:s3_source",
    }


def test_each_entry_point_is_named_by_its_adapter_id_and_registers() -> None:
    adapters = registered_adapters()
    assert [adapter.descriptor.id for adapter in adapters] == sorted(_entry_points(ADAPTERS_GROUP))
    registry = AdapterRegistry(adapters)  # refuses another ABI, a duplicate id, a missing method
    assert all(adapter.descriptor.abi == ABI_VERSION for adapter in registry.adapters())
    assert isinstance(adapters[0], LifecycleAdapter)


def test_adapter_ids_are_prefixed_so_they_cannot_take_a_compiler_id() -> None:
    assert all(name.startswith("deploy_") for name in _entry_points(ADAPTERS_GROUP))


def test_adapters_emit_only_the_compilers_lifecycle_kinds() -> None:
    for adapter in registered_adapters():
        kinds = set(adapter.descriptor.record_kinds)
        assert kinds <= LIFECYCLE, "Deploy adds no record kind; lifecycle kinds are the model's"
        assert kinds <= RECORD_KINDS.keys()
    assert set(LifecycleAdapter().descriptor.record_kinds) == LIFECYCLE


def test_the_pinned_schema_is_the_compilers_and_holds_the_lifecycle_kinds() -> None:
    assert neptune_deploy.PACKAGE_SCHEMA_VERSION == SCHEMA_VERSION
    assert all(kind.since <= neptune_deploy.PACKAGE_SCHEMA_VERSION for kind in LIFECYCLE_KINDS)
    assert len(LIFECYCLE) == 8


def test_contracts_declare_the_same_pin() -> None:
    contracts = (PACKAGE / "docs" / "contracts.md").read_text(encoding="utf-8")
    match = re.search(
        r"\| Package schema \(canonical records\) \|.*?\| \*\*(\d+)\*\* \|", contracts
    )
    assert match is not None
    assert int(match.group(1)) == neptune_deploy.PACKAGE_SCHEMA_VERSION
    lock = (PACKAGE.parents[1] / "contracts" / "lock.toml").read_text(encoding="utf-8")
    section = lock.split("[neptune-deploy]", 1)[1].split("\n[", 1)[0]
    assert f'package-schema = "{neptune_deploy.PACKAGE_SCHEMA_VERSION}.0.0"' in section
