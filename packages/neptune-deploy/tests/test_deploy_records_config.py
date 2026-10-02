"""What a record source is given: URLs, closed options, credentials, identity (ADR 0008)."""

import importlib
import socket
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from deploy_records_fake import FakeServer, JiraBackend
from deploy_records_support import (
    CONFLUENCE_CREDENTIALS,
    DRIVE_CREDENTIALS,
    JIRA_CREDENTIALS,
    LINEAR_CREDENTIALS,
    ONEDRIVE_CREDENTIALS,
    REST_CREDENTIALS,
    SERVICENOW_CREDENTIALS,
    cmms_profile,
    online,
)
from neptune.model.ids import ExternalObjectRef
from neptune_deploy.sources.records import (
    CONNECTOR_IDS,
    RecordConfigError,
    confluence_source,
    gdrive_source,
    jira_source,
    linear_source,
    onedrive_source,
    record_source,
    rest_source,
    servicenow_source,
)

if TYPE_CHECKING:
    from collections.abc import Callable

SECRET = "tok-s3cr3t-never-printed"


def chain(exc: BaseException) -> str:
    """Every message and repr in an exception's cause and context chain."""
    texts: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        texts.append(f"{current!s} {current!r}")
        current = current.__cause__ or current.__context__
    return " ".join(texts)


def make(kind: str, tmp_path: Path, url: str | None = None, **kwargs: Any) -> Any:
    factories = {
        "jira": (jira_source, "jira://acme.atlassian.net/OPS", JIRA_CREDENTIALS),
        "servicenow": (
            servicenow_source,
            "servicenow://acme.service-now.com/change_request",
            SERVICENOW_CREDENTIALS,
        ),
        "gdrive": (gdrive_source, "gdrive://my-drive", DRIVE_CREDENTIALS),
        "confluence": (
            confluence_source,
            "confluence://acme.atlassian.net/5001",
            CONFLUENCE_CREDENTIALS,
        ),
        "rest": (rest_source, "rest://cmms.example.com", REST_CREDENTIALS),
        "onedrive": (onedrive_source, "onedrive://b!siteA_lib01", ONEDRIVE_CREDENTIALS),
        "linear": (linear_source, "linear://acme-robotics/OPS", LINEAR_CREDENTIALS),
    }
    factory, default_url, credentials = factories[kind]
    options = kwargs.pop("options", None)
    if kind == "rest":
        options = {"profile": cmms_profile(), **(options or {})}
    kwargs.setdefault("credentials", credentials)
    return factory(url or default_url, network=online(tmp_path), options=options, **kwargs)


@pytest.mark.parametrize(
    "kind", ["jira", "servicenow", "gdrive", "confluence", "rest", "onedrive", "linear"]
)
def test_every_system_builds_without_touching_the_network(tmp_path: Path, kind: str) -> None:
    source = make(kind, tmp_path)
    assert source.connector_id in CONNECTOR_IDS
    assert source.findings() == ()  # nothing was listed, so nothing was requested


def test_the_factory_names_are_the_connector_ids() -> None:
    assert CONNECTOR_IDS == (
        "deploy_confluence",
        "deploy_gdrive",
        "deploy_jira",
        "deploy_linear",
        "deploy_onedrive",
        "deploy_rest",
        "deploy_servicenow",
    )


def test_importing_the_package_touches_no_network_file_or_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("an import opened a socket")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    import neptune_deploy.sources.records as records

    importlib.reload(records)


@pytest.mark.parametrize(
    "url",
    [
        "jira://user:pw@acme.atlassian.net/OPS",
        "jira://acme.atlassian.net/OPS?token=abc",
        "jira://acme.atlassian.net/OPS#frag",
        "jira://acme.atlassian.net/OPS\\x",
        "jira://acme.atlassian.net/ops lower",
        "jira://acme.atlassian.net",
        "jira:///OPS",
        "https://acme.atlassian.net/OPS",
        "servicenow://acme.atlassian.net/OPS",
        "jira://acme.atlassian.net/../OPS",
        "jira://acme.atlassian.net/OPS/extra",
        "jira://[::1/OPS",
        "jira://acme.atlassian.net:99999/OPS",
        "jira://acme.atlassian.net:²/OPS",
        "jira://acme.atlassian.net/\u202eOPS",
    ],
)
def test_urls_that_are_not_exactly_a_system_and_a_scope_are_refused(
    tmp_path: Path, url: str
) -> None:
    with pytest.raises(RecordConfigError) as raised:
        make("jira", tmp_path, url)
    assert "pw" not in chain(raised.value) and "abc" not in chain(raised.value)


def test_user_information_is_refused_without_echoing_it(tmp_path: Path) -> None:
    with pytest.raises(RecordConfigError, match="declare credentials instead") as raised:
        make("jira", tmp_path, "jira://admin:hunter2@acme.atlassian.net/OPS")
    assert "hunter2" not in chain(raised.value) and "admin" not in chain(raised.value)


@pytest.mark.parametrize(
    "url",
    [
        "servicenow://acme.service-now.com/Change-Request",
        "servicenow://acme.service-now.com/",
        "servicenow://acme.service-now.com/1table",
        "confluence://acme.atlassian.net/ABC",
        "gdrive://my drive",
        "gdrive://my-drive/sub",
        "rest://cmms.example.com/api",
    ],
)
def test_scopes_that_are_not_what_each_system_names_are_refused(tmp_path: Path, url: str) -> None:
    kind = url.split(":")[0]
    with pytest.raises(RecordConfigError):
        make(kind, tmp_path, url)


@pytest.mark.parametrize(
    "options",
    [
        {"unknown": 1},
        {"page_size": 0},
        {"page_size": 100_000},
        {"page_size": True},
        {"max_records": "10"},
        {"max_records": 0},
        {"timeout": -1},
        {"timeout": True},
        {"scheme": "ftp"},
        {"instance": "Has Space"},
        {"instance": "has/slash"},
        {"since": 5},
        {"max_listing_bytes": 10},
        {"fields": []},
        {"fields": ["has space"]},
        {"api_version": "4"},
        {"attachments": "yes"},
    ],
)
def test_options_are_closed_and_checked(tmp_path: Path, options: dict[str, Any]) -> None:
    with pytest.raises(RecordConfigError):
        make("jira", tmp_path, options=options)


def test_a_cursor_must_be_the_connectors_own_and_well_formed(tmp_path: Path) -> None:
    for since in (
        "deploy_servicenow/1:2026-01-01 00:00:00",
        "deploy_jira/2:2026-08-03T11:40:00.000+0200",
        "deploy_jira/1:not a time",
        "2026-08-03T11:40:00.000+0200",
        "deploy_jira/1:",
    ):
        with pytest.raises(RecordConfigError):
            make("jira", tmp_path, options={"since": since})
    make("jira", tmp_path, options={"since": "deploy_jira/1:2026-08-03T11:40:00.000+0200"})
    with pytest.raises(RecordConfigError):  # Unicode digits are not digits here
        make("servicenow", tmp_path, options={"since": "deploy_servicenow/1:٢٠٢٦-01-01 00:00:00"})


def test_http_is_for_a_loopback_host_only(tmp_path: Path) -> None:
    with pytest.raises(RecordConfigError):
        make("jira", tmp_path, options={"scheme": "http"})
    make("jira", tmp_path, "jira://127.0.0.1:8080/OPS", options={"scheme": "http", "instance": "a"})


def test_a_loopback_host_or_declared_endpoint_needs_a_declared_instance(tmp_path: Path) -> None:
    with pytest.raises(RecordConfigError, match="declared instance"):
        make("jira", tmp_path, "jira://127.0.0.1:8080/OPS", options={"scheme": "http"})
    with pytest.raises(RecordConfigError, match="declared instance"):
        make("gdrive", tmp_path, options={"endpoint": "http://127.0.0.1:9"})
    with pytest.raises(RecordConfigError):
        make("gdrive", tmp_path, options={"endpoint": "http://example.com", "instance": "x"})


# --- Identity -----------------------------------------------------------------------------------


def location(source: Any) -> ExternalObjectRef:
    ref: ExternalObjectRef = source.ref("issue/1", "updated:t")
    return ref


def test_identity_is_the_host_and_scope_so_two_sites_never_share_an_identity(
    tmp_path: Path,
) -> None:
    a = make("jira", tmp_path, "jira://a.atlassian.net/OPS")
    b = make("jira", tmp_path, "jira://b.atlassian.net/OPS")
    c = make("jira", tmp_path, "jira://a.atlassian.net/ARM")
    assert location(a) == ExternalObjectRef(
        "deploy_jira", "a.atlassian.net/OPS/issue/1", "updated:t"
    )
    assert len({location(a).key, location(b).key, location(c).key}) == 3


def test_a_declared_instance_name_survives_a_move_to_another_host(tmp_path: Path) -> None:
    before = make("jira", tmp_path, "jira://old.example.com/OPS", options={"instance": "plant-2"})
    after = make("jira", tmp_path, "jira://new.example.com/OPS", options={"instance": "plant-2"})
    assert location(before) == location(after)
    assert location(before).object_id == "@plant-2/OPS/issue/1"


def test_a_non_default_port_is_part_of_an_undeclared_instance(tmp_path: Path) -> None:
    source = make("jira", tmp_path, "jira://jira.example.com:8443/OPS")
    assert location(source).object_id == "jira.example.com:8443/OPS/issue/1"


def test_the_transform_names_what_decided_the_records_and_holds_no_endpoint_or_secret(
    tmp_path: Path,
) -> None:
    url = "jira://a.atlassian.net/OPS"
    named = {"instance": "plant-2"}
    a = make("jira", tmp_path, url, options=named)
    b = make("jira", tmp_path, url, options={**named, "fields": ["summary"]})
    since = "deploy_jira/1:2026-08-03T11:40:00.000+0200"
    c = make("jira", tmp_path, url, options={**named, "since": since})
    moved = make("jira", tmp_path, "jira://b.example.com/OPS", options=named)
    assert a.transform.id != b.transform.id  # other fields, other bytes: another lineage
    assert a.transform.id == c.transform.id  # a cursor says from when, not what
    assert a.transform.id == moved.transform.id  # where it is hosted is not what was read
    text = repr(a.transform.config)
    assert "atlassian" not in text and "jira-secret" not in text


# --- Credentials --------------------------------------------------------------------------------


def test_credentials_are_declared_or_the_neptune_variables_and_never_ambient(
    tmp_path: Path,
) -> None:
    environ = {
        "NEPTUNE_JIRA_EMAIL": "ops@example.com",
        "NEPTUNE_JIRA_API_TOKEN": "env-token",
        "JIRA_API_TOKEN": "ambient",
        "ATLASSIAN_API_TOKEN": "ambient",
    }
    source = jira_source("jira://a.atlassian.net/OPS", network=online(tmp_path), environ=environ)
    assert source.system.api._auth.value.startswith("Basic ")
    with pytest.raises(RecordConfigError, match="missing credentials"):
        jira_source(
            "jira://a.atlassian.net/OPS",
            network=online(tmp_path),
            environ={"JIRA_API_TOKEN": "ambient", "ATLASSIAN_API_TOKEN": "ambient"},
        )


def test_the_process_environment_is_used_only_when_no_environ_is_given(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NEPTUNE_DRIVE_ACCESS_TOKEN", SECRET)
    with pytest.raises(RecordConfigError):  # not this connector's variable
        gdrive_source("gdrive://my-drive", network=online(tmp_path))
    monkeypatch.setenv("NEPTUNE_GDRIVE_ACCESS_TOKEN", SECRET)
    gdrive_source("gdrive://my-drive", network=online(tmp_path))


@pytest.mark.parametrize(
    "credentials",
    [
        {"email": "a@b.c", "api_token": "line\nbreak"},
        {"email": "a@b.c", "api_token": "x\r\nX-Injected: 1"},
        {"email": "a@b.c", "api_token": " padded"},
        {"email": "a:b@b.c", "api_token": "t"},
        {"email": "a@b.c", "api_token": ""},
        {"email": "a@b.c"},
        {"api_token": "t"},
        {"email": "a@b.c", "api_token": "t", "access_token": "u"},
        {"email": "a@b.c", "api_token": "t", "extra": "u"},
    ],
)
def test_credentials_that_are_not_clean_header_text_or_not_one_scheme_are_refused(
    tmp_path: Path, credentials: dict[str, str]
) -> None:
    with pytest.raises(RecordConfigError) as raised:
        make("jira", tmp_path, credentials=credentials)
    text = chain(raised.value)
    assert "X-Injected" not in text and "line" not in text


def test_a_bearer_token_alone_is_a_complete_jira_credential(tmp_path: Path) -> None:
    source = make("jira", tmp_path, credentials={"access_token": "oauth-token"})
    assert source.system.api._auth.value == "Bearer oauth-token"


def test_the_rest_profile_decides_whether_the_credential_is_a_key_or_a_bearer(
    tmp_path: Path,
) -> None:
    with pytest.raises(RecordConfigError):
        make("rest", tmp_path, credentials={"access_token": "t"})  # the profile sends an API key
    profile = {k: v for k, v in cmms_profile().items() if k != "auth"}
    source = rest_source(
        "rest://cmms.example.com",
        network=online(tmp_path),
        options={"profile": profile},
        credentials={"access_token": "t"},
    )
    assert source.system.api._auth.header == "Authorization"
    with pytest.raises(RecordConfigError):
        rest_source(
            "rest://cmms.example.com",
            network=online(tmp_path),
            options={"profile": profile},
            credentials={"api_key": "k"},
        )


def test_no_exception_chain_holds_a_credential(tmp_path: Path) -> None:
    attempts: list[Callable[[], Any]] = [
        lambda: make(
            "jira", tmp_path, credentials={"email": "a@b.c", "api_token": SECRET, "x": "y"}
        ),
        lambda: make("gdrive", tmp_path, credentials={"access_token": SECRET + " space"}),
        lambda: make(
            "gdrive",
            tmp_path,
            options={"endpoint": f"http://user:{SECRET}@127.0.0.1:1", "instance": "x"},
        ),
        lambda: make("jira", tmp_path, f"jira://admin:{SECRET}@a.atlassian.net/OPS"),
        lambda: make("jira", tmp_path, f"jira://a.atlassian.net/OPS?token={SECRET}"),
        lambda: make("jira", tmp_path, options={"instance": SECRET.upper()}),
        lambda: make("servicenow", tmp_path, options={"since": SECRET}),
    ]
    for attempt in attempts:
        with pytest.raises((RecordConfigError, ValueError)) as raised:
            attempt()
        assert SECRET not in chain(raised.value)


def test_credentials_never_appear_in_a_repr_a_transform_or_a_finding(tmp_path: Path) -> None:
    server = FakeServer(JiraBackend())
    with server.serve() as host:
        source = jira_source(
            f"jira://{host}/OPS",
            network=online(tmp_path),
            options={"scheme": "http", "instance": "site-a"},
            credentials={"email": "ops@example.com", "api_token": SECRET},
        )
        source.listing()  # refused with 401: the fake wants another token
        text = repr(source.findings()) + repr(source.transform) + repr(source.system.api)
        text += repr(source.system.api._auth) + repr(source)
        assert SECRET not in text and "ops@example.com" not in text


def test_record_source_refuses_an_unknown_connector(tmp_path: Path) -> None:
    with pytest.raises(KeyError):
        record_source("deploy_nothing", "x://y", network=online(tmp_path))
