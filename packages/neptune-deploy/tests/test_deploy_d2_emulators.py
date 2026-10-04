"""GCS and Azure Blob against their emulators, when one is running (ADR 0006 §2, MVL-158).

ADR 0006 counts the GCS and Azure connectors as verified once each has run against its emulator:
fake-gcs-server for GCS's JSON API, Azurite for Blob Storage. These tests are that run. They are
skipped unless the emulator is named, and add no dependency: the emulators are external oracles.

    fake-gcs-server -scheme http -host 127.0.0.1 -port 4443 -backend memory &
    npx -y -p azurite@3 azurite-blob --blobHost 127.0.0.1 --blobPort 10000 --inMemoryPersistence &
    NEPTUNE_TEST_GCS_ENDPOINT=http://127.0.0.1:4443 \\
    NEPTUNE_TEST_AZURE_ENDPOINT=http://127.0.0.1:10000/devstoreaccount1 \\
        uv run --all-packages --all-groups pytest tests/test_deploy_d2_emulators.py

S3 runs against moto server (``uv run --no-project --with 'moto[server]' moto_server -p 5000``,
``NEPTUNE_TEST_S3_ENDPOINT=http://127.0.0.1:5000``), as ``test_deploy_object_store_live.py`` does
at scale. Each test writes its own
bucket or container through a small admin client of its own; the connector only ever reads, and
``Wire`` records every request it sent.
"""

import base64
import hashlib
import hmac
import json
import os
import secrets
import urllib.parse
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from deploy_d2_proxy import Upstream
from deploy_d2_support import Wire, emitted, entries, fingerprint, ledger_json, spellings
from neptune.identity.revisions import SourceLedger
from neptune.store.workspace import LocalOnlyError, Workspace
from neptune_deploy.sources.object_store import azure_source, gcs_source, s3_source
from neptune_deploy.sources.object_store.sigv4 import AwsCredentials, quote, sign
from neptune_deploy.sources.object_store.transport import Endpoint

S3 = os.environ.get("NEPTUNE_TEST_S3_ENDPOINT")
S3_KEY_ID = os.environ.get("NEPTUNE_TEST_S3_ACCESS_KEY_ID", "testing")
S3_SECRET = os.environ.get("NEPTUNE_TEST_S3_SECRET_ACCESS_KEY", "testing-secret-never-printed")
GCS = os.environ.get("NEPTUNE_TEST_GCS_ENDPOINT")
AZURE = os.environ.get("NEPTUNE_TEST_AZURE_ENDPOINT")
AZURE_ACCOUNT = os.environ.get("NEPTUNE_TEST_AZURE_ACCOUNT", "devstoreaccount1")
# Azurite's documented, public development key: not a secret.
AZURE_KEY = os.environ.get(
    "NEPTUNE_TEST_AZURE_KEY",
    "Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw==",
)
GCS_TOKEN = "ya29.emulator-token-never-printed"
OBJECTS = {
    "arm-cell/joint_states.mcap": b"\x89MCAP0\r\n arm cell take 1" * 3000,
    "amr-07/route.geojson": b'{"type": "FeatureCollection", "features": []}',
    "legged/patrol.bag": b"#ROSBAG V2.0 legged patrol",
    "marine/a//b.csv": b"t,thrust\n0,0.1\n",  # a key with an empty segment, read verbatim
    "humanoid/café.yaml": b"gait: walk\n",  # NFD, never normalised
}


def _online(tmp_path: Path, name: str) -> Workspace:
    workspace = Workspace(tmp_path / name)
    workspace.allow_network(True)
    return workspace


def _request(
    endpoint: str, method: str, path: str, body: bytes = b"", headers: dict[str, str] | None = None
) -> tuple[int, bytes]:
    parsed = Endpoint.parse(endpoint)
    connection = Upstream(parsed.host, parsed.port, timeout=30)  # the test's, not the connector's
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def _check(source: Any, tmp_path: Path, secret: str, change: Any) -> None:
    """Every D2 guarantee the emulator can show: identity across syncs, a new revision with the
    old one kept, GET only, local-only refusal, no credential in any output, determinism."""
    wire = Wire()
    with wire.recording():
        ledger = SourceLedger()
        first = source()
        listed = entries(first.walk())
        assert sorted(e.key for e in listed) == sorted(OBJECTS)
        for entry in listed:
            with first.open(entry.location) as stream:
                assert stream.read() == OBJECTS[entry.key]  # its own bytes, keys verbatim
        fingerprint(first, ledger)
        text = emitted(first)
        assert first.findings() == ()
        assert emitted(source()) == text  # determinism: a second source, the same bytes
        again = source(ledger=ledger).discover(ledger)
        assert not again.new and not again.changed and len(again.unchanged) == len(OBJECTS)
        old = set(ledger.revisions())
        change()
        later = source(ledger=ledger)
        discovery = later.discover(ledger)
        assert [e.key for e in discovery.changed] == ["arm-cell/joint_states.mcap"]
        fingerprint(later, ledger)
        assert old < set(ledger.revisions()) and len(ledger.revisions()) == len(old) + 1
        texts = [text, emitted(later), ledger_json(ledger)]
    assert wire.sent and wire.methods() == {"GET"}
    for written in texts:
        assert not [s for s in spellings(secret) if s in written]
    with pytest.raises(LocalOnlyError):
        source(network=Workspace(tmp_path / "local"))


# --- S3: moto server ---------------------------------------------------------------------------


@pytest.mark.skipif(not S3, reason="no S3-compatible server: set NEPTUNE_TEST_S3_ENDPOINT to run")
def test_s3_against_moto_server(tmp_path: Path) -> None:
    assert S3 is not None
    bucket = f"neptune-d2-{secrets.token_hex(4)}"
    endpoint = Endpoint.parse(S3)
    credentials = AwsCredentials(S3_KEY_ID, S3_SECRET)

    def admin(method: str, path: str, query: str = "", body: bytes = b"") -> int:
        signed = sign(
            method=method,
            host=endpoint.authority,
            path=path,
            query=[(query, "")] if query else [],
            headers={},
            credentials=credentials,
            region="us-east-1",
            when=datetime.now(UTC),
            payload_sha256=hashlib.sha256(body).hexdigest(),
        )
        target = path + (f"?{query}" if query else "")
        headers = {**signed, "Host": endpoint.authority}
        return _request(S3, method, target, body, headers)[0]

    assert admin("PUT", f"/{bucket}") == 200, "could not create the test bucket"
    versioning = b"<VersioningConfiguration><Status>Enabled</Status></VersioningConfiguration>"
    admin("PUT", f"/{bucket}", "versioning", versioning)

    def put(key: str, data: bytes) -> None:
        assert admin("PUT", f"/{bucket}/{quote(key, safe='/')}", "", data) == 200

    for key, data in OBJECTS.items():
        put(key, data)

    def source(network: Any = None, ledger: Any = None) -> Any:
        return s3_source(
            f"s3://{bucket}/",
            network=network or _online(tmp_path, f"h{secrets.token_hex(2)}"),
            ledger=ledger,
            options={"endpoint": S3, "store": "emulator", "region": "us-east-1", "page_size": 2},
            credentials={"s3_access_key_id": S3_KEY_ID, "s3_secret_access_key": S3_SECRET},
        )

    _check(source, tmp_path, S3_SECRET, lambda: put("arm-cell/joint_states.mcap", b"take 2"))


# --- GCS: fake-gcs-server -----------------------------------------------------------------------


@pytest.mark.skipif(not GCS, reason="no GCS emulator: set NEPTUNE_TEST_GCS_ENDPOINT to run")
def test_gcs_against_fake_gcs_server(tmp_path: Path) -> None:
    assert GCS is not None
    bucket = f"neptune-d2-{secrets.token_hex(4)}"
    created = json.dumps({"name": bucket, "versioning": {"enabled": True}}).encode()
    status, _ = _request(
        GCS, "POST", "/storage/v1/b", created, {"Content-Type": "application/json"}
    )
    assert status == 200, "could not create the test bucket"

    def put(key: str, data: bytes) -> None:
        name = urllib.parse.quote(key, safe="")
        target = f"/upload/storage/v1/b/{bucket}/o?uploadType=media&name={name}"
        assert _request(GCS, "POST", target, data)[0] == 200

    for key, data in OBJECTS.items():
        put(key, data)

    def source(network: Any = None, ledger: Any = None) -> Any:
        return gcs_source(
            f"gs://{bucket}/",
            network=network or _online(tmp_path, f"h{secrets.token_hex(2)}"),
            ledger=ledger,
            options={"endpoint": GCS, "store": "emulator", "page_size": 2},
            credentials={"gcs_access_token": GCS_TOKEN},
        )

    _check(source, tmp_path, GCS_TOKEN, lambda: put("arm-cell/joint_states.mcap", b"take 2"))


# --- Azure Blob: Azurite ------------------------------------------------------------------------


def _account_sas(permissions: str) -> str:
    """An account SAS (version 2019-12-12) signed with the account key, as Azure documents it."""
    fields = {
        "sv": "2019-12-12",
        "ss": "b",
        "srt": "sco",
        "sp": permissions,
        "se": "2030-01-01T00:00:00Z",
        "spr": "https,http",
    }
    to_sign = "\n".join(
        [
            AZURE_ACCOUNT,
            fields["sp"],
            fields["ss"],
            fields["srt"],
            "",
            fields["se"],
            "",
            fields["spr"],
            fields["sv"],
            "",
        ]
    )
    key = base64.b64decode(AZURE_KEY)
    fields["sig"] = base64.b64encode(
        hmac.new(key, to_sign.encode("utf-8"), hashlib.sha256).digest()
    ).decode()
    return urllib.parse.urlencode(fields, quote_via=urllib.parse.quote)


@pytest.mark.skipif(not AZURE, reason="no Azure emulator: set NEPTUNE_TEST_AZURE_ENDPOINT to run")
def test_azure_blob_against_azurite(tmp_path: Path) -> None:
    assert AZURE is not None
    container = f"neptune-d2-{secrets.token_hex(4)}"
    base = Endpoint.parse(AZURE).base_path
    admin = _account_sas("rwdlac")
    version = {"x-ms-version": "2021-08-06"}
    status, _ = _request(
        AZURE, "PUT", f"{base}/{container}?restype=container&{admin}", b"", version
    )
    assert status == 201, "could not create the test container"

    def put(key: str, data: bytes) -> None:
        target = f"{base}/{container}/{urllib.parse.quote(key, safe='/')}?{admin}"
        headers = {**version, "x-ms-blob-type": "BlockBlob"}
        assert _request(AZURE, "PUT", target, data, headers)[0] == 201

    for key, data in OBJECTS.items():
        put(key, data)
    read_only = _account_sas("rl")
    signature = urllib.parse.parse_qs(read_only)["sig"][0]

    def source(network: Any = None, ledger: Any = None) -> Any:
        return azure_source(
            f"az://{AZURE_ACCOUNT}/{container}/",
            network=network or _online(tmp_path, f"h{secrets.token_hex(2)}"),
            ledger=ledger,
            options={"endpoint": AZURE, "store": "emulator", "page_size": 2},
            credentials={"azure_sas_token": read_only},
        )

    _check(source, tmp_path, signature, lambda: put("arm-cell/joint_states.mcap", b"take 2"))
