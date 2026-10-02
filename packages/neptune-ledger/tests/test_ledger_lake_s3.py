"""The lakehouse view over packages held in an S3-compatible object store (MVL-95, ADR 0013).

The packages of ``test_ledger_lake`` are registered from local roots, then mirrored byte for
byte into a bucket, and the ``SeriesCatalog`` is told to find each package there. Both engines
must read the same table as from the local roots, with the window pushed to the scan.

These tests need a live S3-compatible service and are skipped without one:

- ``NEPTUNE_LEDGER_S3_ENDPOINT`` (``http://127.0.0.1:9000``), with ``NEPTUNE_LEDGER_S3_ACCESS_KEY``,
  ``NEPTUNE_LEDGER_S3_SECRET_KEY`` and optionally ``NEPTUNE_LEDGER_S3_REGION``, names a running
  MinIO (or any S3-compatible server) the tests may create buckets on; or
- a ``minio`` binary on ``PATH``, which the tests start on a free local port for the session.
"""

import itertools
import os
import shutil
import socket
import subprocess
import time
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from neptune_ledger.api.types import TimeWindow
from neptune_ledger.lake.read import DataFusionReader, DuckDBReader, plan_series
from neptune_ledger.lake.series import SeriesCatalog
from neptune_ledger.lake.store import ObjectStore, S3ObjectStore, S3Settings, StoreError
from test_ledger_lake import (
    START,
    _datafusion_scan_filter,
    _duckdb_scan_filter,
    arm_run,
    catalog,
    read_all,
    series,
)

__all__ = ["arm_run", "catalog", "series"]  # fixtures this module reuses
_buckets = itertools.count()


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture(scope="session")
def s3(tmp_path_factory: pytest.TempPathFactory) -> Iterator[S3Settings]:
    endpoint = os.environ.get("NEPTUNE_LEDGER_S3_ENDPOINT")
    if endpoint:
        yield S3Settings(
            region=os.environ.get("NEPTUNE_LEDGER_S3_REGION", "us-east-1"),
            endpoint=endpoint,
            access_key=os.environ.get("NEPTUNE_LEDGER_S3_ACCESS_KEY"),
            secret_key=os.environ.get("NEPTUNE_LEDGER_S3_SECRET_KEY"),
            allow_http=endpoint.startswith("http://"),
        )
        return
    binary = shutil.which("minio")
    if binary is None:
        pytest.skip("no S3-compatible service: set NEPTUNE_LEDGER_S3_ENDPOINT or put minio on PATH")
    port = _free_port()
    user, password = "neptune-ledger", "neptune-ledger-test"
    env = {**os.environ, "MINIO_ROOT_USER": user, "MINIO_ROOT_PASSWORD": password}
    data = tmp_path_factory.mktemp("minio")
    server = subprocess.Popen(
        [binary, "server", str(data), "--address", f"127.0.0.1:{port}", "--quiet"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        url = f"http://127.0.0.1:{port}"
        for _ in range(100):
            try:
                urllib.request.urlopen(f"{url}/minio/health/live", timeout=1)
                break
            except OSError:
                time.sleep(0.1)
        else:
            pytest.fail("minio did not start")
        yield S3Settings("us-east-1", url, user, password, allow_http=True)
    finally:
        server.terminate()
        server.wait(timeout=10)


def _bucket(settings: S3Settings) -> str:
    import pyarrow.fs as pafs

    name = f"ledger-lake-{os.getpid()}-{next(_buckets)}"
    endpoint = str(settings.endpoint)
    admin = pafs.S3FileSystem(
        access_key=settings.access_key,
        secret_key=settings.secret_key,
        region=settings.region,
        endpoint_override=endpoint.split("://", 1)[1],
        scheme=endpoint.split("://", 1)[0],
        allow_bucket_creation=True,
    )
    admin.create_dir(name)
    return name


def _mirror(settings: S3Settings, bucket: str, roots: dict[str, Path]) -> None:
    """Every package file, byte for byte, under ``packages/<package id>/`` in ``bucket``."""
    import pyarrow.fs as pafs

    endpoint = str(settings.endpoint)
    fs = pafs.S3FileSystem(
        access_key=settings.access_key,
        secret_key=settings.secret_key,
        region=settings.region,
        endpoint_override=endpoint.split("://", 1)[1],
        scheme=endpoint.split("://", 1)[0],
    )
    for package_id, root in roots.items():
        for path in sorted(root.rglob("*")):
            if path.is_file():
                key = f"{bucket}/packages/{package_id}/{path.relative_to(root).as_posix()}"
                with fs.open_output_stream(key) as out:
                    out.write(path.read_bytes())


@pytest.fixture
def mirrored(
    s3: S3Settings, pg_uri: str, arm_run: dict[str, Any], series: SeriesCatalog
) -> Iterator[dict[str, Any]]:
    bucket = _bucket(s3)
    roots = dict(zip((p for p, _ in arm_run["pairs"]), arm_run["roots"].values(), strict=True))
    _mirror(s3, bucket, roots)

    def locate(package_id: str, root_locator: str) -> ObjectStore:
        del root_locator  # the mirror, not the registered root
        return S3ObjectStore(s3, bucket, f"packages/{package_id}")

    with SeriesCatalog(pg_uri, "acme", locate=locate) as remote:
        yield {"remote": remote, "local": series, "bucket": bucket, "run": arm_run}


@pytest.mark.slow
def test_a_run_spanning_two_packages_reads_the_same_from_s3(mirrored: dict[str, Any]) -> None:
    pairs = mirrored["run"]["pairs"]
    remote = mirrored["remote"].files(pairs)
    assert remote.findings == ()
    assert all(f.location.url.startswith(f"s3://{mirrored['bucket']}/") for f in remote.files)
    local = mirrored["local"].files(pairs)
    assert read_all(plan_series(remote.files)).equals(read_all(plan_series(local.files)))


@pytest.mark.slow
def test_a_window_is_pushed_to_the_s3_scan(mirrored: dict[str, Any]) -> None:
    files = mirrored["remote"].files(mirrored["run"]["pairs"]).files
    low, high = START + 10_000, START + 19_000
    plan = plan_series(files, windows=[TimeWindow(files[0].clocks[0], low, high)])
    assert read_all(plan).column("seq").to_pylist() == list(range(10, 20))
    assert _duckdb_scan_filter(DuckDBReader().explain(plan), "time/0", low, high)
    assert _datafusion_scan_filter(DataFusionReader().explain(plan), "time/0", low, high)


@pytest.mark.slow
@pytest.mark.parametrize(
    ("how", "code"),
    [("manifest", "manifest_digest_mismatch"), ("series", "file_missing")],
)
def test_an_object_changed_in_the_bucket_is_a_finding(
    s3: S3Settings, mirrored: dict[str, Any], how: str, code: str
) -> None:
    import pyarrow.fs as pafs

    endpoint = str(s3.endpoint)
    fs = pafs.S3FileSystem(
        access_key=s3.access_key,
        secret_key=s3.secret_key,
        region=s3.region,
        endpoint_override=endpoint.split("://", 1)[1],
        scheme=endpoint.split("://", 1)[0],
    )
    package_id, stream_id = mirrored["run"]["pairs"][0]
    prefix = f"{mirrored['bucket']}/packages/{package_id}"
    if how == "manifest":
        with fs.open_output_stream(f"{prefix}/manifest.json") as out:
            out.write(b"{}\n")
    else:
        fs.delete_file(f"{prefix}/series/{stream_id.removeprefix('rec:sha256:')}.parquet")
    selection = mirrored["remote"].files([(package_id, stream_id)])
    assert [f.code for f in selection.findings] == [code]


# --- settings, always run ---------------------------------------------------------------------


def test_s3_settings_never_show_credentials() -> None:
    settings = S3Settings("eu-west-1", "https://s3.example", "AKIA-SECRET-ID", "very-secret")
    store = S3ObjectStore(settings, "fleet-packages", "packages/x")
    assert "SECRET" not in repr(settings) and "secret" not in repr(settings)
    assert store.describe() == "s3://fleet-packages/packages/x at https://s3.example"
    location = store.location("series/a.parquet")
    assert location.url == "s3://fleet-packages/packages/x/series/a.parquet"
    assert (location.bucket, location.path) == (
        "fleet-packages",
        "fleet-packages/packages/x/series/a.parquet",
    )


@pytest.mark.parametrize(
    ("make", "message"),
    [
        (lambda: S3Settings(""), "region"),
        (lambda: S3Settings("r", "ftp://x"), "http"),
        (lambda: S3Settings("r", "127.0.0.1:9000"), "http"),
        (lambda: S3Settings("r", "http://127.0.0.1:9000"), "allow_http"),
        (lambda: S3ObjectStore(S3Settings("r"), "Bad_Bucket"), "bucket"),
        (lambda: S3ObjectStore(S3Settings("r"), "a..b"), "bucket"),
        (lambda: S3ObjectStore(S3Settings("r"), "bucket", "../up"), "key"),
    ],
)
def test_s3_settings_and_stores_outside_the_contract_are_refused(make: Any, message: str) -> None:
    with pytest.raises(StoreError, match=message):
        make()
