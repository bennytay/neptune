"""How the SDK sends a URI to a connector (ADR 0067): which one, when, and what it refuses.

Connectors are real plugin distributions (``make_plugin_dists``); the working one is the
in-process fake object store. Nothing is listed or fetched here: these are the checks made before
any job runs.
"""

from pathlib import Path
from types import ModuleType
from typing import Final

import pytest

from neptune.sdk import (
    ConfigurationError,
    IgnorePolicy,
    InvalidSourceError,
    JobOptions,
    Neptune,
    NetworkRefusedError,
    NothingToResumeError,
    RemoteSource,
)
from neptune.store.workspace import Workspace

pytestmark = pytest.mark.usefixtures("forget_plugins")

FAKE: Final = (
    Path(__file__).parents[2] / "fixtures" / "sources" / "fake_object_store.py"
).read_text(encoding="utf-8")
OTHERS: Final = """
def raising(url, *, network, options=None):
    raise ValueError("the bucket name is not one")

raising.schemes = ("raise",)

def nothing(url, *, network, options=None):
    return object()

nothing.schemes = ("nothing",)

def rival(url, *, network, options=None):
    raise AssertionError("never called")

rival.schemes = ("fake",)

def exits(url, *, network, options=None):
    raise SystemExit(3)

exits.schemes = ("exits",)
"""


@pytest.fixture
def connectors(plugin_dists: ModuleType, plugin_site: Path) -> Path:
    plugin_dists.install(
        plugin_site,
        "neptune-test-fake-store",
        "1.0.0",
        sources={"fake_store": ":make"},
        module=FAKE,
    )
    plugin_dists.install(
        plugin_site,
        "neptune-test-others",
        "1.0.0",
        sources={"raising": ":raising", "nothing": ":nothing", "exits": ":exits"},
        module=OTHERS,
    )
    return plugin_site


@pytest.fixture
def client(connectors: Path, tmp_path: Path) -> Neptune:
    workspace = Workspace(tmp_path / "ws")
    workspace.allow_network(True)
    return Neptune(workspace)


@pytest.mark.parametrize(
    "uri",
    [
        "fake://user:secret@bucket/p/",  # credentials
        "fake://bucket@host/p/",
        "fake://bucket/p/?sig=abc",  # a query (an Azure SAS lives there)
        "fake://bucket/p/#part",
    ],
)
def test_a_uri_holding_credentials_a_query_or_a_fragment_is_refused(
    client: Neptune, uri: str
) -> None:
    with pytest.raises(InvalidSourceError):
        client.dry_run(RemoteSource(uri, options={"store": "/nowhere"}))


def test_a_local_only_workspace_refuses_before_the_connector_is_built(
    connectors: Path, tmp_path: Path
) -> None:
    import neptune_test_fake_store as fake  # type: ignore[import-not-found]

    with pytest.raises(NetworkRefusedError):
        Neptune(tmp_path / "local").dry_run(RemoteSource("fake://b/p/", options={"store": "/x"}))
    assert fake.BUILT == []


def test_the_connector_is_the_one_declaring_the_scheme_or_the_one_named(
    client: Neptune, tmp_path: Path
) -> None:
    store = tmp_path / "store"
    store.mkdir()
    remote = RemoteSource("fake://b/p/", connector="fake_store", options={"store": str(store)})
    assert client.dry_run(remote).planned
    with pytest.raises(ConfigurationError, match="no connector 'missing' is installed"):
        client.dry_run(RemoteSource("fake://b/p/", connector="missing"))
    with pytest.raises(ConfigurationError, match="reads fake://, not other://"):
        client.dry_run(RemoteSource("other://b/p/", connector="fake_store"))
    with pytest.raises(ConfigurationError, match="no installed connector reads none://"):
        client.dry_run("none://b/p/")


def test_two_connectors_declaring_one_scheme_must_be_chosen_between(
    plugin_dists: ModuleType, connectors: Path, tmp_path: Path
) -> None:
    plugin_dists.install(
        connectors, "neptune-test-rival", "1.0.0", sources={"rival": ":rival"}, module=OTHERS
    )
    workspace = Workspace(tmp_path / "ws")
    workspace.allow_network(True)
    with pytest.raises(ConfigurationError, match="read by fake_store, rival"):
        Neptune(workspace).dry_run("fake://b/p/")


@pytest.mark.parametrize(
    ("uri", "message"),
    [
        ("raise://b/p/", "ValueError: the bucket name is not one"),
        ("nothing://b/p/", "built no ExternalSource"),
        ("exits://b/p/", "SystemExit"),  # a plugin never ends the client
    ],
)
def test_a_connector_that_cannot_build_the_source_is_a_configuration_error(
    client: Neptune, uri: str, message: str
) -> None:
    with pytest.raises(ConfigurationError, match=message):
        client.dry_run(uri)


@pytest.mark.parametrize("options", [[1], {"n": float("nan")}, {1: "key"}])
def test_options_are_one_json_object(client: Neptune, options: object) -> None:
    with pytest.raises(ConfigurationError):
        client.dry_run(RemoteSource("fake://b/p/", options=options))  # type: ignore[arg-type]


def test_a_manifest_or_ignore_patterns_name_local_paths(client: Neptune, tmp_path: Path) -> None:
    remote = RemoteSource("fake://b/p/", options={"store": str(tmp_path)})
    with pytest.raises(ConfigurationError, match="manifest"):
        client.dry_run(remote, manifest="neptune.yaml")
    with pytest.raises(NothingToResumeError):
        client.dry_run(remote, resume=True)
    ignoring = Neptune(
        client.workspace, options=JobOptions(ignore=IgnorePolicy(patterns=("*.tmp",)))
    )
    with pytest.raises(ConfigurationError, match="ignore patterns"):
        ignoring.dry_run(remote)
