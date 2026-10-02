"""What the Foxglove connector accepts as a URL, options and credentials (ADR 0007 §5, §6)."""

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from deploy_foxglove_fake import API_KEY, FakeFoxglove
from neptune.store.workspace import Workspace
from neptune_deploy.sources.foxglove import FoxgloveConfigError, foxglove_source
from neptune_deploy.sources.foxglove.config import (
    API_KEY_ENV,
    Options,
    api_key_for,
    endpoint_for,
    parse_url,
)


def online(tmp_path: Path) -> Workspace:
    workspace = Workspace(tmp_path / "home")
    workspace.allow_network(True)
    return workspace


# --- The URL -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "project"),
    [("foxglove://prj_plant_a", "prj_plant_a"), ("foxglove://-", None), ("FOXGLOVE://p1", "p1")],
)
def test_a_url_names_one_project_or_all_of_them(url: str, project: str | None) -> None:
    assert parse_url(url) == project


@pytest.mark.parametrize(
    "url",
    [
        "s3://bucket/key",
        "foxglove://",
        "foxglove:///x",
        "foxglove://prj/extra",
        "foxglove://prj?x=1",
        "foxglove://a b",
        "foxglove://a:b@c",
        "foxglove://" + "p" * 129,
        "foxglove://é",
        "https://api.foxglove.dev",
        "foxglove:/x",
        "",
    ],
)
def test_anything_else_is_refused(url: str) -> None:
    with pytest.raises(FoxgloveConfigError):
        parse_url(url)


def test_a_url_that_is_not_text_is_refused() -> None:
    with pytest.raises(FoxgloveConfigError):
        parse_url(None)  # type: ignore[arg-type]


# --- Options -----------------------------------------------------------------------------------


def test_the_defaults_are_the_public_api_read_only_and_bounded() -> None:
    options = Options.parse(None)
    assert options.endpoint == "https://api.foxglove.dev/v1" and options.store is None
    assert (options.topics, options.compression) == (True, "lz4")
    assert endpoint_for(options).authority == "api.foxglove.dev"
    assert endpoint_for(options).base_path == "/v1"
    assert options.max_recordings == 1_000_000 and options.page_size == 1000


@pytest.mark.parametrize(
    "options",
    [
        {"nope": 1},
        {"anonymous": True},  # an API key is always needed
        {"versions": True},
        {"endpoint": "https://staging.example/v1"},  # a declared endpoint needs a store
        {"store": "staging"},  # and a store needs an endpoint
        {"endpoint": 5, "store": "x"},
        {"endpoint": "https://x.example/v1", "store": "Bad Name"},
        {"endpoint": "http://api.example/v1", "store": "x"},  # plain http off loopback
        {"endpoint": "https://u:p@api.example/v1", "store": "x"},
        {"endpoint": "https://api.example/v1?x=1", "store": "x"},
        {"device_id": "a/b"},
        {"device_id": 5},
        {"device_name": "has space"},
        {"device_name": "x" * 101},
        {"start": "2026-09-30"},
        {"start": "2026-09-30T00:00:00+02:00"},  # the API reads UTC "Z" times only
        {"end": "٢٠٢٦-09-30T00:00:00Z"},  # non-ASCII digits
        {"link_hosts": "api.example"},
        {"link_hosts": ["https://api.example"]},
        {"link_hosts": [5]},
        {"identifier_properties": ["Serial", "serial"]},
        {"identifier_properties": ["has space"]},
        {"identifier_properties": "serial"},
        {"topics": "yes"},
        {"compression": "brotli"},
        {"compression": None},
        {"max_recordings": 0},
        {"max_recordings": True},
        {"max_recordings": 10**9},
        {"max_listing_bytes": 1023},
        {"page_size": 0},
        {"page_size": 2001},
        {"page_size": 1.5},
        {"timeout": 0},
        {"timeout": -1},
        {"timeout": "5"},
        {"timeout": True},
    ],
)
def test_an_option_that_is_unknown_or_malformed_is_refused(options: dict[str, Any]) -> None:
    with pytest.raises(FoxgloveConfigError):
        Options.parse(options)


def test_valid_options_are_kept_as_declared_and_sorted() -> None:
    options = Options.parse(
        {
            "endpoint": "http://127.0.0.1:9/v1",
            "store": "staging-1",
            "device_name": "ur5e-cell-1",
            "device_id": "dev_ur5e_cell1",
            "start": "2026-09-30T00:00:00Z",
            "end": "2026-09-30T23:59:59.999999999Z",
            "link_hosts": ["b.example", "a.example", "a.example"],
            "identifier_properties": ["serial", "asset_tag"],
            "topics": False,
            "compression": "",
            "page_size": 2000,
            "timeout": 5,
        }
    )
    assert options.link_hosts == ("a.example", "b.example")
    assert options.identifier_properties == ("asset_tag", "serial")
    assert (options.topics, options.compression, options.page_size) == (False, "", 2000)
    assert endpoint_for(options).host == "127.0.0.1"


# --- The API key -------------------------------------------------------------------------------


def test_the_key_is_declared_or_comes_from_a_neptune_variable_and_nothing_ambient() -> None:
    assert api_key_for({"foxglove_api_key": "fox_sk_a"}, {}) == "fox_sk_a"
    assert api_key_for(None, {API_KEY_ENV: "fox_sk_env"}) == "fox_sk_env"
    # A declared key wins; the environment is not consulted beside it.
    assert api_key_for({"foxglove_api_key": "fox_sk_a"}, {API_KEY_ENV: "fox_sk_env"}) == "fox_sk_a"
    for ambient in (
        {"FOXGLOVE_API_KEY": "fox_sk_x"},
        {"FOXGLOVE_TOKEN": "fox_sk_x"},
        {"AWS_ACCESS_KEY_ID": "x"},
        {"HOME": "/root"},
    ):
        with pytest.raises(FoxgloveConfigError, match="no Foxglove API key"):
            api_key_for(None, ambient)


@pytest.mark.parametrize(
    "key", ["", " ", "fox sk", "fox\nsk", "fox\x00", "fóx_sk", "k" * 513, "fox_sk‮"]
)
def test_a_key_that_is_not_one_printable_ascii_token_is_refused_and_not_echoed(key: str) -> None:
    for declared, environ in (({"foxglove_api_key": key}, {}), (None, {API_KEY_ENV: key})):
        try:
            api_key_for(declared, environ)
        except FoxgloveConfigError as error:
            assert key.strip() not in str(error) or not key.strip()
        else:  # an empty variable is "not set", which is also refused
            pytest.fail("accepted")


def test_unknown_declared_credentials_are_refused() -> None:
    with pytest.raises(FoxgloveConfigError, match="unknown credentials"):
        api_key_for({"foxglove_api_key": "k", "aws_access_key_id": "x"}, {})


def test_no_key_appears_in_a_repr(tmp_path: Path) -> None:
    fake = FakeFoxglove()
    with fake.serve() as endpoint:
        source = foxglove_source(
            "foxglove://-",
            network=online(tmp_path),
            options={"endpoint": endpoint, "store": "fixture"},
            credentials={"foxglove_api_key": API_KEY},
        )
    for thing in (source, source.client, source.client.transport, source.options):
        assert API_KEY not in repr(thing)
    assert API_KEY not in repr(vars(source.options))


def test_the_environment_is_read_when_the_factory_is_called(tmp_path: Path) -> None:
    fake = FakeFoxglove()
    with fake.serve() as endpoint:
        source = foxglove_source(
            "foxglove://-",
            network=online(tmp_path),
            options={"endpoint": endpoint, "store": "fixture"},
            environ={API_KEY_ENV: API_KEY},
        )
        assert len(source.index().recordings) == 5
    with fake.serve() as endpoint, pytest.raises(FoxgloveConfigError):
        foxglove_source(
            "foxglove://-",
            network=online(tmp_path),
            options={"endpoint": endpoint, "store": "fixture"},
            environ={"FOXGLOVE_API_KEY": API_KEY},  # the unprefixed name is never read
        )


# --- Importing touches nothing -------------------------------------------------------------------


def test_importing_the_package_opens_no_socket_and_reads_no_environment() -> None:
    program = (
        "import os, socket, sys\n"
        "def refuse(*a, **k):\n"
        "    raise AssertionError('an import used the network')\n"
        "socket.create_connection = refuse\n"
        "socket.socket.connect = refuse\n"
        "class Env(dict):\n"
        "    def get(self, *a):\n"
        "        raise AssertionError('an import read the environment')\n"
        "import neptune_deploy.sources.foxglove as package\n"
        "assert package.foxglove_source\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", program],
        env={**os.environ, API_KEY_ENV: "must-not-be-read-at-import"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr
