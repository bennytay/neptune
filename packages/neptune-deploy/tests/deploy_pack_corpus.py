"""Generator of the Demo v1 test double (``fixtures/packs/acceptance_corpus.graph.json``,
ADR 0014): not a Memory snapshot.

A hand-written graph document about the acceptance corpus (Platform ADR 0007, ``harness/acceptance``
version 1.0.0): PLANT-2's manipulator cell (ARM-3A, incident INC-C3-0011) and legged inspection robot
(LEG-01), and S-007's lift AMR (AMR-07, incident INC-0007). It is **hand-written in the shape of
graph-schema**, not computed: Memory cannot build it from the corpus yet, because

- the event index (MVL-134, PR #125, graph-schema 1.6.0) and calibration drift (MVL-128, PR #129,
  1.7.0) are not merged, and identity does not yet link two records of one incident (Memory ADR
  0013, Revisit);
- the corpus's lifecycle exports declare no configuration ids except the commissioning baseline
  (``cfg-c3-1.4``) and the managed export's revision (``1.5``). The configuration chains here
  follow the corpus's gold answers (``harness/acceptance/gold.json`` Q2, Q3), and stay Unknown
  where the gold answer says no record states one (WO-26-0911 onwards);
- the compiler does not decode bag payloads, so nothing from a bag's ``/diagnostics`` is here.

Each source a claim cites is a corpus file, by its content id in ``corpus.lock.json`` 1.0.0
(``CORPUS``, checked by ``test_deploy_packs_corpus``), with a row (header row 0), a page (from 0)
or a JSON pointer. Where the corpus has no record of something a test needs (the S-007 incident's
CMMS row, the fleet manager's syslog, its time-sync statement, an intervention log, the AMR's
controller log, a lidar calibration), the source is **synthetic** (``synthetic(...)``), named for
what it stands for.

Clocks follow Memory: a local wall time with no offset stays on its own clock (Memory ADR 0002
§3); an offset-bearing time is on the civil clock; a bag's log time is its recorder's clock.
Simplification: one clock per export or document (the mapper keys one per column, Deploy ADR 0012
§2, which would split each chain further). The only mappings are the ones a source states:
Memory's inferred estimate of the cell PC against the controller (gold Q4), and the synthetic
statement that S-007's fleet manager syslog runs on the site's CMMS clock.

``python deploy_pack_corpus.py`` rewrites the file; ``test_deploy_packs_corpus`` checks it is
what this module generates.
"""

# ruff: noqa: E501  (corpus paths and content ids read whole)

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from deploy_pack_graphs import EVENT_PREDICATES, FIXTURES, REPO, claim, node, rec, sha, text
from neptune.identity import canonical_json

VOCABULARY_1_4: Final = REPO / "contracts/graph-schema/v1.4.0/golden/vocabulary.json"
CORPUS_VERSION: Final = "1.0.0"
CORPUS: Final = {
    "neptune.yaml": "sha256:1279658641566e0b752c487a4ff6ca85f6895996d94ed6fb9134572011382dba",
    "records/asset_register.csv": "sha256:b24e816567ecca2d15afb5da6c046d7d666538c8327cafd247ed738311869e7d",
    "sites/PLANT-2/cell3/incidents/INC-C3-0011.pdf": "sha256:bfc3c2e8e1d23ee47f2b4c476e6164bf879da8732d3936978b2dbf7e19c97908",
    "sites/PLANT-2/cell3/documents/commissioning_CR-C3-2026-02.pdf": "sha256:5e9fde08afd857fc9696313d71432ff87f2d126a2d5b44a2508806cdf391a9d7",
    "sites/PLANT-2/changes/servicenow_changes.csv": "sha256:fdca333962781db7f725a857dc6647b73e192666d2e9b505a2151e0502f1ef16",
    "sites/PLANT-2/cmms/work_orders.csv": "sha256:f4e37e7d88632fff34f908769d4470480060cfb0e59942e6a24f95a24efdcfb3",
    "sites/PLANT-2/cell3/requalification/requalification_tests.csv": "sha256:30be864d249a145a23a78fc8b38686e09a3ca1d3c8b4c9f08c08feb847a3f2be",
    "sites/PLANT-2/cell3/config/cell_config.yaml": "sha256:57ea0f32041ae257fcb6bfed1c7c5a6738203f6290e7c0beccf3180a2245fb25",
    "sites/PLANT-2/cell3/calibration/CAL-ARM3A-0623.yaml": "sha256:530ef05aaa164117044f3192bcafcb24737c65212fb9771ba8f205894aacdf80",
    "sites/PLANT-2/cell3/calibration/CAL-ARM3A-0818.yaml": "sha256:a2d19ecfbcda50ea0d69888d09d5f529e747ed4eb2434641837f09f657b175e9",
    "sites/PLANT-2/cell3/calibration/CAL-ARM3A-0911.yaml": "sha256:0ac61f856e5f99b1a8e8231bcfcfe5a7ad4497f50da2c4ad8b3a994f14fdbfa4",
    "sites/PLANT-2/cell3/bags/pallet_2026-09-09/metadata.yaml": "sha256:13b72327894552ab66dd421ab7081c4ef2594e7b980ad77d48cd8b902c178c49",
    "sites/PLANT-2/cell3/bags/pallet_2026-09-14/metadata.yaml": "sha256:dc69a522a2f10db3e282bd0b1d0ab047ccf9eeb83c0fb139da2f456e16006c12",
    "sites/PLANT-2/legged/config/2026-09-01/LEG-01_patrol.yaml": "sha256:9813ae367c6be18b197ef4f323c988e1cbaa5bd30279fcfe44b2008b040b6514",
    "sites/PLANT-2/legged/config/2026-09-13/LEG-01_patrol.yaml": "sha256:7e0c0d49f6d992a862132aeaf2302dd976edc5d8b228bf4a88bac613c1115308",
    "sites/PLANT-2/legged/runs/patrol_2026-09-14/metadata.yaml": "sha256:4bb95d42f085cd51e8d8d29557dcaed833b24d977938ac9e07a8032b2c62372a",
    "sites/S-007/cmms/work_orders.csv": "sha256:653d2e3bff6fd54ab9d76e36b7af4bb952d0cdfb70623315309a513c755f232d",
    "sites/S-007/changes/servicenow_changes.csv": "sha256:91d38d927ae0f43c8e0706a41a5da1d39d50fb94be8dac1e87ed9b21e71bff1c",
    "sites/S-007/requalification/requalification_tests.csv": "sha256:e1dd2870994f4adbd7d45e8d4df91961afcb4fa5285e37e27ddad645265e2931",
    "sites/S-007/authorisation/zone_register.csv": "sha256:45788eab57528e0fb36a3e2ae9d72a843252edda2ed63596378bd2e3c2a187a1",
    "sites/S-007/incidents/INC-0007.pdf": "sha256:b793183fb43a23f9bb069be4e3f4c360321da5dc1c60d929312bd8942abbcda8",
    "sites/S-007/runs/amr-07_2026-04-02.mcap": "sha256:34bb6772f2fd57ccededd1a9f6f6e83e5b8c5fefaa389765a28d3c311910bbe8",
    "sites/S-007/runs/amr-07_2026-04-15.mcap": "sha256:e1987a0b1303fe67c473a25d87b363be8de1085f07f51e267e90d311e83202da",
}
SECOND: Final = 10**9
EDT: Final = 4 * 3600 * SECOND  # both sites keep US Eastern daylight time in the storyline


# --- Evidence ------------------------------------------------------------------------------------


def _ref(source: str, *locator: dict[str, Any]) -> dict[str, Any]:
    return {"locator": list(locator), "source": source}


def row(path: str, line: int) -> dict[str, Any]:
    """A corpus CSV row (the header is row 0)."""
    return _ref(CORPUS[path], {"kind": "row", "row": line})


def page(path: str, index: int) -> dict[str, Any]:
    """A corpus document page (from 0)."""
    return _ref(CORPUS[path], {"kind": "page", "index": index})


def pointer(path: str, at: str) -> dict[str, Any]:
    """A value of a corpus YAML or JSON file."""
    return _ref(CORPUS[path], {"kind": "json_pointer", "pointer": at})


def synthetic(name: str, line: int) -> dict[str, Any]:
    """A row of a source the corpus does not hold, standing for ``name``."""
    return _ref(f"sha256:{sha('synthetic source ' + name)}", {"kind": "row", "row": line})


# --- Values and time -----------------------------------------------------------------------------


def wall(y: int, mo: int, d: int, h: int = 0, mi: int = 0, s: int = 0) -> int:
    """Nanosecond ticks of a local wall clock that counts its own civil time (no zone applied)."""
    return int(datetime(y, mo, d, h, mi, s, tzinfo=UTC).timestamp()) * SECOND


def instant(y: int, mo: int, d: int, h: int = 0, mi: int = 0, s: int = 0) -> int:
    """POSIX nanoseconds of an EDT time stated with its offset (-04:00)."""
    return wall(y, mo, d, h, mi, s) + EDT


def days(y: int, mo: int, d: int) -> int:
    return wall(y, mo, d) // (86_400 * SECOND)


def at(clock: str, ticks: int) -> dict[str, Any]:
    return {"domain_id": clock, "ticks": ticks}


def tick(clock: str, ticks: int) -> tuple[dict[str, Any], dict[str, Any]]:
    """An instant as Memory places one: ``[t, t + 1 tick)``."""
    return (at(clock, ticks), at(clock, ticks + 1))


def span(clock: str, start: int, end: int | None) -> tuple[dict[str, Any], dict[str, Any] | str]:
    return (at(clock, start), "open" if end is None else at(clock, end))


def record(name: str) -> dict[str, Any]:
    return {"kind": "record", "record_id": rec(name)}


def clock_map(
    source: str, target: str, method: str, anchor: tuple[int, int] | None, bound: int | None
) -> dict[str, Any]:
    """A ``clock_map`` literal (graph-schema 1.4.0): rate 1, the anchor and bound as stated."""
    return {
        "datatype": "clock_map",
        "kind": "literal",
        "unit": {"knowledge": "not_applicable"},
        "value": {
            "anchor": {"knowledge": "unknown"}
            if anchor is None
            else {
                "knowledge": "known",
                "value": {"source": at(source, anchor[0]), "target": at(target, anchor[1])},
            },
            "chain": [],
            "method": method,
            "rate": {"knowledge": "known", "value": {"denominator": 1, "numerator": 1}},
            "residual_bound": {"knowledge": "unknown"}
            if bound is None
            else {"knowledge": "known", "value": at(target, bound)},
            "target": target,
            "via": [],
        },
    }


def delta(earlier: str, later: str, name: str, values: list[float], unit: str) -> dict[str, Any]:
    """A ``delta`` literal (graph-schema 1.7.0): ``later - earlier`` per component, as declared."""
    return {
        "datatype": "delta",
        "kind": "literal",
        "unit": {"knowledge": "known", "value": unit},
        "value": {
            "earlier": rec(earlier),
            "later": rec(later),
            "name": name,
            "quantity": "parameter",
            "representation": "values",
            "values": values,
        },
    }


# --- Clocks (node ids are the clocks' TimestampDomain record ids) --------------------------------

CIVIL: Final = rec("memory civil clock posix unix ns")
P2_LIFE: Final = rec("domain PLANT-2 lifecycle exports, local wall time")
P2_REPORT: Final = rec("domain INC-C3-0011 report times (cell HMI alarm log)")
P2_ASSETS: Final = rec("domain asset register commissioned dates")
RUN09: Final = rec("domain CELL3-IPC log time, pallet_2026-09-09")
RUN14: Final = rec("domain CELL3-IPC log time, pallet_2026-09-14")
CTRL14: Final = rec("domain ARM-3A controller header stamps, pallet_2026-09-14")
LEG14: Final = rec("domain LEG-01 log time, patrol_2026-09-14")
S7_LIFE: Final = rec("domain S-007 CMMS exports, local wall time")
S7_ENV: Final = rec("domain S-007 zone register dates")
S7_REPORT: Final = rec("domain INC-0007 report times")
S7_SYSLOG: Final = rec("domain S-007 fleet manager syslog")  # synthetic
AMR07_CTRL: Final = rec("domain AMR-07 drive controller boot clock")  # synthetic, unmapped
RUN0402: Final = rec("domain AMR-07 log time, amr-07_2026-04-02")
RUN0415: Final = rec("domain AMR-07 log time, amr-07_2026-04-15")

ARM: Final = node("machine", "asset-tag:ARM-3A")
LEG: Final = node("machine", "asset-tag:LEG-01")
AMR: Final = node("machine", "asset-tag:AMR-07")
PLANT: Final = node("site", "site-code:PLANT-2")
S007: Final = node("site", "site-code:S-007")
CELL3: Final = node("zone", "zone-code:CELL-3")
PICK_A: Final = node("zone", "zone-code:PICK-A")
WCAM: Final = node("sensor", "asset-tag:WCAM-3A")
LIDAR: Final = node("sensor", "serial:SN-L150-0107-LIDAR")


def config(name: str) -> dict[str, Any]:
    return node("configuration", f"cfg:{name}")


def run(name: str) -> dict[str, Any]:
    return node("run", f"record:{rec('run ' + name)}")


def event(name: str) -> dict[str, Any]:
    return node("event", f"record:{rec(name)}")


def entry(incident: str, index: int) -> dict[str, Any]:
    """A timeline entry of an incident record (Memory ADR 0013 §2)."""
    return node("event", f"record:{rec(incident)}/timeline/{index}")


INC_C3: Final = "incident record INC-C3-0011"
INC_0007_REPORT: Final = "incident record INC-0007 (report)"
INC_0007_CMMS: Final = "incident record INC-0007 (CMMS)"
SYSLOG_PSTOP: Final = "syslog line 4182"
SYSLOG_WARN: Final = "syslog line 4179"
SYSLOG_MAP: Final = "clock mapping S-007 syslog -> CMMS clock"
INTERVENTION: Final = "intervention IV-S007-0031"
CONTROLLER_FAULT: Final = "AMR-07 controller log line 88"


# --- Claims --------------------------------------------------------------------------------------

Claims = list[dict[str, Any]]


def facts(
    subject: dict[str, Any],
    valid: tuple[dict[str, Any], dict[str, Any] | str],
    records: tuple[str, ...],
    evidence: dict[str, Any],
    items: list[tuple[str, dict[str, Any]]],
    *,
    consolidator: str = "memory.events",
    recorded_at: int = 4,
) -> Claims:
    return [
        claim(
            subject,
            predicate,
            obj,
            valid,
            records=records,
            evidence=(evidence,),
            consolidator=consolidator,
            recorded_at=recorded_at,
        )
        for predicate, obj in items
    ]


def identity(
    subject: dict[str, Any],
    predicate: str,
    obj: dict[str, Any],
    valid: tuple[dict[str, Any], dict[str, Any] | str],
    records: tuple[str, ...],
    *evidence: dict[str, Any],
) -> dict[str, Any]:
    return claim(
        subject,
        predicate,
        obj,
        valid,
        records=records,
        evidence=evidence,
        consolidator="memory.identity",
        recorded_at=1,
    )


def chain(
    machine: dict[str, Any],
    predicate: str,
    obj: dict[str, Any],
    valid: tuple[dict[str, Any], dict[str, Any] | str],
    records: tuple[str, ...],
    *evidence: dict[str, Any],
    kind: str = "stated",
) -> dict[str, Any]:
    return claim(
        machine,
        predicate,
        obj,
        valid,
        records=records,
        evidence=evidence,
        kind=kind,
        consolidator="memory.configuration",
        recorded_at=2,
    )


def clock_claims(
    machine: dict[str, Any],
    clocks: list[tuple[str, int, int, str, dict[str, Any]]],
) -> Claims:
    """``has_clock`` per clock a run of ``machine`` carries (Memory ADR 0011 §1)."""
    return [
        claim(
            machine,
            "has_clock",
            node("clock", clock),
            span(clock, first, last + 1),
            records=(run_name,),
            evidence=(evidence,),
            kind="observed",
            consolidator="memory.time",
            recorded_at=3,
        )
        for clock, first, last, run_name, evidence in clocks
    ]


def mapping(
    source: str,
    target: str,
    valid: tuple[dict[str, Any], dict[str, Any] | str],
    value: dict[str, Any],
    records: tuple[str, ...],
    evidence: dict[str, Any],
    *,
    estimated: bool,
) -> Claims:
    """A mapping is a ``maps_to`` edge plus a ``clock_map`` literal (Memory ADR 0011 §2)."""
    out = []
    for predicate, obj in (("maps_to", node("clock", target)), ("clock_map", value)):
        made = claim(
            node("clock", source),
            predicate,
            obj,
            valid,
            records=records,
            evidence=(evidence,),
            kind="inferred" if estimated else "stated",
            consolidator="memory.time_estimates" if estimated else "memory.time",
            recorded_at=3,
            model={"model_id": "neptune.clocks", "model_version": "1"} if estimated else None,
            confidence=0.0 if estimated else None,
        )
        if estimated:
            # An estimate states a residual bound, not a probability: confidence is Unknown
            # (Memory ADR 0011 §4). The id hashes the confidence, so re-derive it.
            made = _reconfidence(made, {"knowledge": "unknown"})
        out.append(made)
    return out


def _reconfidence(made: dict[str, Any], confidence: dict[str, Any]) -> dict[str, Any]:
    content = {
        key: made[key]
        for key in (
            "assertion_kind",
            "object",
            "predicate",
            "provenance",
            "subject",
            "valid",
        )
    }
    content["confidence"] = confidence
    digest = sha(canonical_json.dumps({"claim": content, "scheme": "deploy-pack-fixture"}).decode())
    return {**made, "confidence": confidence, "id": f"claim:sha256:{digest}"}


# --- PLANT-2: ARM-3A, LEG-01 and INC-C3-0011 -----------------------------------------------------

CMMS_P2: Final = "sites/PLANT-2/cmms/work_orders.csv"
CHANGES_P2: Final = "sites/PLANT-2/changes/servicenow_changes.csv"
REQUAL_P2: Final = "sites/PLANT-2/cell3/requalification/requalification_tests.csv"
ASSETS: Final = "records/asset_register.csv"
REPORT_P2: Final = "sites/PLANT-2/cell3/incidents/INC-C3-0011.pdf"
COMMISSIONING: Final = "sites/PLANT-2/cell3/documents/commissioning_CR-C3-2026-02.pdf"
MANIFEST: Final = "neptune.yaml"

BAG09_START: Final = 1_788_976_895_600_000_000
BAG14_START: Final = 1_789_410_576_700_000_000
BAG_LENGTH: Final = 309 * SECOND
LEG14_START: Final = 1_789_410_300_000_000_000
LEG14_LENGTH: Final = 838 * SECOND


def plant2_identity() -> Claims:
    return [
        identity(
            ARM,
            "located_at",
            PLANT,
            span(P2_ASSETS, days(2026, 2, 26), None),
            ("asset register ARM-3A",),
            row(ASSETS, 4),
        ),
        identity(
            LEG,
            "located_at",
            PLANT,
            span(P2_ASSETS, days(2026, 5, 12), None),
            ("asset register LEG-01",),
            row(ASSETS, 10),
        ),
        identity(
            WCAM,
            "mounted_on",
            ARM,
            span(P2_ASSETS, days(2026, 2, 26), None),
            ("asset register WCAM-3A",),
            row(ASSETS, 6),
        ),
        identity(
            CELL3,
            "zone_of",
            PLANT,
            span(P2_ASSETS, days(2026, 2, 26), None),
            ("asset register ARM-3A",),
            row(ASSETS, 4),
        ),
    ]


def plant2_configuration() -> Claims:
    c14, c15 = config("cfg-c3-1.4"), config("cfg-c3-1.5")
    commissioned = wall(2026, 2, 26, 16, 30)
    chg12 = wall(2026, 3, 10, 19, 30)
    chg13 = wall(2026, 8, 18, 12, 55)
    wo0911 = wall(2026, 9, 10, 16, 20)
    return [
        # As commissioned (CR-C3-2026-02, "Configuration baseline: cfg-c3-1.4").
        chain(
            ARM,
            "has_configuration",
            c14,
            span(P2_LIFE, commissioned, chg12),
            ("commissioning baseline CR-C3-2026-02",),
            page(COMMISSIONING, 0),
        ),
        # The software change names the machine but no configuration: a gap, never bridged.
        chain(
            ARM,
            "configuration_unknown",
            record("change record CHG0030012"),
            span(P2_LIFE, chg12, chg13),
            ("change record CHG0030012",),
            row(CHANGES_P2, 1),
        ),
        chain(
            ARM,
            "configuration_unknown",
            record("work order WO-26-0310"),
            span(P2_LIFE, chg12, chg13),
            ("work order WO-26-0310",),
            row(CMMS_P2, 1),
        ),
        # The 2026-08-18 finger change, approved (CHG0030013) and requalified (RQ-2026-006).
        chain(
            ARM,
            "has_configuration",
            c15,
            span(P2_LIFE, chg13, wo0911),
            ("change record CHG0030013", "requalification RQ-2026-006"),
            row(CHANGES_P2, 2),
            row(REQUAL_P2, 3),
        ),
        # WO-26-0911 (finger set FS-0340, TCP edit, bracket refit) and WO-26-0912 (hand-eye
        # recalibration) state no configuration; no change record or requalification follows.
        chain(
            ARM,
            "configuration_unknown",
            record("work order WO-26-0911"),
            span(P2_LIFE, wo0911, None),
            ("work order WO-26-0911",),
            row(CMMS_P2, 6),
        ),
        chain(
            ARM,
            "configuration_unknown",
            record("work order WO-26-0912"),
            span(P2_LIFE, wo0911, None),
            ("work order WO-26-0912",),
            row(CMMS_P2, 7),
        ),
    ]


def plant2_runs() -> Claims:
    c15 = config("cfg-c3-1.5")
    managed = pointer("sites/PLANT-2/cell3/config/cell_config.yaml", "/config_revision")
    out: Claims = []
    for name, clock, start, bag in (
        ("cell3-2026-09-09", RUN09, BAG09_START, "pallet_2026-09-09"),
        ("cell3-2026-09-14", RUN14, BAG14_START, "pallet_2026-09-14"),
    ):
        window = span(clock, start, start + BAG_LENGTH)
        meta = pointer(
            f"sites/PLANT-2/cell3/bags/{bag}/metadata.yaml",
            "/rosbag2_bagfile_information/starting_time",
        )
        out += [
            claim(
                run(name),
                "recorded_by",
                ARM,
                window,
                records=(f"run {name}",),
                evidence=(pointer(MANIFEST, "/runs/1" if "09-09" in name else "/runs/2"), meta),
                consolidator="memory.identity",
                recorded_at=3,
            ),
            # The managed export (revision 1.5) is the only configuration bound to either run:
            # stated, and shown as such beside the machine chain's Unknown on another clock.
            claim(
                run(name),
                "configuration_active_during",
                c15,
                window,
                records=(f"run {name}", f"snapshot binding {name}"),
                evidence=(managed,),
                recorded_at=3,
            ),
            # No envelope at PLANT-2 names cfg-c3-1.5, so no part of the window is covered.
            claim(
                run(name),
                "not_covered_by_authorisation",
                c15,
                window,
                kind="observed",
                records=(f"run {name}", f"snapshot binding {name}"),
                evidence=(managed,),
                recorded_at=3,
            ),
        ]
    window = span(LEG14, LEG14_START, LEG14_START + LEG14_LENGTH)
    meta = pointer(
        "sites/PLANT-2/legged/runs/patrol_2026-09-14/metadata.yaml",
        "/rosbag2_bagfile_information/starting_time",
    )
    out.append(
        claim(
            run("leg01-2026-09-14"),
            "recorded_by",
            LEG,
            window,
            records=("run leg01-2026-09-14",),
            evidence=(pointer(MANIFEST, "/runs/4"), meta),
            consolidator="memory.identity",
            recorded_at=3,
        )
    )
    # Two exports of LEG-01's configuration bind the patrol (firmware 3.1.4 and 3.2.0): an
    # Ambiguous link, every reading shown (Memory ADR 0010 §3, binding_overlap).
    for exported, firmware in (("2026-09-01", "3.1.4"), ("2026-09-13", "3.2.0")):
        out.append(
            claim(
                run("leg01-2026-09-14"),
                "configuration_candidate",
                config(f"leg01-patrol-fw-{firmware}"),
                window,
                records=("run leg01-2026-09-14", f"snapshot binding leg01 {exported}"),
                evidence=(
                    pointer(
                        f"sites/PLANT-2/legged/config/{exported}/LEG-01_patrol.yaml", "/firmware"
                    ),
                ),
                recorded_at=3,
            )
        )
    return out


def plant2_calibration() -> Claims:
    out = []
    for earlier, later, start, end, values in (
        (
            "CAL-ARM3A-0623",
            "CAL-ARM3A-0818",
            instant(2026, 6, 23, 17, 30),
            instant(2026, 8, 18, 12, 40),
            [0.0334 - 0.0334, -0.0103 - -0.0102, 0.0745 - 0.0709],
        ),
        (
            "CAL-ARM3A-0818",
            "CAL-ARM3A-0911",
            instant(2026, 8, 18, 12, 40),
            instant(2026, 9, 11, 10, 40),
            [0.0334 - 0.0334, -0.0103 - -0.0103, 0.0702 - 0.0745],
        ),
    ):
        out.append(
            claim(
                WCAM,
                "drift",
                delta(f"calibration {earlier}", f"calibration {later}", "translation", values, "m"),
                span(CIVIL, start, end),
                kind="observed",
                records=(f"calibration {earlier}", f"calibration {later}"),
                evidence=(
                    pointer(f"sites/PLANT-2/cell3/calibration/{earlier}.yaml", "/translation"),
                    pointer(f"sites/PLANT-2/cell3/calibration/{later}.yaml", "/translation"),
                ),
                consolidator="memory.calibration",
                recorded_at=3,
            )
        )
    return out


def plant2_clocks() -> Claims:
    out = clock_claims(
        ARM,
        [
            (
                RUN09,
                BAG09_START,
                BAG09_START + BAG_LENGTH,
                "run cell3-2026-09-09",
                pointer(
                    "sites/PLANT-2/cell3/bags/pallet_2026-09-09/metadata.yaml",
                    "/rosbag2_bagfile_information",
                ),
            ),
            (
                RUN14,
                BAG14_START,
                BAG14_START + BAG_LENGTH,
                "run cell3-2026-09-14",
                pointer(
                    "sites/PLANT-2/cell3/bags/pallet_2026-09-14/metadata.yaml",
                    "/rosbag2_bagfile_information",
                ),
            ),
            (
                CTRL14,
                BAG14_START - 96_700_000_000,
                BAG14_START - 96_700_000_000 + BAG_LENGTH,
                "run cell3-2026-09-14",
                pointer(
                    "sites/PLANT-2/cell3/bags/pallet_2026-09-14/metadata.yaml",
                    "/rosbag2_bagfile_information",
                ),
            ),
        ],
    )
    # Memory's estimate of the cell PC's log time against the controller's header stamps
    # (co-recorded, latency unbounded: gold Q4). Inferred, so left out unless a spec includes it.
    out += mapping(
        RUN14,
        CTRL14,
        span(RUN14, BAG14_START, BAG14_START + BAG_LENGTH),
        clock_map(RUN14, CTRL14, "co_sampled", (BAG14_START, BAG14_START - 96_700_000_000), None),
        ("run cell3-2026-09-14", "derived clock mapping pallet_2026-09-14"),
        pointer(
            "sites/PLANT-2/cell3/bags/pallet_2026-09-14/metadata.yaml",
            "/rosbag2_bagfile_information",
        ),
        estimated=True,
    )
    return out


INC_C3_TIMELINE: Final = (
    ((2026, 9, 14, 14, 28, 0), "Cell restarted after the break; PALLET_C3 started from the HMI"),
    ((2026, 9, 14, 14, 32, 38), "Collision detection on joint 5 at pick P1; protective stop"),
    ((2026, 9, 14, 14, 32, 41), "Operator presses the E-stop at OP-2"),
    ((2026, 9, 14, 14, 33, 30), "Cell supervisor notified; cell locked out"),
    ((2026, 9, 14, 14, 52, 0), "PF-3 locating edge found bent; part 7731-B dropped"),
)
INC_C3_DESCRIPTION: Final = (
    "During the pick at P1 the gripper fingers struck the locating edge of infeed fixture PF-3."
    " Collision detection stopped the arm and the operator pressed the E-stop at OP-2. The part"
    " fell onto the floor guard. Nobody was inside the cell."
)


def plant2_incident() -> Claims:
    """INC-C3-0011 as its report states it: the incident and its five timeline entries, on the
    report's own clock (the HMI alarm log's times as the report writes them)."""
    out = facts(
        event(INC_C3),
        tick(P2_REPORT, wall(2026, 9, 14, 14, 32)),
        (INC_C3,),
        page(REPORT_P2, 0),
        [
            ("event_kind", text("incident")),
            ("stated_severity", text("Property damage, no injury")),
            ("involves", ARM),
            ("at_site", PLANT),
            ("in_zone", CELL3),
            ("evidenced_by", record(INC_C3)),
        ],
    )
    out += facts(
        event(INC_C3),
        tick(P2_REPORT, wall(2026, 9, 14, 14, 32)),
        (INC_C3,),
        page(REPORT_P2, 1),
        [("has_description", text(INC_C3_DESCRIPTION))],
    )
    for index, (when, said) in enumerate(INC_C3_TIMELINE):
        out += facts(
            entry(INC_C3, index),
            tick(P2_REPORT, wall(*when)),
            (INC_C3,),
            page(REPORT_P2, 0),
            [("has_description", text(said)), ("evidenced_by", record(INC_C3))],
        )
    return out


# --- S-007: AMR-07 and INC-0007 ------------------------------------------------------------------

CMMS_S7: Final = "sites/S-007/cmms/work_orders.csv"
CHANGES_S7: Final = "sites/S-007/changes/servicenow_changes.csv"
REQUAL_S7: Final = "sites/S-007/requalification/requalification_tests.csv"
ENVELOPES_S7: Final = "sites/S-007/authorisation/zone_register.csv"
REPORT_S7: Final = "sites/S-007/incidents/INC-0007.pdf"

RUN0402_START: Final = 1_775_138_400_000_000_000
RUN0415_START: Final = 1_776_259_800_000_000_000
AMR_RUN_LENGTH: Final = 600 * SECOND


def s007_identity() -> Claims:
    return [
        identity(
            AMR,
            "located_at",
            S007,
            span(P2_ASSETS, days(2025, 11, 3), None),
            ("asset register AMR-07",),
            row(ASSETS, 3),
        ),
        identity(
            PICK_A,
            "zone_of",
            S007,
            span(S7_ENV, days(2026, 3, 9), None),
            ("authorisation envelope ENV-S007-04",),
            row(ENVELOPES_S7, 2),
        ),
        identity(
            LIDAR,
            "mounted_on",
            AMR,
            span(P2_ASSETS, days(2025, 11, 3), None),
            ("asset register AMR-07",),
            row(ASSETS, 3),
        ),
    ]


def s007_configuration() -> Claims:
    a, b, c, d = (config(f"AMR-07-{x}") for x in "ABCD")
    wo0303 = wall(2026, 3, 3, 8, 30)
    wo0319 = wall(2026, 3, 19, 15, 10)
    wo0402 = wall(2026, 4, 2, 16, 0)
    wo0414 = wall(2026, 4, 14, 19, 30)
    rq = wall(2026, 4, 15, 9, 30)
    return [
        chain(
            AMR,
            "has_configuration",
            a,
            span(S7_LIFE, wo0303, wo0319),
            ("work order WO-26-0303",),
            row(CMMS_S7, 3),
        ),
        chain(
            AMR,
            "has_configuration",
            b,
            span(S7_LIFE, wo0319, wo0402),
            ("work order WO-26-0319",),
            row(CMMS_S7, 4),
        ),
        claim(
            b,
            "succeeds",
            a,
            span(S7_LIFE, wo0319, wo0402),
            records=("work order WO-26-0303", "work order WO-26-0319"),
            evidence=(row(CMMS_S7, 3), row(CMMS_S7, 4)),
            recorded_at=2,
        ),
        # The fork repair after INC-0007 states no resulting configuration: a gap.
        chain(
            AMR,
            "configuration_unknown",
            record("work order WO-26-0402"),
            span(S7_LIFE, wo0402, wo0414),
            ("work order WO-26-0402",),
            row(CMMS_S7, 5),
        ),
        # WO-26-0414 flashed firmware 4.3.1 but does not say which map revision: two readings.
        chain(
            AMR,
            "configuration_candidate",
            c,
            span(S7_LIFE, wo0414, rq),
            ("work order WO-26-0414",),
            row(CMMS_S7, 6),
        ),
        chain(
            AMR,
            "configuration_candidate",
            d,
            span(S7_LIFE, wo0414, rq),
            ("work order WO-26-0414", "change record CHG0050023"),
            row(CMMS_S7, 6),
            row(CHANGES_S7, 3),
        ),
        # As requalified: firmware 4.3.1 and map revision 14 (RQ-S007-0007).
        chain(
            AMR,
            "has_configuration",
            d,
            span(S7_LIFE, rq, None),
            ("requalification RQ-S007-0007",),
            row(REQUAL_S7, 1),
        ),
        claim(
            S007,
            "authorised_configuration",
            a,
            span(S7_ENV, days(2026, 3, 9), days(2026, 9, 9)),
            records=("authorisation envelope ENV-S007-04",),
            evidence=(row(ENVELOPES_S7, 2),),
            recorded_at=2,
        ),
    ]


def s007_runs_and_calibration() -> Claims:
    b, c, d = (config(f"AMR-07-{x}") for x in "BCD")
    w2 = span(RUN0402, RUN0402_START, RUN0402_START + AMR_RUN_LENGTH)
    w15 = span(RUN0415, RUN0415_START, RUN0415_START + AMR_RUN_LENGTH)
    bag2 = _ref(
        CORPUS["sites/S-007/runs/amr-07_2026-04-02.mcap"],
        {"kind": "byte_range", "length": 64, "offset": 0},
    )
    bag15 = _ref(
        CORPUS["sites/S-007/runs/amr-07_2026-04-15.mcap"],
        {"kind": "byte_range", "length": 64, "offset": 0},
    )
    lidar_cal = config("AMR-07-lidar-2026-04-14")
    return [
        claim(
            run("amr07-2026-04-02"),
            "recorded_by",
            AMR,
            w2,
            records=("run amr07-2026-04-02",),
            evidence=(pointer(MANIFEST, "/runs/7"), bag2),
            consolidator="memory.identity",
            recorded_at=3,
        ),
        claim(
            run("amr07-2026-04-02"),
            "configuration_active_during",
            b,
            w2,
            records=("run amr07-2026-04-02", "snapshot binding amr07-2026-04-02"),
            evidence=(bag2,),
            recorded_at=3,
        ),
        claim(
            run("amr07-2026-04-15"),
            "recorded_by",
            AMR,
            w15,
            records=("run amr07-2026-04-15",),
            evidence=(pointer(MANIFEST, "/runs/8"), bag15),
            consolidator="memory.identity",
            recorded_at=3,
        ),
        claim(
            run("amr07-2026-04-15"),
            "configuration_candidate",
            c,
            w15,
            records=("run amr07-2026-04-15", "snapshot binding amr07-2026-04-15 map r13"),
            evidence=(bag15,),
            recorded_at=3,
        ),
        claim(
            run("amr07-2026-04-15"),
            "configuration_candidate",
            d,
            w15,
            records=("run amr07-2026-04-15", "snapshot binding amr07-2026-04-15 map r14"),
            evidence=(bag15,),
            recorded_at=3,
        ),
        claim(
            LIDAR,
            "calibrated_with",
            lidar_cal,
            span(S7_LIFE, wall(2026, 4, 14, 19, 30), None),
            records=("lidar calibration AMR-07 2026-04-14",),
            evidence=(synthetic("AMR-07 lidar calibration file", 1),),
            consolidator="memory.calibration",
            recorded_at=3,
        ),
        claim(
            lidar_cal,
            "calibrated_by",
            record("work order WO-26-0414"),
            span(S7_LIFE, wall(2026, 4, 14, 19, 30), None),
            records=("work order WO-26-0414", "lidar calibration AMR-07 2026-04-14"),
            evidence=(row(CMMS_S7, 6),),
            consolidator="memory.calibration",
            recorded_at=3,
        ),
        *clock_claims(
            AMR,
            [
                (
                    RUN0402,
                    RUN0402_START,
                    RUN0402_START + AMR_RUN_LENGTH,
                    "run amr07-2026-04-02",
                    bag2,
                ),
                (
                    AMR07_CTRL,
                    8_000_000_000,
                    9_000_000_000,
                    "run amr07-2026-04-02",
                    synthetic("AMR-07 controller log", 1),
                ),
            ],
        ),
    ]


INC_0007_TIMELINE: Final = (
    ((2026, 4, 2, 14, 5), "AMR-07 starts pallet pick in aisle A"),
    ((2026, 4, 2, 14, 7), "Fork contacts rack upright; protective stop"),
    ((2026, 4, 2, 14, 9), "Fleet manager flags AMR-07 as blocked"),
    ((2026, 4, 2, 14, 31), "Technician clears the aisle and resets"),
)


def s007_incident() -> Claims:
    """INC-0007 from three sources: the CMMS incident row (synthetic) at 14:07:00 on the CMMS
    clock; the fleet manager's syslog (synthetic) stopping AMR-07 at 14:07:32, placed on the CMMS
    clock through a stated mapping; and the corpus's report, on its own clock. Identity joins the
    CMMS row and the syslog line (same_as) and cannot decide the report (same_as_candidate)."""
    cmms = event(INC_0007_CMMS)
    pstop = event(SYSLOG_PSTOP)
    warn = event(SYSLOG_WARN)
    report = event(INC_0007_REPORT)
    occurred = wall(2026, 4, 2, 14, 7, 0)
    out = facts(
        cmms,
        tick(S7_LIFE, occurred),
        (INC_0007_CMMS,),
        synthetic("S-007 CMMS incidents", 3),
        [
            ("event_kind", text("incident")),
            ("stated_severity", text("Minor, no injury")),
            ("involves", AMR),
            ("at_site", S007),
            ("in_zone", PICK_A),
            ("evidenced_by", record(INC_0007_CMMS)),
        ],
    )
    for name, when, items, line in (
        (
            SYSLOG_PSTOP,
            wall(2026, 4, 2, 14, 7, 32),
            [
                ("event_kind", text("protective_stop")),
                ("declared_kind", text("PSTOP")),
                ("has_description", text("AMR-07 PSTOP: fork contact, rack upright 14B")),
                ("involves", AMR),
                ("evidenced_by", record(SYSLOG_PSTOP)),
            ],
            4182,
        ),
        (
            SYSLOG_WARN,
            wall(2026, 4, 2, 14, 6, 58),
            [
                ("event_kind", text("warning")),
                ("declared_kind", text("WARN")),
                ("has_description", text("AMR-07 WARN: protective field interrupted in PICK-A")),
                ("involves", AMR),
                ("in_zone", PICK_A),
                ("evidenced_by", record(SYSLOG_WARN)),
            ],
            4179,
        ),
    ):
        evidence = synthetic("S-007 fleet manager syslog", line)
        out += facts(event(name), tick(S7_SYSLOG, when), (name,), evidence, items)
        # The same statements placed on the CMMS clock through the stated mapping (bound 0).
        out += facts(
            event(name),
            tick(S7_LIFE, when),
            (name, SYSLOG_MAP, "domain S-007 CMMS exports, local wall time"),
            evidence,
            items,
        )
    out += mapping(
        S7_SYSLOG,
        S7_LIFE,
        span(S7_SYSLOG, wall(2026, 1, 1), None),
        clock_map(S7_SYSLOG, S7_LIFE, "stated", (wall(2026, 1, 1), wall(2026, 1, 1)), 0),
        (SYSLOG_MAP,),
        synthetic("S-007 time-sync statement", 1),
        estimated=False,
    )
    out += facts(
        event(INTERVENTION),
        span(S7_LIFE, wall(2026, 4, 2, 14, 31), wall(2026, 4, 2, 14, 36)),
        (INTERVENTION,),
        synthetic("S-007 intervention log", 31),
        [
            ("event_kind", text("intervention")),
            ("has_description", text("Technician clears the aisle and resets AMR-07")),
            ("involves", AMR),
            ("at_site", S007),
            ("evidenced_by", record(INTERVENTION)),
        ],
    )
    out += facts(
        event(CONTROLLER_FAULT),
        tick(AMR07_CTRL, 8_123_456_789),
        (CONTROLLER_FAULT,),
        synthetic("AMR-07 controller log", 88),
        [
            ("event_kind", text("fault")),
            ("declared_kind", text("FORK_LOAD_SPIKE")),
            ("involves", AMR),
            ("evidenced_by", record(CONTROLLER_FAULT)),
        ],
    )
    out += facts(
        report,
        tick(S7_REPORT, wall(2026, 4, 2, 14, 7)),
        (INC_0007_REPORT,),
        page(REPORT_S7, 0),
        [
            ("event_kind", text("incident")),
            ("stated_severity", text("Minor, no injury")),
            ("involves", AMR),
            ("at_site", S007),
            ("in_zone", PICK_A),
            ("evidenced_by", record(INC_0007_REPORT)),
        ],
    )
    for index, (moment, said) in enumerate(INC_0007_TIMELINE):
        out += facts(
            entry(INC_0007_REPORT, index),
            tick(S7_REPORT, wall(*moment)),
            (INC_0007_REPORT,),
            page(REPORT_S7, 0),
            [("has_description", text(said)), ("evidenced_by", record(INC_0007_REPORT))],
        )
    linked = span(S7_LIFE, occurred, None)
    out.append(
        claim(
            cmms,
            "same_as",
            pstop,
            linked,
            records=(INC_0007_CMMS, SYSLOG_PSTOP),
            evidence=(
                synthetic("S-007 CMMS incidents", 3),
                synthetic("S-007 fleet manager syslog", 4182),
            ),
            consolidator="memory.identity",
            recorded_at=5,
        )
    )
    out.append(
        claim(
            report,
            "same_as_candidate",
            cmms,
            linked,
            records=(INC_0007_REPORT, INC_0007_CMMS),
            evidence=(page(REPORT_S7, 0), synthetic("S-007 CMMS incidents", 3)),
            consolidator="memory.identity",
            recorded_at=5,
        )
    )
    # Different sources within the 5 s window on the CMMS clock: co-occurrence, never cause.
    window = span(S7_LIFE, wall(2026, 4, 2, 14, 6, 58), wall(2026, 4, 2, 14, 7, 3))
    for a, b in ((warn, cmms), (cmms, warn)):
        out.append(
            claim(
                a,
                "co_occurs_within",
                b,
                window,
                kind="observed",
                records=(SYSLOG_WARN, INC_0007_CMMS, SYSLOG_MAP),
                evidence=(
                    synthetic("S-007 fleet manager syslog", 4179),
                    synthetic("S-007 CMMS incidents", 3),
                ),
                consolidator="memory.events",
                recorded_at=5,
            )
        )
    return out


# --- The document --------------------------------------------------------------------------------

# Calibration's vocabulary additions (Memory ADR 0014 §6, graph-schema 1.7.0).
CALIBRATION_PREDICATES: Final = (
    ("calibrated_by", "many", ["configuration"], ["record"]),
    ("calibrated_with", "many", ["sensor"], ["configuration"]),
    ("calibration_candidate", "many", ["sensor"], ["configuration"]),
    ("drift", "many", ["sensor"], ["delta"]),
)
VOCABULARY_VERSION: Final = 9  # events (8, ADR 0013) and calibration (9, ADR 0014) over 1.4.0's 6


def vocabulary() -> dict[str, Any]:
    base = json.loads(VOCABULARY_1_4.read_text(encoding="utf-8"))
    names = {p["name"] for p in base["predicates"]}
    added = [
        {
            "cardinality": cardinality,
            "description": f"{name} (Memory ADR {adr})",
            "domain": domain,
            "name": name,
            "range": range_,
            "version": 1,
        }
        for predicates, adr in ((EVENT_PREDICATES, "0013"), (CALIBRATION_PREDICATES, "0014"))
        for name, cardinality, domain, range_ in predicates
        if name not in names
    ]
    for spec in base["predicates"]:
        if spec["name"] in ("at_site", "at_site_candidate"):
            spec["domain"] = sorted({*spec["domain"], "event"})
        if spec["name"] in ("evidenced_by", "has_name", "same_as", "same_as_candidate"):
            spec["domain"] = sorted({*spec["domain"], "event"})
        if spec["name"] in ("same_as", "same_as_candidate"):
            spec["range"] = sorted({*spec["range"], "event"})
    return {"predicates": sorted([*base["predicates"], *added], key=lambda p: p["name"])}


def acceptance_corpus() -> dict[str, Any]:
    claims = [
        *plant2_identity(),
        *plant2_configuration(),
        *plant2_runs(),
        *plant2_calibration(),
        *plant2_clocks(),
        *plant2_incident(),
        *s007_identity(),
        *s007_configuration(),
        *s007_runs_and_calibration(),
        *s007_incident(),
    ]
    resolver: dict[str, Any] = {
        "priorities": {
            "memory.calibration": 6,
            "memory.configuration": 3,
            "memory.events": 4,
            "memory.identity": 2,
            "memory.time": 5,
            "memory.time_estimates": 7,
        },
        "vocabulary": vocabulary(),
        "vocabulary_version": VOCABULARY_VERSION,
    }
    return {
        "claims": sorted(claims, key=lambda c: (c["recorded_at"], c["id"])),
        "findings": [],
        "generation": f"sha256:{sha(canonical_json.dumps(resolver).decode())}",
        "graph_schema_version": 1,
        "head": 5,
        "kind": "memory.graph",
        "resolver_config": resolver,
    }


NAME: Final = "acceptance_corpus"


def fixture_bytes() -> bytes:
    document = acceptance_corpus()
    return (json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode()


def fixture_path() -> Path:
    return FIXTURES / f"{NAME}.graph.json"


if __name__ == "__main__":
    fixture_path().write_bytes(fixture_bytes())
