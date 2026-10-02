"""What an object-store source accepts: URLs, options, endpoints, credentials (ADR 0006)."""

import importlib
from importlib.metadata import distribution
from pathlib import Path
from typing import Any

import pytest

from neptune.store.workspace import Workspace
from neptune_deploy.sources.object_store import (
    CONNECTOR_IDS,
    ObjectStoreConfigError,
    Provider,
    azure_source,
    gcs_source,
    s3_source,
)
from neptune_deploy.sources.object_store.clients import Addressing, PageInvalid, _xml
from neptune_deploy.sources.object_store.config import (
    Options,
    credentials_for,
    endpoint_for,
    parse_url,
)
from neptune_deploy.sources.object_store.transport import Endpoint

S3_KEYS = {"s3_access_key_id": "AKID", "s3_secret_access_key": "secret"}


# --- URLs ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "provider", "bucket", "prefix", "account"),
    [
        ("s3://fleet-logs", Provider.S3, "fleet-logs", "", None),
        ("s3://fleet-logs/", Provider.S3, "fleet-logs", "", None),
        ("s3://fleet.logs/amr-07/2026/", Provider.S3, "fleet.logs", "amr-07/2026/", None),
        ("S3://fleet-logs/a//b/../c", Provider.S3, "fleet-logs", "a//b/../c", None),
        ("s3://fleet-logs/%2e%2e/café?x#y", Provider.S3, "fleet-logs", "%2e%2e/café?x#y", None),
        ("gs://my_bucket/arm/", Provider.GCS, "my_bucket", "arm/", None),
        ("az://acct01/cell-3/plc/", Provider.AZURE, "cell-3", "plc/", "acct01"),
    ],
)
def test_a_url_names_a_bucket_and_a_verbatim_prefix(
    url: str, provider: Provider, bucket: str, prefix: str, account: str | None
) -> None:
    location = parse_url(url, provider)
    assert (location.bucket, location.prefix, location.account) == (bucket, prefix, account)
    assert location.object_id("k") == (f"{account}/" if account else "") + f"{bucket}/k"


@pytest.mark.parametrize(
    ("url", "provider"),
    [
        ("gs://fleet-logs/", Provider.S3),  # another connector's scheme
        ("s3:/fleet-logs", Provider.S3),
        ("fleet-logs/x", Provider.S3),
        ("s3://", Provider.S3),
        ("s3://ab/", Provider.S3),  # too short
        ("s3://Fleet/", Provider.S3),  # upper case
        ("s3://fleet..logs/", Provider.S3),
        ("s3://../etc/passwd", Provider.S3),
        ("s3://-fleet/", Provider.S3),
        ("s3://user:pw@fleet/", Provider.S3),
        ("s3://fleet-logs/" + "x" * 1025, Provider.S3),
        ("s3://fleet-logs/\udc80", Provider.S3),  # a lone surrogate
        ("az://acct01/", Provider.AZURE),  # no container
        ("az://ACCT/c-1/", Provider.AZURE),
        ("az://acct01/c_1/", Provider.AZURE),
    ],
)
def test_a_url_that_names_no_valid_bucket_is_refused(url: str, provider: Provider) -> None:
    with pytest.raises(ObjectStoreConfigError):
        parse_url(url, provider)


# --- Options and endpoints -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("options", "provider"),
    [
        ({"unknown": 1}, Provider.S3),
        ({"region": "us-east-1"}, Provider.GCS),  # S3 only
        ({"versions": False}, Provider.AZURE),
        ({"region": "US East"}, Provider.S3),
        ({"addressing": "dns"}, Provider.S3),
        ({"versions": "yes"}, Provider.S3),
        ({"max_objects": 0}, Provider.S3),
        ({"max_objects": True}, Provider.S3),
        ({"page_size": 1001}, Provider.S3),
        ({"timeout": 0}, Provider.S3),
        ({"endpoint": 5, "store": "site-a"}, Provider.S3),
        ({"endpoint": "http://127.0.0.1:9000"}, Provider.S3),  # whose bucket namespace?
        ({"store": "site-a"}, Provider.GCS),  # a public endpoint's namespace is global
        ({"endpoint": "http://127.0.0.1:9000", "store": "Site A"}, Provider.AZURE),
        ({"endpoint": "http://127.0.0.1:9000", "store": "a:b"}, Provider.S3),
    ],
)
def test_options_are_closed_and_checked(options: dict[str, Any], provider: Provider) -> None:
    with pytest.raises(ObjectStoreConfigError):
        Options.parse(options, provider)


@pytest.mark.parametrize(
    "url",
    [
        "http://minio.internal:9000",  # plain http off the loopback
        "https://user:pw@minio:9000",
        "https://minio:9000/?x=1",
        "https://minio:99999",
        "ftp://minio",
        "https://",
        "http://::1:9000",  # IPv6 unbracketed
        "http://minio:9000:80",
    ],
)
def test_endpoints_are_https_or_loopback_http(url: str) -> None:
    location = parse_url("s3://fleet-logs/", Provider.S3)
    with pytest.raises(ObjectStoreConfigError):
        endpoint_for(location, Options(endpoint=url))


def test_endpoints_and_addressing() -> None:
    s3 = parse_url("s3://fleet-logs/", Provider.S3)
    assert endpoint_for(s3, Options(region="eu-west-1")) == (
        Endpoint("https", "fleet-logs.s3.eu-west-1.amazonaws.com", 443),
        Addressing.VIRTUAL,
    )
    assert endpoint_for(s3, Options(endpoint="http://127.0.0.1:9000/")) == (
        Endpoint("http", "127.0.0.1", 9000),
        Addressing.PATH,
    )
    assert endpoint_for(s3, Options(endpoint="http://[::1]:9000/minio")) == (
        Endpoint("http", "::1", 9000, "/minio"),
        Addressing.PATH,
    )
    dotted = parse_url("s3://fleet.logs/", Provider.S3)
    with pytest.raises(ObjectStoreConfigError):
        endpoint_for(dotted, Options())  # a dotted bucket host fails TLS
    assert endpoint_for(dotted, Options(addressing=Addressing.PATH))[0].host == (
        "s3.us-east-1.amazonaws.com"
    )
    azure = parse_url("az://acct01/cell-3/", Provider.AZURE)
    assert endpoint_for(azure, Options())[0].host == "acct01.blob.core.windows.net"
    gcs = parse_url("gs://fleet-logs/", Provider.GCS)
    assert endpoint_for(gcs, Options())[0] == Endpoint("https", "storage.googleapis.com", 443)
    assert Endpoint.parse("http://LocalHost:9000").host == "localhost"
    assert Endpoint("https", "h", 8443).authority == "h:8443"
    assert Endpoint("http", "::1", 80).authority == "[::1]"


# --- Credentials ---------------------------------------------------------------------------------


def test_credentials_come_from_the_declaration_or_neptune_variables_only() -> None:
    ambient = {"AWS_ACCESS_KEY_ID": "admin", "AWS_SECRET_ACCESS_KEY": "admin-secret"}
    with pytest.raises(ObjectStoreConfigError, match="NEPTUNE_S3_ACCESS_KEY_ID"):
        credentials_for(Provider.S3, None, ambient, anonymous=False)
    env = {"NEPTUNE_S3_ACCESS_KEY_ID": "ro", "NEPTUNE_S3_SECRET_ACCESS_KEY": "ro-secret"}
    found = credentials_for(Provider.S3, None, {**ambient, **env}, anonymous=False)
    assert found.aws is not None and found.aws.access_key_id == "ro"
    declared = credentials_for(Provider.S3, S3_KEYS, env, anonymous=False)  # declared wins whole
    assert declared.aws is not None and declared.aws.access_key_id == "AKID"
    assert "secret" not in repr(declared) and "secret" not in repr(declared.aws)
    gcs = credentials_for(
        Provider.GCS, None, {"NEPTUNE_GCS_ACCESS_TOKEN": "ya29.x"}, anonymous=False
    )
    assert gcs.gcs_token == "ya29.x"


@pytest.mark.parametrize(
    ("provider", "declared", "anonymous"),
    [
        (Provider.S3, {"s3_access_key_id": "AKID"}, False),  # no secret
        (Provider.S3, {**S3_KEYS, "region": "x"}, False),  # not a credential
        (Provider.S3, S3_KEYS, True),  # anonymous and credentials
        (Provider.S3, {"s3_access_key_id": "AK\nID", "s3_secret_access_key": "s"}, False),
        (Provider.GCS, {}, False),
        (Provider.GCS, {"gcs_access_token": "two words"}, False),
        (Provider.AZURE, {"azure_sas_token": "sp=rl"}, False),  # no signature
        (Provider.AZURE, {"azure_sas_token": "sig=x&sp=rl&sp=rw"}, False),  # repeated
        (Provider.AZURE, {"azure_sas_token": "sig=x&sp=rl&comp=list"}, False),  # sets a request
        (Provider.AZURE, {"azure_sas_token": "sig=x&sp=rld"}, False),  # can delete
        (Provider.AZURE, {"azure_sas_token": "sig=x"}, False),  # permissions unstated
        (Provider.AZURE, {"azure_sas_token": "sig=x&sp=rl&&"}, False),  # not a query string
        (Provider.AZURE, {"azure_sas_token": "sig=x&sp=rl&flag"}, False),
    ],
)
def test_credentials_that_are_missing_or_writable_are_refused(
    provider: Provider, declared: dict[str, str], anonymous: bool
) -> None:
    with pytest.raises(ObjectStoreConfigError):
        credentials_for(provider, declared, {}, anonymous=anonymous)


def test_a_sas_signature_keeps_its_plus_signs() -> None:
    token = "sv=2021-08-06&sp=rl&sig=ab+c/d%2Be%3D"
    found = credentials_for(Provider.AZURE, {"azure_sas_token": token}, {}, anonymous=False)
    assert found.azure_sas is not None and dict(found.azure_sas)["sig"] == "ab+c/d+e="


def test_anonymous_access_needs_no_credentials() -> None:
    for provider in Provider:
        found = credentials_for(provider, None, {}, anonymous=True)
        assert (found.aws, found.gcs_token, found.azure_sas) == (None, None, None)


# --- Registration and the network boundary -----------------------------------------------------


def test_each_provider_is_one_entry_point_named_by_its_connector_id() -> None:
    points = {
        ep.name: ep.value
        for ep in distribution("neptune-deploy").entry_points
        if ep.group == "neptune.sources" and ep.name != "deploy_foxglove"  # its own ADR (0007)
    }
    factories = {"s3": s3_source, "gcs": gcs_source, "azure": azure_source}
    assert set(points) == set(CONNECTOR_IDS.values())
    for provider, connector in CONNECTOR_IDS.items():
        module, _, name = points[connector].partition(":")
        assert getattr(importlib.import_module(module), name) is factories[provider.value]


@pytest.mark.parametrize(
    ("factory", "url"),
    [
        (s3_source, "s3://fleet-logs/"),
        (gcs_source, "gs://fleet-logs/"),
        (azure_source, "az://acct01/fleet-logs/"),
    ],
)
def test_a_local_only_workspace_refuses_every_connector(
    tmp_path: Path, factory: Any, url: str
) -> None:
    from neptune.store.workspace import LocalOnlyError

    with pytest.raises(LocalOnlyError, match="local-only"):
        factory(url, network=Workspace(tmp_path / "home"), options={"anonymous": True})


def test_building_a_source_sends_nothing(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "home")
    workspace.allow_network(True)
    source = s3_source(
        "s3://fleet-logs/amr/",
        network=workspace,
        options={"endpoint": "http://127.0.0.1:9", "store": "site-a"},
        credentials=S3_KEYS,
    )
    assert source.client.transport.requests == 0
    assert source.transform.adapter_id == "deploy_s3"
    assert dict(source.transform.config) == {
        "bucket": "fleet-logs",
        "max_listing_bytes": 256 * 1024 * 1024,
        "max_objects": 1_000_000,
        "prefix": "amr/",
        "provider": "s3",
        "store": "site-a",
        "versions": True,
    }
    assert source.ref("k", "etag:x").object_id == "site-a:fleet-logs/k"


# --- Listing XML ---------------------------------------------------------------------------------

ENTITY_PAGE = (
    '<?xml version="1.0" encoding="{encoding}"?>'
    '<!DOCTYPE r [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;">]>'
    "<ListBucketResult><Contents><Key>&b;</Key></Contents></ListBucketResult>"
)


@pytest.mark.parametrize(
    "body",
    [
        ENTITY_PAGE.format(encoding="UTF-8").encode("utf-8"),
        ENTITY_PAGE.format(encoding="UTF-16").encode("utf-16"),  # hides from a byte search
        ENTITY_PAGE.format(encoding="UTF-32").encode("utf-32"),
        b'<?xml version="1.0"?><! DOCTYPE r><r/>',
        b"<r>\xff</r>",
        b"<r>",
    ],
)
def test_a_listing_page_that_declares_entities_or_is_not_utf8_is_refused(body: bytes) -> None:
    with pytest.raises(PageInvalid):
        _xml(body)


def test_a_listing_page_is_parsed_as_utf8_whatever_it_declares() -> None:
    page = '﻿<?xml version="1.0" encoding="UTF-16"?><r><k>café</k></r>'
    assert _xml(page.encode("utf-8")).findtext("k") == "café"
