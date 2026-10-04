"""The D2 guarantees against a live tenant, one connector at a time, when one is named (MVL-158).

CI has no tenants and no credentials, so every test here is skipped unless its connector is named:

    NEPTUNE_TEST_LIVE_<CONNECTOR>_URL       the factory URL, e.g. foxglove://prj_123 or jira://acme.atlassian.net/OPS
    NEPTUNE_TEST_LIVE_<CONNECTOR>_OPTIONS   optional declared options, as JSON

``<CONNECTOR>`` is the entry point name in capitals (``DEPLOY_FOXGLOVE``, ``DEPLOY_JIRA``, ...).
Credentials are the connector's own ``NEPTUNE_*`` variables (ADR 0006 §6, 0007 §7, 0008 §6, 0009 §3,
0010 §2), read by the connector, never by this test; issue them read-only. For example:

    NEPTUNE_FOXGLOVE_API_KEY=... NEPTUNE_TEST_LIVE_DEPLOY_FOXGLOVE_URL=foxglove://- \\
        uv run --all-packages --all-groups pytest tests/test_deploy_d2_live.py -k foxglove

The connector is loaded through its ``neptune.sources`` entry point, as the compiler will load it.
On a live tenant the gate can show: two syncs give the same identities and the same output; a
ledger of the first three objects read finds them unchanged in the second (on a quiet tenant);
``http.client`` sent only the methods and routes of the connector's read-only surface; local-only
refuses it; no ``NEPTUNE_*`` secret is in anything it emits; and the first three objects are
readable whole, which is where Foxglove's ranged stream and Graph's download redirect are
verified. The report (``docs/reviews/d2-stress-test.md``) lists what each connector still needs.
"""

import json
import os
import re
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

import pytest

from deploy_d2_support import (
    Wire,
    emitted,
    entries,
    fingerprint,
    ledger_json,
    location_of,
    no_sockets,
    spellings,
)
from neptune.identity.hashing import digest_stream
from neptune.identity.revisions import SourceLedger
from neptune.store.workspace import LocalOnlyError, Workspace

CONNECTORS = sorted(ep.name for ep in entry_points(group="neptune.sources"))
# The one non-GET request each connector may send, by path (ADR 0007 §7, 0008 §6, 0009 §2, 0010 §2).
POSTS = {
    "deploy_foxglove": re.compile(r"/data/stream$"),
    "deploy_roboto": re.compile(r"/v1/datasets/[^/]+/files/query$"),
    "deploy_linear": re.compile(r"/graphql$"),
    "deploy_formant": re.compile(
        r"/v1/admin/(devices|events|annotations|intervention-requests|files)/query$"
    ),
}
SAMPLE = 3  # objects read whole on a live tenant
SECRET_NAME = re.compile(r"^NEPTUNE_(?!TEST_).*(TOKEN|KEY|SECRET|PASSWORD|SAS)")


def _named(connector: str) -> tuple[str, dict[str, Any]] | None:
    url = os.environ.get(f"NEPTUNE_TEST_LIVE_{connector.upper()}_URL")
    if not url:
        return None
    options = json.loads(os.environ.get(f"NEPTUNE_TEST_LIVE_{connector.upper()}_OPTIONS", "{}"))
    return url, options


def _factory(connector: str) -> Any:
    (found,) = [ep for ep in entry_points(group="neptune.sources") if ep.name == connector]
    return found.load()


def _secrets() -> set[str]:
    """Every ``NEPTUNE_*`` secret as it could leak: verbatim, percent-encoded, and paired with
    each ``*_EMAIL`` or ``*_USERNAME`` as an HTTP Basic credential's base64."""
    users = [
        v for k, v in os.environ.items() if re.match(r"^NEPTUNE_(?!TEST_).*_(EMAIL|USERNAME)$", k)
    ]
    found: set[str] = set()
    for name, value in os.environ.items():
        if SECRET_NAME.match(name) and len(value) >= 8:
            found |= spellings(value)
            for user in users:
                found |= spellings(value, user=user)
    return found


@pytest.mark.parametrize("connector", CONNECTORS)
def test_live_tenant(connector: str, tmp_path: Path) -> None:
    named = _named(connector)
    if named is None:
        pytest.skip(f"no live tenant: set NEPTUNE_TEST_LIVE_{connector.upper()}_URL to run")
    url, options = named
    factory = _factory(connector)
    local = connector == "deploy_open_rmf"

    def build(home: str, ledger: SourceLedger | None = None, online: bool = True) -> Any:
        workspace = Workspace(tmp_path / home)
        workspace.allow_network(online)
        return factory(url, network=workspace, ledger=ledger, options=options)

    wire = Wire()
    with wire.recording():
        if local:
            with no_sockets():
                first, second = build("a"), build("b")
                ledger = SourceLedger()
                fingerprint(first, ledger)
                texts = [emitted(first), emitted(second)]
        else:
            first = build("a")
            listed = entries(first.walk())[:SAMPLE]  # a live tenant is never downloaded whole
            ledger = SourceLedger()
            for entry in listed:
                with first.open(entry.location) as stream:
                    ledger.observe(entry.location, digest_stream(stream, chunk_size=1 << 20))
            texts = [emitted(first), emitted(build("b"))]
            discovery = build("c", ledger).discover(ledger)
            unchanged = {location_of(item) for item in discovery.unchanged}
            assert {e.location for e in listed} <= unchanged, "the tenant changed during the run"
            with pytest.raises(LocalOnlyError):
                build("d", online=False)
    assert texts[0] == texts[1]  # identity and output survive a re-sync, byte for byte
    for text in [*texts, ledger_json(ledger)]:
        assert not [s for s in _secrets() if s in text]
    if local:
        assert wire.sent == []
        return
    for sent in wire.sent:
        path = sent.target.partition("?")[0]
        allowed = sent.method == "GET" or (
            sent.method == "POST" and connector in POSTS and bool(POSTS[connector].search(path))
        )
        assert allowed, (sent.method, path)
    if connector == "deploy_formant":
        assert {s.method for s in wire.sent} == {"POST"}
