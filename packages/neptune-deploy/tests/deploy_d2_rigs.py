"""One rig per D2 connector: its fake, how to build it, how its remote changes (MVL-158).

The gate (``test_deploy_d2_gate.py``) runs the same checks over every rig. A rig knows only what
differs between connectors: the fake that speaks the system's wire format, the factory call, one
edit the remote makes between two syncs, the requests the system's read-only surface allows, the
credentials it is given and where on the wire they may travel, and the option names for a page
size, a listing budget and a timeout. Networked rigs serve their fake behind the gate's hostile
proxy (``deploy_d2_proxy``); Open-RMF reads a local directory.

Run as a script, it prints one digest per rig of everything a clean sync emits; the gate runs it
under several ``PYTHONHASHSEED`` values and compares.
"""

import copy
import re
import shutil
import sys
import tempfile
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import Any, ClassVar

from deploy_d2_proxy import HostileProxy, Seen
from deploy_d2_support import fingerprint, ledger_json, spellings
from deploy_formant_fake import FakeFormant
from deploy_formant_fake import serve as serve_formant
from deploy_foxglove_fake import API_KEY as FOXGLOVE_KEY
from deploy_foxglove_fake import STREAM, FakeFoxglove
from deploy_object_store_fake import FakeStore
from deploy_records_fake import (
    ConfluenceBackend,
    DriveBackend,
    FakeServer,
    JiraBackend,
    RestBackend,
    ServiceNowBackend,
)
from deploy_records_fake_graph import GraphBackend, LinearBackend
from deploy_records_support import cmms_profile
from deploy_roboto_fake import DATASET, ORG, FakeRoboto
from deploy_roboto_fake import TOKEN as ROBOTO_TOKEN
from neptune.identity.revisions import SourceLedger
from neptune.store.workspace import Workspace
from neptune_deploy.sources.fleet_ops import formant_source, open_rmf_source
from neptune_deploy.sources.foxglove import foxglove_source
from neptune_deploy.sources.object_store import azure_source, gcs_source, s3_source
from neptune_deploy.sources.records import (
    confluence_source,
    gdrive_source,
    jira_source,
    linear_source,
    onedrive_source,
    rest_source,
    servicenow_source,
)
from neptune_deploy.sources.rerun import rerun_source
from neptune_deploy.sources.roboto import roboto_source

TESTS = Path(__file__).parent
S3_KEY_ID, S3_SECRET = "AKIDGATEEXAMPLE", "s3-gate-secret-never-printed"
GCS_TOKEN = "ya29.gate-token-never-printed"
SAS_SIG = "Z2F0ZS1zaWduYXR1cmU+eA=="
SAS = f"sv=2021-08-06&ss=b&srt=co&sp=rl&se=2030-01-01T00:00:00Z&sig={SAS_SIG}"
# A byte read: a ranged GET, a media download, an attachment or a signed link.
READ_PATH = re.compile(
    r"/attachment/content/|/attachment/[^/]+/file$|/content$|/download$|^/dl/|^/blob/|^/content/"
)


def is_read(seen: Seen) -> bool:
    return "range" in seen.headers or "alt=media" in seen.query or bool(READ_PATH.search(seen.path))


def online(home: Path) -> Workspace:
    workspace = Workspace(home)
    workspace.allow_network(True)
    return workspace


class Rig(ABC):
    """One connector under the gate. ``connector`` is its entry point and connector id."""

    connector: ClassVar[str]
    networked: ClassVar[bool] = True
    # Strings that must never appear in anything the source emits, and the one that is sent on
    # the wire (the positive control: the gate's scan is not vacuous).
    secrets: ClassVar[tuple[str, ...]] = ()
    sent_secret: ClassVar[str | None] = None
    basic_user: ClassVar[str | None] = None
    repage: ClassVar[dict[str, Any]] = {"page_size": 1}
    budget: ClassVar[dict[str, Any]] = {}  # options under which the clean listing is over budget
    limit_code: ClassVar[str] = "listing_limit"
    base_path: ClassVar[str] = ""
    has_reads: ClassVar[bool] = True  # bytes are fetched by a request of their own
    changed: ClassVar[frozenset[str]]  # object ids the remote's edit changes
    read_only_rule: ClassVar[str] = "GET only"

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.proxy: HostileProxy | None = None
        self._homes = 0

    # --- What differs per connector ----------------------------------------------------------

    @abstractmethod
    def upstream(self) -> AbstractContextManager[str]:
        """Serve the fake; yield ``http://127.0.0.1:<port>``."""

    @abstractmethod
    def make(self, endpoint: str, authority: str, network: Any, ledger: Any, opts: Any) -> Any:
        """The factory call against the proxy's ``endpoint`` (or ``authority``, for a URL)."""

    @abstractmethod
    def change(self) -> None:
        """The remote edits one object (a re-upload, a re-import, an edited ticket)."""

    def allowed(self, seen: Seen) -> bool:
        """Whether ``seen`` is on the system's read-only surface."""
        return seen.method == "GET" and not seen.body

    def carries(self, seen: Seen) -> bool:
        """Whether ``seen`` may carry the credential: an API request, never a signed link."""
        return not seen.path.startswith(("/blob/", "/content/", "/dl/"))

    def timeout(self, seconds: float) -> dict[str, Any]:
        return {"timeout": seconds}

    # --- Shared --------------------------------------------------------------------------------

    def home(self) -> Path:
        self._homes += 1
        return self.tmp / f"home-{self._homes}"

    def network(self) -> Workspace:
        return online(self.home())

    @contextmanager
    def running(self, attack: Callable[[Seen], str | None] | None = None) -> Iterator[HostileProxy]:
        with self.upstream() as upstream:
            proxy = HostileProxy(re.sub(r"^(http://127\.0\.0\.1:\d+).*$", r"\1", upstream))
            if attack is not None:
                proxy.attack = attack
            with proxy.serve():
                self.proxy = proxy
                try:
                    yield proxy
                finally:
                    self.proxy = None

    def source(
        self, network: Any = None, ledger: SourceLedger | None = None, **options: Any
    ) -> Any:
        assert self.proxy is not None, "build a networked source inside running()"
        return self.make(
            self.proxy.url + self.base_path,
            self.proxy.authority,
            self.network() if network is None else network,
            ledger,
            options,
        )

    def leak_spellings(self) -> set[str]:
        found: set[str] = set()
        for secret in self.secrets:
            found |= spellings(secret, user=self.basic_user)
        return found


# --- Object stores (ADR 0006) ---------------------------------------------------------------------


class _Store(Rig):
    provider: ClassVar[str]
    budget: ClassVar[dict[str, Any]] = {"max_objects": 2}

    def __init__(self, tmp: Path) -> None:
        super().__init__(tmp)
        self.fake = FakeStore(provider=self.provider, bucket="fleet-logs")
        self.fake.put("arm-cell/joint_states.mcap", b"\x89MCAP0\r\n arm cell take 1")
        self.fake.put("amr-07/route.geojson", b'{"type": "FeatureCollection"}')
        self.fake.put("legged/patrol.bag", b"#ROSBAG V2.0 legged patrol")
        self.fake.put("marine/thrusters.csv", b"t,port,starboard\n0,0.1,0.1\n")

    def upstream(self) -> AbstractContextManager[str]:
        return self.fake.serve()

    def change(self) -> None:
        self.fake.put("arm-cell/joint_states.mcap", b"\x89MCAP0\r\n arm cell take 2, re-exported")


class S3Rig(_Store):
    connector = "deploy_s3"
    provider = "s3"
    secrets = (S3_KEY_ID, S3_SECRET)
    sent_secret = S3_KEY_ID  # the key id is in every signature; the secret key is never sent
    changed = frozenset({"site-a:fleet-logs/arm-cell/joint_states.mcap"})

    def make(self, endpoint: str, authority: str, network: Any, ledger: Any, opts: Any) -> Any:
        return s3_source(
            "s3://fleet-logs/",
            network=network,
            ledger=ledger,
            options={"endpoint": endpoint, "store": "site-a", **opts},
            credentials={"s3_access_key_id": S3_KEY_ID, "s3_secret_access_key": S3_SECRET},
        )


class GcsRig(_Store):
    connector = "deploy_gcs"
    provider = "gcs"
    secrets = (GCS_TOKEN,)
    sent_secret = GCS_TOKEN
    changed = frozenset({"site-a:fleet-logs/arm-cell/joint_states.mcap"})

    def make(self, endpoint: str, authority: str, network: Any, ledger: Any, opts: Any) -> Any:
        return gcs_source(
            "gs://fleet-logs/",
            network=network,
            ledger=ledger,
            options={"endpoint": endpoint, "store": "site-a", **opts},
            credentials={"gcs_access_token": GCS_TOKEN},
        )


class AzureRig(_Store):
    connector = "deploy_azure_blob"
    provider = "azure"
    secrets = (SAS_SIG,)
    sent_secret = SAS_SIG
    base_path = "/devstoreaccount1"
    changed = frozenset({"site-a:devstoreaccount1/fleet-logs/arm-cell/joint_states.mcap"})

    def carries(self, seen: Seen) -> bool:
        return True  # a SAS token is a query parameter of every request, by design

    def make(self, endpoint: str, authority: str, network: Any, ledger: Any, opts: Any) -> Any:
        return azure_source(
            "az://devstoreaccount1/fleet-logs/",
            network=network,
            ledger=ledger,
            options={"endpoint": endpoint, "store": "site-a", **opts},
            credentials={"azure_sas_token": SAS},
        )


# --- Foxglove (ADR 0007) ----------------------------------------------------------------------


class FoxgloveRig(Rig):
    connector = "deploy_foxglove"
    secrets = (FOXGLOVE_KEY,)
    sent_secret = FOXGLOVE_KEY
    base_path = "/v1"
    budget: ClassVar[dict[str, Any]] = {"max_recordings": 1}
    changed = frozenset({"fixture:recording/rec_arm_cell_0001"})
    read_only_rule = "GET of the index, devices and topics; POST /v1/data/stream (a link); GET link"

    def __init__(self, tmp: Path) -> None:
        super().__init__(tmp)
        self.fake = FakeFoxglove()

    def upstream(self) -> AbstractContextManager[str]:
        return self.fake.serve()

    def make(self, endpoint: str, authority: str, network: Any, ledger: Any, opts: Any) -> Any:
        return foxglove_source(
            "foxglove://prj_plant_a",
            network=network,
            ledger=ledger,
            options={"endpoint": endpoint, "store": "fixture", **opts},
            credentials={"foxglove_api_key": FOXGLOVE_KEY},
        )

    def change(self) -> None:
        recording = next(r for r in self.fake.recordings if r["id"] == "rec_arm_cell_0001")
        recording["importedAt"] = "2026-10-01T00:00:00Z"
        self.fake.streams["rec_arm_cell_0001"] = STREAM + b"re-imported"

    def allowed(self, seen: Seen) -> bool:
        if seen.method == "POST":
            return seen.path == "/v1/data/stream"
        api = seen.path in ("/v1/recordings", "/v1/devices", "/v1/data/topics")
        return (
            seen.method == "GET"
            and not seen.body
            and (api or seen.path.startswith(("/v1/recordings/", "/blob/")))
        )


# --- Roboto and Rerun (ADR 0009) --------------------------------------------------------------


class RobotoRig(Rig):
    connector = "deploy_roboto"
    secrets = (ROBOTO_TOKEN,)
    sent_secret = ROBOTO_TOKEN
    budget: ClassVar[dict[str, Any]] = {"max_objects": 1}
    changed = frozenset({f"{ORG}/{DATASET}/amr07/calibration.yaml"})
    read_only_rule = "GET; POST of the dataset files query only"

    def __init__(self, tmp: Path) -> None:
        super().__init__(tmp)
        self.fake = FakeRoboto()

    def upstream(self) -> AbstractContextManager[str]:
        return self.fake.serve()

    def make(self, endpoint: str, authority: str, network: Any, ledger: Any, opts: Any) -> Any:
        return roboto_source(
            f"roboto://{ORG}/{DATASET}/",
            network=network,
            ledger=ledger,
            options={"endpoint": endpoint, **opts},
            credentials={"roboto_api_token": ROBOTO_TOKEN},
        )

    def change(self) -> None:
        self.fake.reupload("amr07/calibration.yaml", b"camera: {fx: 612.4, fy: 611.9}\n")

    def allowed(self, seen: Seen) -> bool:
        if seen.method == "POST":
            return seen.path == f"/v1/datasets/{DATASET}/files/query"
        return seen.method == "GET" and not seen.body


class RerunRig(Rig):
    connector = "deploy_rerun"
    secrets = (S3_KEY_ID, S3_SECRET)
    sent_secret = S3_KEY_ID
    budget: ClassVar[dict[str, Any]] = {"max_objects": 1}
    changed = frozenset({"hub-site:fleet-rrd/episodes/legged01_slip.rrd"})

    def __init__(self, tmp: Path) -> None:
        super().__init__(tmp)
        self.fake = FakeStore(bucket="fleet-rrd")
        sizes = {
            "episodes/amr07_aisle12.rrd": 260,
            "episodes/arm3_pick_0914.rrd": 200,
            "episodes/arm3_pick_0914.annotations.rrd": 140,
            "episodes/legged01_slip.rrd": 180,
        }
        for key, size in sizes.items():
            seed = sum(key.encode())
            self.fake.put(key, (b"RRF2" + bytes((seed + 5 * i) % 251 for i in range(size)))[:size])
        self.export = tmp / "rerun" / "catalog.json"
        self.export.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(
            TESTS / "fixtures" / "connectors" / "rerun" / "catalog_export.json", self.export
        )

    repage: ClassVar[dict[str, Any]] = {}

    def upstream(self) -> AbstractContextManager[str]:
        return self.fake.serve()

    def timeout(self, seconds: float) -> dict[str, Any]:
        return {"storage_timeout": seconds}

    def make(self, endpoint: str, authority: str, network: Any, ledger: Any, opts: Any) -> Any:
        opts = dict(opts)
        s3 = {"endpoint": endpoint, "store": "hub-site"}
        if "storage_timeout" in opts:
            s3["timeout"] = opts.pop("storage_timeout")
        return rerun_source(
            str(self.export),
            network=network,
            ledger=ledger,
            options={"storage": {"s3": s3}, **opts},
            credentials={"s3_access_key_id": S3_KEY_ID, "s3_secret_access_key": S3_SECRET},
        )

    def change(self) -> None:
        old = self.fake.latest(b"episodes/legged01_slip.rrd")
        assert old is not None
        self.fake.put("episodes/legged01_slip.rrd", bytes(reversed(old.data)))


# --- Record systems (ADR 0008) ----------------------------------------------------------------


class _Records(Rig):
    budget: ClassVar[dict[str, Any]] = {"max_records": 1}
    backend_type: ClassVar[type]
    credentials: ClassVar[dict[str, str]]

    def __init__(self, tmp: Path) -> None:
        super().__init__(tmp)
        self.backend = self.backend_type()
        self.server = FakeServer(self.backend)

    @contextmanager
    def upstream(self) -> Iterator[str]:
        with self.server.serve() as host:
            yield f"http://{host}"

    def _opts(self, **opts: Any) -> dict[str, Any]:
        return {"scheme": "http", "instance": "site-a", **opts}


class JiraRig(_Records):
    connector = "deploy_jira"
    backend_type = JiraBackend
    secrets = ("jira-secret",)
    sent_secret = "jira-secret"
    basic_user = "ops@example.com"
    changed = frozenset({"@site-a/OPS/issue/10002"})

    def make(self, endpoint: str, authority: str, network: Any, ledger: Any, opts: Any) -> Any:
        return jira_source(
            f"jira://{authority}/OPS",
            network=network,
            ledger=ledger,
            options=self._opts(**opts),
            credentials={"email": "ops@example.com", "api_token": "jira-secret"},
        )

    def change(self) -> None:
        self.backend.edit("OPS-2", "2026-08-21T07:00:00.000+0200", summary="Re-teach pick frame")


class ServiceNowRig(_Records):
    connector = "deploy_servicenow"
    backend_type = ServiceNowBackend
    secrets = ("sn-secret",)
    sent_secret = "sn-secret"
    basic_user = "integration"
    changed = frozenset(
        {"@site-a/change_request/table/change_request/2d852ce80c3433118629589e94784bf4"}
    )

    def make(self, endpoint: str, authority: str, network: Any, ledger: Any, opts: Any) -> Any:
        return servicenow_source(
            f"servicenow://{authority}/change_request",
            network=network,
            ledger=ledger,
            options=self._opts(**opts),
            credentials={"username": "integration", "password": "sn-secret"},
        )

    def change(self) -> None:
        self.backend.edit("CHG0030002", "2026-07-05 11:00:00", u_after="max_speed 0.8")


class LinearRig(_Records):
    connector = "deploy_linear"
    backend_type = LinearBackend
    secrets = ("lin_api_key-never-printed",)
    sent_secret = "lin_api_key-never-printed"
    has_reads = False
    changed = frozenset({"@acme-robotics/OPS/issue/5b1f0c1e-62a4-4c7e-9a11-0d6c3b7a0003"})
    read_only_rule = "POST /graphql of query documents only"

    def make(self, endpoint: str, authority: str, network: Any, ledger: Any, opts: Any) -> Any:
        return linear_source(
            "linear://acme-robotics/OPS",
            network=network,
            ledger=ledger,
            options={"scheme": "http", "endpoint": endpoint, **opts},
            credentials={"api_key": "lin_api_key-never-printed"},
        )

    def change(self) -> None:
        self.backend.edit("OPS-14", "2026-03-07T11:00:00.000Z", title="Recalibrate thrusters v2")

    def allowed(self, seen: Seen) -> bool:
        text = seen.body.decode("utf-8", "replace")
        return (
            seen.method == "POST"
            and seen.path == "/graphql"
            and '"query"' in text
            and "mutation" not in text
            and "subscription" not in text
        )


class DriveRig(_Records):
    connector = "deploy_gdrive"
    backend_type = DriveBackend
    secrets = ("drive-token-never-printed",)
    sent_secret = "drive-token-never-printed"
    changed = frozenset({"@site-a/my-drive/file/file_sop_dock"})

    def make(self, endpoint: str, authority: str, network: Any, ledger: Any, opts: Any) -> Any:
        return gdrive_source(
            "gdrive://my-drive",
            network=network,
            ledger=ledger,
            options=self._opts(endpoint=endpoint, **opts),
            credentials={"access_token": "drive-token-never-printed"},
        )

    def change(self) -> None:
        self.backend.edit("file_sop_dock", b"%PDF-1.4 SOP dock charging v8")


class OneDriveRig(_Records):
    connector = "deploy_onedrive"
    backend_type = GraphBackend
    secrets = ("onedrive-token-never-printed",)
    sent_secret = "onedrive-token-never-printed"
    changed = frozenset({"@site-a/b!siteA_lib01/item/01SOPDOCK00000001"})

    def make(self, endpoint: str, authority: str, network: Any, ledger: Any, opts: Any) -> Any:
        return onedrive_source(
            "onedrive://b!siteA_lib01",
            network=network,
            ledger=ledger,
            options=self._opts(endpoint=endpoint, download_hosts=["127.0.0.1"], **opts),
            credentials={"access_token": "onedrive-token-never-printed"},
        )

    def change(self) -> None:
        self.backend.edit("01SOPDOCK00000001", b"%PDF-1.4 SOP dock charging v8")


class ConfluenceRig(_Records):
    connector = "deploy_confluence"
    backend_type = ConfluenceBackend
    secrets = ("wiki-secret",)
    sent_secret = "wiki-secret"
    basic_user = "wiki@example.com"
    has_reads = False
    changed = frozenset({"@site-a/5001/page/9003"})

    def make(self, endpoint: str, authority: str, network: Any, ledger: Any, opts: Any) -> Any:
        return confluence_source(
            f"confluence://{authority}/5001",
            network=network,
            ledger=ledger,
            options=self._opts(**opts),
            credentials={"email": "wiki@example.com", "api_token": "wiki-secret"},
        )

    def change(self) -> None:
        self.backend.edit("9003", "<ul><li>Check thruster current</li><li>Tide</li></ul>")


class RestRig(_Records):
    connector = "deploy_rest"
    backend_type = RestBackend
    secrets = ("cmms-session-token-never-printed",)
    sent_secret = "cmms-session-token-never-printed"
    changed = frozenset({"@site-a/cmms-example/record/502"})

    def make(self, endpoint: str, authority: str, network: Any, ledger: Any, opts: Any) -> Any:
        return rest_source(
            f"rest://{authority}",
            network=network,
            ledger=ledger,
            options=self._opts(profile=cmms_profile(), **opts),
            credentials={"api_key": "cmms-session-token-never-printed"},
        )

    def change(self) -> None:
        self.backend.edit(502, "2026-06-25T08:00:00Z", problem="Left drive wheel vibration")


# --- Fleet ops (ADR 0010) ---------------------------------------------------------------------


class FormantRig(Rig):
    connector = "deploy_formant"
    secrets = ("test-token-123",)
    sent_secret = "test-token-123"
    budget: ClassVar[dict[str, Any]] = {"max_records": 1}
    limit_code = "part_limit"
    has_reads = False
    changed = frozenset({"@acme-test/org-acme/interventions"})
    read_only_rule = "POST /v1/admin/<part>/query for the five parts only"
    ROUTES = frozenset(
        f"/v1/admin/{route}/query"
        for route in ("devices", "events", "annotations", "intervention-requests", "files")
    )

    def __init__(self, tmp: Path) -> None:
        super().__init__(tmp)
        self.fake = FakeFormant.standard()

    def upstream(self) -> AbstractContextManager[str]:
        return serve_formant(self.fake)

    def make(self, endpoint: str, authority: str, network: Any, ledger: Any, opts: Any) -> Any:
        return formant_source(
            "formant://org-acme",
            network=network,
            ledger=ledger,
            options={"endpoint": endpoint, "instance": "@acme-test", **opts},
            credentials={"formant_access_token": "test-token-123"},
        )

    def change(self) -> None:
        page = self.fake.pages["interventions"][0]
        item = page["items"][0]
        item["message"] = str(item.get("message", "")) + " (edited by the operator)"

    def allowed(self, seen: Seen) -> bool:
        return seen.method == "POST" and seen.path in self.ROUTES


class OpenRmfRig(Rig):
    connector = "deploy_open_rmf"
    networked = False
    has_reads = False
    budget: ClassVar[dict[str, Any]] = {}
    repage: ClassVar[dict[str, Any]] = {}
    changed = frozenset({"warehouse-1/tasks"})
    read_only_rule = "local files, opened read-only; SQLite through mode=ro and an authoriser"
    FILES: ClassVar[dict[str, Any]] = {
        "tasks": {"file": "tasks.json"},
        "fleet_states": {"file": "fleet_states.json"},
        "dispatches": {"file": "dispatches.json"},
        "map": {"file": "nav_graph.json"},
    }

    def __init__(self, tmp: Path) -> None:
        super().__init__(tmp)
        self.root = tmp / "rmf"
        shutil.copytree(TESTS / "fixtures" / "fleet_ops" / "rmf", self.root)

    @contextmanager
    def upstream(self) -> Iterator[str]:
        yield ""

    @contextmanager
    def running(self, attack: Callable[[Seen], str | None] | None = None) -> Iterator[HostileProxy]:
        yield HostileProxy("")

    def source(
        self, network: Any = None, ledger: SourceLedger | None = None, **options: Any
    ) -> Any:
        return self.make("", "", network, ledger, options)

    def make(self, endpoint: str, authority: str, network: Any, ledger: Any, opts: Any) -> Any:
        return open_rmf_source(
            self.root,
            network=network,
            ledger=ledger,
            options={"site": "warehouse-1", "files": copy.deepcopy(self.FILES), **opts},
        )

    def change(self) -> None:
        path = self.root / "tasks.json"
        path.write_bytes(path.read_bytes().replace(b"completed", b"failed", 1))


RIGS: dict[str, type[Rig]] = {
    rig.connector: rig
    for rig in (
        S3Rig,
        GcsRig,
        AzureRig,
        FoxgloveRig,
        RobotoRig,
        RerunRig,
        JiraRig,
        ServiceNowRig,
        LinearRig,
        DriveRig,
        OneDriveRig,
        ConfluenceRig,
        RestRig,
        FormantRig,
        OpenRmfRig,
    )
}
NETWORKED = tuple(name for name, rig in RIGS.items() if rig.networked)


def clean_sync(name: str, tmp: Path) -> tuple[str, str]:
    """One clean sync of ``name``: everything it emits, and the ledger it leaves."""
    from deploy_d2_support import emitted

    rig = RIGS[name](tmp)
    with rig.running():
        source = rig.source()
        ledger = SourceLedger()
        walked = list(source.walk())
        fingerprint(source, ledger)
        text = emitted(source, walked=walked)
        if hasattr(source, "close"):
            source.close()
    return text, ledger_json(ledger)


def main() -> None:
    import hashlib

    for name in sorted(RIGS):
        with tempfile.TemporaryDirectory() as tmp:
            text, ledger = clean_sync(name, Path(tmp))
        digest = hashlib.sha256((text + "\n" + ledger).encode()).hexdigest()
        sys.stdout.write(f"{name} {digest}\n")


if __name__ == "__main__":
    main()
