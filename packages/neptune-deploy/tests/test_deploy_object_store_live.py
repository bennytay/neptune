"""The S3 connector against a real S3-compatible server, when one is available (ADR 0006 §8).

Skipped unless ``NEPTUNE_TEST_S3_ENDPOINT`` names one (a loopback ``http`` URL or any ``https``
URL), with ``NEPTUNE_TEST_S3_ACCESS_KEY_ID`` and ``NEPTUNE_TEST_S3_SECRET_ACCESS_KEY`` (MinIO's
defaults when unset). For example::

    minio server /tmp/minio --address 127.0.0.1:9000 &
    NEPTUNE_TEST_S3_ENDPOINT=http://127.0.0.1:9000 make check PKG=neptune-deploy

or ``moto_server -p 9000`` in place of MinIO. The test creates its own bucket and writes to it
through a small signed admin client of its own; the connector itself only ever reads.
"""

import hashlib
import http.client
import os
import secrets
from datetime import UTC, datetime
from pathlib import Path

import pytest

from neptune.identity.hashing import digest_stream
from neptune.identity.revisions import SourceLedger
from neptune.store.workspace import Workspace
from neptune_deploy.sources.object_store import ObjectEntry, ObjectStoreSource, s3_source
from neptune_deploy.sources.object_store.sigv4 import AwsCredentials, quote, sign
from neptune_deploy.sources.object_store.transport import Endpoint

ENDPOINT = os.environ.get("NEPTUNE_TEST_S3_ENDPOINT")
KEY_ID = os.environ.get("NEPTUNE_TEST_S3_ACCESS_KEY_ID", "minioadmin")
SECRET = os.environ.get("NEPTUNE_TEST_S3_SECRET_ACCESS_KEY", "minioadmin")
REGION = os.environ.get("NEPTUNE_TEST_S3_REGION", "us-east-1")

pytestmark = pytest.mark.skipif(
    not ENDPOINT, reason="no S3-compatible server: set NEPTUNE_TEST_S3_ENDPOINT to run"
)


class Admin:
    """The test's own writer: signed PUT and DELETE, which the connector can never send."""

    def __init__(self, endpoint: str, bucket: str) -> None:
        self.endpoint = Endpoint.parse(endpoint)
        self.bucket = bucket
        self.credentials = AwsCredentials(KEY_ID, SECRET)

    def request(
        self, method: str, key: str | None, body: bytes = b"", query: str = ""
    ) -> http.client.HTTPResponse:
        path = f"{self.endpoint.base_path}/{self.bucket}"
        if key is not None:
            path += "/" + quote(key, safe="/")
        signed = sign(
            method=method,
            host=self.endpoint.authority,
            path=path,
            query=[(query, "")] if query else [],
            headers={},
            credentials=self.credentials,
            region=REGION,
            when=datetime.now(UTC),
            payload_sha256=hashlib.sha256(body).hexdigest(),
        )
        cls = (
            http.client.HTTPSConnection
            if self.endpoint.scheme == "https"
            else http.client.HTTPConnection
        )
        connection = cls(self.endpoint.host, self.endpoint.port, timeout=30)
        connection.request(
            method,
            path + (f"?{query}" if query else ""),
            body=body,
            headers={**signed, "Host": self.endpoint.authority},
        )
        response = connection.getresponse()
        response.read()
        connection.close()
        return response

    def put(self, key: str, body: bytes) -> bool:
        return self.request("PUT", key, body).status == 200


@pytest.fixture
def admin() -> Admin:
    assert ENDPOINT is not None
    store = Admin(ENDPOINT, f"neptune-test-{secrets.token_hex(6)}")
    assert store.request("PUT", None).status == 200, "could not create the test bucket"
    versioning = b"<VersioningConfiguration><Status>Enabled</Status></VersioningConfiguration>"
    store.request("PUT", None, versioning, "versioning")  # a single-drive server may refuse it
    return store


def _source(tmp_path: Path, admin: Admin, ledger: SourceLedger | None = None) -> ObjectStoreSource:
    workspace = Workspace(tmp_path / "home")
    workspace.allow_network(True)
    assert ENDPOINT is not None
    return s3_source(
        f"s3://{admin.bucket}/",
        network=workspace,
        ledger=ledger,
        options={
            "endpoint": ENDPOINT,
            "store": "live",
            "region": REGION,
            "page_size": 100,
        },
        credentials={"s3_access_key_id": KEY_ID, "s3_secret_access_key": SECRET},
    )


def test_a_live_bucket_lists_reads_and_revises(tmp_path: Path, admin: Admin) -> None:
    for index in range(250):
        assert admin.put(f"fleet/amr-{index % 5}/{index:04d}.bag", f"bag {index}".encode())
    assert admin.put("arm/joint_states.mcap", b"v1" * 70_000)
    hostile = ["robots/a//b", "robots/a/../b", "robots/café", "robots/café"]
    accepted = [key for key in hostile if admin.put(key, key.encode())]

    ledger = SourceLedger()
    source = _source(tmp_path, admin)
    listing = source.listing()
    assert listing.complete and source.findings() == ()
    assert len(listing.entries) == 251 + len(accepted)
    tokens = {entry.location.revision_token.partition(":")[0] for entry in listing.entries}
    assert tokens in ({"version"}, {"etag"})
    for entry in listing.entries:
        with source.open(entry.location) as stream:
            ledger.observe(entry.location, digest_stream(stream, chunk_size=64 * 1024))
        if entry.key in accepted:
            with source.open(entry.location) as stream:
                assert stream.read() == entry.key.encode()  # its own bytes, never normalised

    assert admin.put("arm/joint_states.mcap", b"v2" * 70_000)
    later = _source(tmp_path, admin, ledger)
    (changed,) = [e for e in later.walk() if isinstance(e, ObjectEntry)]
    assert changed.key == "arm/joint_states.mcap"
    with later.open(changed.location) as stream:
        artifact = digest_stream(stream, chunk_size=64 * 1024)
    observation = ledger.observe(changed.location, artifact)
    assert observation.new_revision and len(observation.revision.supersedes) == 1
    reader = later.reader(changed.location, artifact)
    assert reader.read(100_000, 4) == b"v2v2"
    assert later.findings() == ()
