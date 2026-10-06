"""Generator of the pack compiler's fixture snapshots (``fixtures/packs/*.graph.json``).

Each is a graph document in Memory's published graph-schema shape (``contracts/graph-schema``),
written the way Memory's consolidators state things (Memory ADR 0010 for configuration lineage,
ADR 0013 for events): Ambiguous as ``*_candidate`` claims, Unknown as ``*_unknown`` claims naming a
record, every claim on one clock. Deploy cannot run Memory (it reads the contract only), so the
fixtures are generated here and validated against the contract's JSON Schema by the tests.

Claim, finding and record ids are pattern-valid sha256 ids derived from fixture content; Deploy
never re-derives Memory's ids. ``python deploy_pack_graphs.py`` rewrites the files;
``test_deploy_packs_fixtures`` checks they are what this module generates.

- ``arm_cell_configuration``: graph-schema 1.2.0 (vocabulary 4). A manipulator cell (ARM-06) and an
  AMR (AMR-12) at one site, under one deployment: a decided span, an Ambiguous span (two
  candidates), an Unknown gap, a succession, an authorisation envelope cited by an external object,
  runs on their own clocks, an inferred run link, a superseded version and a resolver finding.
- ``arm_cell_events``: graph-schema 1.6.0 shape (MVL-134, PR #125; vocabulary 8): an e-stop on the
  HMI clock mapped onto the civil clock, a fault row on the civil clock, an intervention mapped by
  two clock mappings to two times (a conflict), an event on an unmapped clock, an event that might
  involve either machine, and a co-occurrence pair.
- ``dock_fleet_configuration_v2``: graph-schema 2.0.0 (vocabulary 11, ``graph_schema`` and
  ``builds``; Deploy ADR 0018). Two AMRs share one configuration node and only AMR-05 changes
  (rule 12: a change is one machine's own abutting spans); a ``succeeds`` statement about the two
  configurations that no change may be read from; a palletising arm whose decided span meets an
  Ambiguous span, then an Unknown one; a legged inspection robot that changes on its own clock;
  and an incident involving AMR-06, the machine that did not change.
"""

import hashlib
import json
from pathlib import Path
from typing import Any, Final

from neptune.identity import canonical_json

ROOT: Final = Path(__file__).resolve().parent
REPO: Final = ROOT.parents[2]
FIXTURES: Final = ROOT / "fixtures" / "packs"
VOCABULARY_1_2: Final = REPO / "contracts/graph-schema/v1.2.0/golden/vocabulary.json"
VOCABULARY_2_0: Final = REPO / "contracts/graph-schema/v2.0.0/golden/vocabulary.json"


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def rec(name: str) -> str:
    return f"rec:sha256:{sha('record ' + name)}"


def src(name: str) -> str:
    return f"sha256:{sha('source ' + name)}"


def node(node_type: str, node_id: str) -> dict[str, Any]:
    return {"kind": "node", "node_id": node_id, "node_type": node_type}


def text(value: str) -> dict[str, Any]:
    return {
        "datatype": "text",
        "kind": "literal",
        "unit": {"knowledge": "not_applicable"},
        "value": value,
    }


def record(name: str) -> dict[str, Any]:
    return {"kind": "record", "record_id": rec(name)}


def at(domain: str, ticks: int) -> dict[str, Any]:
    return {"domain_id": domain, "ticks": ticks}


def row(source: str, line: int) -> dict[str, Any]:
    return {"locator": [{"kind": "row", "row": line}], "source": src(source)}


def claim(
    subject: dict[str, Any],
    predicate: str,
    obj: dict[str, Any],
    valid: tuple[dict[str, Any], dict[str, Any] | str],
    *,
    records: tuple[str, ...],
    evidence: tuple[dict[str, Any], ...],
    kind: str = "stated",
    consolidator: str = "memory.configuration",
    consolidator_version: str = "1",
    recorded_at: int = 1,
    superseded_at: int | str = "open",
    model: dict[str, str] | None = None,
    confidence: float | None = None,
) -> dict[str, Any]:
    provenance: dict[str, Any] = {
        "config_hash": f"sha256:{sha('config ' + consolidator)}",
        "consolidator_id": consolidator,
        "consolidator_version": consolidator_version,
        "evidence": list(evidence),
        "records": [rec(r) for r in records],
    }
    if model is not None:
        provenance["model"] = model
    content: dict[str, Any] = {
        "assertion_kind": kind,
        "confidence": {"knowledge": "not_applicable"}
        if confidence is None
        else {"knowledge": "known", "value": confidence},
        "object": obj,
        "predicate": predicate,
        "provenance": provenance,
        "subject": subject,
        "valid": {"end": valid[1], "start": valid[0]},
    }
    digest = sha(canonical_json.dumps({"claim": content, "scheme": "deploy-pack-fixture"}).decode())
    return {
        **content,
        "id": f"claim:sha256:{digest}",
        "recorded_at": recorded_at,
        "superseded_at": superseded_at,
        "supersedes": [],
    }


def finding(
    code: str, about: dict[str, Any], others: list[dict[str, Any]], tx: int
) -> dict[str, Any]:
    provenance = {
        "config_hash": f"sha256:{sha('resolver')}",
        "resolver_id": "memory.supersede",
        "resolver_version": "2",
    }
    body = {"claim": about["id"], "code": code, "others": [o["id"] for o in others]}
    return {
        **body,
        "id": f"finding:sha256:{sha(canonical_json.dumps(body).decode())}",
        "provenance": provenance,
        "recorded_at": tx,
        "superseded_at": "open",
    }


def graph(
    claims: list[dict[str, Any]],
    findings: list[dict[str, Any]],
    head: int,
    vocabulary: dict[str, Any],
    vocabulary_version: int,
    release: str | None = None,
) -> dict[str, Any]:
    """A graph document: graph-schema 1.x, or with ``release`` the 2.x document that names it, with
    one build per consolidator listing every claim it emitted."""
    resolver: dict[str, Any] = {
        "priorities": {"memory.configuration": 3, "memory.events": 4, "memory.identity": 2},
        "vocabulary": vocabulary,
        "vocabulary_version": vocabulary_version,
    }
    order = sorted(claims, key=lambda c: (c["recorded_at"], c["id"]))
    document: dict[str, Any] = {
        "claims": order,
        "findings": sorted(findings, key=lambda f: (f["recorded_at"], f["id"])),
        "generation": f"sha256:{sha(canonical_json.dumps(resolver).decode())}",
        "graph_schema_version": 1,
        "head": head,
        "kind": "memory.graph",
        "resolver_config": resolver,
    }
    if release is None:
        return document
    builds: dict[str, dict[str, Any]] = {}
    for c in order:
        provenance = c["provenance"]
        build = builds.setdefault(
            provenance["consolidator_id"],
            {
                "claims": [],
                "config_hash": provenance["config_hash"],
                "consolidator_id": provenance["consolidator_id"],
                "recorded_at": 0,
                "version": provenance["consolidator_version"],
            },
        )
        build["claims"].append(c["id"])
        build["recorded_at"] = max(build["recorded_at"], c["recorded_at"])
    for build in builds.values():
        build["claims"].sort()
    return {
        **document,
        "builds": sorted(builds.values(), key=lambda b: (b["recorded_at"], b["consolidator_id"])),
        "graph_schema": release,
        "graph_schema_version": int(release.split(".")[0]),
    }


# --- Clocks and nodes ---------------------------------------------------------------------------

CIVIL: Final = rec("civil clock tai ns")  # a Memory civil clock id
HMI: Final = rec("domain hmi-panel clock")
PLC: Final = rec("domain plc clock")
TEACH: Final = rec("domain teach-pendant clock")  # no mapping reaches it
RUN1_CLOCK: Final = rec("domain run-1 log clock")
RUN2_CLOCK: Final = rec("domain run-2 log clock")
RUN3_CLOCK: Final = rec("domain run-3 log clock")

DAY: Final = 86_400 * 10**9
T0: Final = 1_790_000_000 * 10**9

ARM: Final = node("machine", "asset-tag:ARM-06")
AMR: Final = node("machine", "asset-tag:AMR-12")
SITE: Final = node("site", "site-code:CELL-3")
DEPLOYMENT: Final = node("deployment", "deployment:cell-3-pilot")
LONE: Final = node("machine", "asset-tag:QUAD-02")  # a legged robot nothing is claimed about


def config(name: str) -> dict[str, Any]:
    return node("configuration", f"cmms.config:{name}")


def run(name: str) -> dict[str, Any]:
    return node("run", f"record:{rec('run ' + name)}")


def event(name: str) -> dict[str, Any]:
    return node("event", f"record:{rec('event ' + name)}")


def arm_cell_configuration() -> dict[str, Any]:
    cfg_a, cfg_b, cfg_c, cfg_d = (config(n) for n in ("ARM06-A", "ARM06-B", "ARM06-C·Ω", "ARM06-D"))
    cfg_x, cfg_y, cfg_old = config("AMR12-X"), config("AMR12-Y"), config("ARM06-PRE")
    run1, run2, run3 = run("1"), run("2"), run("3")
    whole_source = {"locator": [], "source": src("commissioning-pack.pdf")}
    confluence = {
        "locator": [{"kind": "page", "index": 2}],
        "source": {
            "connector_id": "deploy_confluence",
            "kind": "external",
            "object_id": "acme.atlassian.net/SAFE/88123",
            "revision_token": "page:14",
        },
    }
    identity: dict[str, Any] = {"consolidator": "memory.identity"}
    located_arm = claim(
        ARM,
        "located_at",
        SITE,
        (at(CIVIL, T0), "open"),
        records=("asset register ARM-06",),
        evidence=(row("assets.csv", 4),),
        **identity,
    )
    located_amr = claim(
        AMR,
        "located_at",
        SITE,
        (at(CIVIL, T0), "open"),
        records=("asset register AMR-12",),
        evidence=(row("assets.csv", 7),),
        **identity,
    )
    deployed = claim(
        DEPLOYMENT,
        "deployed_at",
        SITE,
        (at(CIVIL, T0), "open"),
        records=("deployment charter",),
        evidence=(row("deployments.csv", 1),),
        **identity,
    )
    pre = claim(
        ARM,
        "has_configuration",
        cfg_old,
        (at(CIVIL, T0), "open"),
        records=("commissioning baseline draft",),
        evidence=(row("cmms.csv", 1),),
        superseded_at=2,
    )
    commissioned = claim(
        ARM,
        "has_configuration",
        cfg_a,
        (at(CIVIL, T0), at(CIVIL, T0 + DAY)),
        records=("commissioning baseline ARM-06",),
        evidence=(row("cmms.csv", 2), whole_source),
        recorded_at=2,
    )
    candidate_b = claim(
        ARM,
        "configuration_candidate",
        cfg_b,
        (at(CIVIL, T0 + DAY), at(CIVIL, T0 + 2 * DAY)),
        records=("change record CR-17",),
        evidence=(row("changes.csv", 3),),
        recorded_at=2,
    )
    candidate_c = claim(
        ARM,
        "configuration_candidate",
        cfg_c,
        (at(CIVIL, T0 + DAY), at(CIVIL, T0 + 2 * DAY)),
        records=("maintenance work order WO-311",),
        evidence=(row("work_orders.csv", 9),),
        recorded_at=2,
    )
    gap = claim(
        ARM,
        "configuration_unknown",
        record("maintenance work order WO-340"),
        (at(CIVIL, T0 + 2 * DAY), at(CIVIL, T0 + 3 * DAY)),
        records=("maintenance work order WO-340",),
        evidence=(row("work_orders.csv", 12),),
        recorded_at=2,
    )
    requalified = claim(
        ARM,
        "has_configuration",
        cfg_d,
        (at(CIVIL, T0 + 3 * DAY), "open"),
        records=("requalification RQ-5",),
        evidence=(row("requalifications.csv", 1),),
        recorded_at=2,
    )
    amr_x = claim(
        AMR,
        "has_configuration",
        cfg_x,
        (at(CIVIL, T0), at(CIVIL, T0 + DAY)),
        records=("commissioning baseline AMR-12",),
        evidence=(row("cmms.csv", 5),),
    )
    amr_y = claim(
        AMR,
        "has_configuration",
        cfg_y,
        (at(CIVIL, T0 + DAY), "open"),
        records=("change record CR-18",),
        evidence=(row("changes.csv", 4),),
    )
    succession = claim(
        cfg_y,
        "succeeds",
        cfg_x,
        (at(CIVIL, T0 + DAY), "open"),
        records=("commissioning baseline AMR-12", "change record CR-18"),
        evidence=(row("cmms.csv", 5), row("changes.csv", 4)),
    )
    envelope = claim(
        SITE,
        "authorised_configuration",
        cfg_a,
        (at(CIVIL, T0), at(CIVIL, T0 + 2 * DAY)),
        records=("authorisation envelope AE-2",),
        evidence=(confluence,),
    )
    run1_by = claim(
        run1,
        "recorded_by",
        ARM,
        (at(RUN1_CLOCK, 0), at(RUN1_CLOCK, 1_000)),
        records=("run 1",),
        evidence=(row("runs.csv", 1),),
        **identity,
        recorded_at=3,
    )
    run1_cfg = claim(
        run1,
        "configuration_active_during",
        cfg_a,
        (at(RUN1_CLOCK, 0), at(RUN1_CLOCK, 1_000)),
        records=("run 1", "snapshot binding run 1"),
        evidence=(row("bindings.csv", 1),),
        recorded_at=3,
    )
    run1_gap = claim(
        run1,
        "not_covered_by_authorisation",
        cfg_a,
        (at(RUN1_CLOCK, 600), at(RUN1_CLOCK, 1_000)),
        kind="observed",
        records=("run 1", "snapshot binding run 1", "authorisation envelope AE-2"),
        evidence=(row("bindings.csv", 1),),
        recorded_at=3,
    )
    run2_by = claim(
        run2,
        "recorded_by",
        AMR,
        (at(RUN2_CLOCK, 0), at(RUN2_CLOCK, 500)),
        records=("run 2",),
        evidence=(row("runs.csv", 2),),
        **identity,
        recorded_at=3,
    )
    run2_unknown = claim(
        run2,
        "configuration_unknown",
        record("run 2"),
        (at(RUN2_CLOCK, 0), at(RUN2_CLOCK, 500)),
        kind="observed",
        records=("run 2",),
        evidence=(row("runs.csv", 2),),
        recorded_at=3,
    )
    run3_by = claim(
        run3,
        "recorded_by",
        ARM,
        (at(RUN3_CLOCK, 0), at(RUN3_CLOCK, 200)),
        kind="inferred",
        model={"model_id": "log-attribution", "model_version": "3"},
        confidence=0.6,
        records=("run 3",),
        evidence=(row("runs.csv", 3),),
        **identity,
        recorded_at=3,
    )
    run3_cfg = claim(
        run3,
        "configuration_active_during",
        cfg_d,
        (at(RUN3_CLOCK, 0), at(RUN3_CLOCK, 200)),
        records=("run 3", "snapshot binding run 3"),
        evidence=(row("bindings.csv", 3),),
        recorded_at=3,
    )
    claims = [
        located_arm,
        located_amr,
        deployed,
        pre,
        commissioned,
        candidate_b,
        candidate_c,
        gap,
        requalified,
        amr_x,
        amr_y,
        succession,
        envelope,
        run1_by,
        run1_cfg,
        run1_gap,
        run2_by,
        run2_unknown,
        run3_by,
        run3_cfg,
    ]
    findings = [finding("overridden_on_arrival", pre, [commissioned], 2)]
    vocabulary = json.loads(VOCABULARY_1_2.read_text(encoding="utf-8"))
    return graph(claims, findings, 3, vocabulary, 4)


# The event vocabulary of graph-schema 1.6.0 (Memory ADR 0013 §6, PR #125): what the 1.2.0
# vocabulary lacks, with the domains and ranges #125 publishes.
EVENT_PREDICATES: Final = (
    ("at_site", "one", ["event", "run"], ["site"]),
    ("at_site_candidate", "many", ["event", "run"], ["site"]),
    ("co_occurs_within", "many", ["event"], ["event"]),
    ("declared_kind", "one", ["event"], ["integer", "text"]),
    ("event_kind", "one", ["event"], ["text"]),
    ("has_description", "one", ["event"], ["text"]),
    ("in_zone", "one", ["event"], ["zone"]),
    ("in_zone_candidate", "many", ["event"], ["zone"]),
    ("involves", "many", ["event"], ["asset", "machine"]),
    ("involves_candidate", "many", ["event"], ["asset", "machine"]),
    ("stated_severity", "one", ["event"], ["integer", "text"]),
)


def _event_vocabulary() -> dict[str, Any]:
    base = json.loads(VOCABULARY_1_2.read_text(encoding="utf-8"))
    names = {p["name"] for p in base["predicates"]}
    added = [
        {
            "cardinality": cardinality,
            "description": f"{name} (Memory ADR 0013)",
            "domain": domain,
            "name": name,
            "range": range_,
            "version": 1,
        }
        for name, cardinality, domain, range_ in EVENT_PREDICATES
        if name not in names
    ]
    for spec in base["predicates"]:
        if spec["name"] == "evidenced_by":
            spec["domain"] = sorted({*spec["domain"], "event"})
    return {"predicates": sorted([*base["predicates"], *added], key=lambda p: p["name"])}


def arm_cell_events() -> dict[str, Any]:
    events: dict[str, Any] = {"consolidator": "memory.events"}
    estop, fault, intervention, pendant, maybe = (
        event(n) for n in ("estop", "fault", "intervention", "pendant", "maybe")
    )
    h0 = 5_000_000  # HMI clock ticks (ms)
    c_estop = T0 + 3_600 * 10**9  # its mapped onset on the civil clock
    hmi_map, plc_map_a, plc_map_b = (
        "clock mapping hmi->civil",
        "clock mapping plc->civil A",
        "clock mapping plc->civil B",
    )

    def facts(
        subject: dict[str, Any],
        valid: tuple[dict[str, Any], dict[str, Any] | str],
        base: tuple[str, ...],
        source: str,
        line: int,
        items: list[tuple[str, dict[str, Any]]],
    ) -> list[dict[str, Any]]:
        return [
            claim(
                subject,
                predicate,
                obj,
                valid,
                records=base,
                evidence=(row(source, line),),
                recorded_at=4,
                **events,
            )
            for predicate, obj in items
        ]

    estop_items = [
        ("event_kind", text("emergency_stop")),
        ("stated_severity", text("high")),
        ("has_description", text("Operator pressed the E-stop → arm halted mid-pick")),
        ("involves", ARM),
        ("at_site", SITE),
        ("evidenced_by", record("incident INC-C3-0011")),
    ]
    claims = [
        *facts(
            estop,
            (at(HMI, h0), at(HMI, h0 + 1)),
            ("incident INC-C3-0011",),
            "hmi.csv",
            40,
            estop_items,
        ),
        *facts(
            estop,
            (at(CIVIL, c_estop - 5), at(CIVIL, c_estop + 6)),
            ("incident INC-C3-0011", hmi_map, "civil clock"),
            "hmi.csv",
            40,
            estop_items,
        ),
    ]
    fault_items = [
        ("event_kind", text("fault")),
        ("declared_kind", text("DRIVE_OVERCURRENT")),
        ("involves", ARM),
        ("evidenced_by", record("diagnostics row 812")),
    ]
    c_fault = c_estop + 2 * 10**9
    claims += facts(
        fault,
        (at(CIVIL, c_fault), at(CIVIL, c_fault + 1)),
        ("diagnostics row 812",),
        "diagnostics.csv",
        812,
        fault_items,
    )
    p0 = 77_000
    intervention_items = [
        ("event_kind", text("intervention")),
        ("involves", ARM),
        ("evidenced_by", record("intervention IV-9")),
    ]
    c_iv = c_estop + 60 * 10**9
    claims += facts(
        intervention,
        (at(PLC, p0), at(PLC, p0 + 30)),
        ("intervention IV-9",),
        "plc.csv",
        3,
        intervention_items,
    )
    claims += facts(
        intervention,
        (at(CIVIL, c_iv), at(CIVIL, c_iv + 30 * 10**9)),
        ("intervention IV-9", plc_map_a, "civil clock"),
        "plc.csv",
        3,
        intervention_items,
    )
    claims += facts(
        intervention,
        (at(CIVIL, c_iv + 97 * 10**9), at(CIVIL, c_iv + 127 * 10**9)),
        ("intervention IV-9", plc_map_b, "civil clock"),
        "plc.csv",
        3,
        intervention_items,
    )
    claims += facts(
        pendant,
        (at(TEACH, 12), at(TEACH, 13)),
        ("pendant log line 12",),
        "pendant.log",
        12,
        [("event_kind", text("protective_stop")), ("involves", ARM)],
    )
    claims += facts(
        maybe,
        (at(CIVIL, c_fault + 10**9), at(CIVIL, c_fault + 10**9 + 1)),
        ("ticket T-55",),
        "tickets.csv",
        55,
        [
            ("event_kind", text("near_miss")),
            ("involves_candidate", ARM),
            ("involves_candidate", AMR),
            ("at_site", SITE),
        ],
    )
    claims.append(
        claim(
            ARM,
            "located_at",
            SITE,
            (at(CIVIL, T0), "open"),
            records=("asset register ARM-06",),
            evidence=(row("assets.csv", 4),),
            consolidator="memory.identity",
        )
    )
    window = (at(CIVIL, c_estop - 5), at(CIVIL, c_estop - 5 + 5 * 10**9))
    for a, b in ((estop, fault), (fault, estop)):
        claims.append(
            claim(
                a,
                "co_occurs_within",
                b,
                window,
                kind="observed",
                records=("incident INC-C3-0011", "diagnostics row 812", hmi_map),
                evidence=(row("hmi.csv", 40), row("diagnostics.csv", 812)),
                recorded_at=4,
                **events,
            )
        )
    return graph(claims, [], 4, _event_vocabulary(), 8)


# --- graph-schema 2.0.0: machine-scoped configuration changes (Deploy ADR 0018) --------------

DOCK: Final = node("site", "site-code:DOCK-1")
AMR05: Final = node("machine", "asset-tag:AMR-05")
AMR06: Final = node("machine", "asset-tag:AMR-06")
PALLET_ARM: Final = node("machine", "asset-tag:ARM-09")
QUAD: Final = node("machine", "asset-tag:QUAD-03")
QUAD_CLOCK: Final = rec("domain quad-03 controller clock")  # no mapping relates it to civil time
INCIDENT: Final = event("INC-D1-0004")
HOUR: Final = 3_600 * 10**9


def dock_fleet_configuration_v2() -> dict[str, Any]:
    nav_old, nav_new = config("fleet-nav-4.2.0"), config("fleet-nav-4.3.1")
    arm_a, arm_b, arm_c, arm_d = (config(f"ARM09-{n}") for n in "ABCD")
    quad_1, quad_2 = config("QUAD03-gait-1"), config("QUAD03-gait-2")
    identity: dict[str, Any] = {"consolidator": "memory.identity", "consolidator_version": "3"}
    configuration: dict[str, Any] = {"consolidator_version": "2"}
    events: dict[str, Any] = {"consolidator": "memory.events", "consolidator_version": "1"}

    def located(machine: dict[str, Any], line: int) -> dict[str, Any]:
        return claim(
            machine,
            "located_at",
            DOCK,
            (at(CIVIL, T0), "open"),
            records=(f"asset register {machine['node_id']}",),
            evidence=(row("assets.csv", line),),
            **identity,
        )

    def span(
        machine: dict[str, Any],
        predicate: str,
        obj: dict[str, Any],
        valid: tuple[dict[str, Any], dict[str, Any] | str],
        record_name: str,
        source: str,
        line: int,
    ) -> dict[str, Any]:
        return claim(
            machine,
            predicate,
            obj,
            valid,
            records=(record_name,),
            evidence=(row(source, line),),
            recorded_at=2,
            **configuration,
        )

    claims = [located(m, i) for i, m in enumerate((AMR05, AMR06, PALLET_ARM, QUAD), start=1)]
    claims += [
        # Both AMRs commissioned on the shared 4.2.0 node; only AMR-05 moves to 4.3.1.
        span(
            AMR05,
            "has_configuration",
            nav_old,
            (at(CIVIL, T0), at(CIVIL, T0 + DAY)),
            "commissioning AMR-05",
            "cmms.csv",
            1,
        ),
        span(
            AMR05,
            "has_configuration",
            nav_new,
            (at(CIVIL, T0 + DAY), "open"),
            "change record CR-40 AMR-05",
            "changes.csv",
            1,
        ),
        span(
            AMR06,
            "has_configuration",
            nav_old,
            (at(CIVIL, T0), "open"),
            "commissioning AMR-06",
            "cmms.csv",
            2,
        ),
        # The palletising arm: decided, then Ambiguous (two readings), then Unknown, then decided.
        span(
            PALLET_ARM,
            "has_configuration",
            arm_a,
            (at(CIVIL, T0), at(CIVIL, T0 + DAY)),
            "commissioning ARM-09",
            "cmms.csv",
            3,
        ),
        span(
            PALLET_ARM,
            "configuration_candidate",
            arm_b,
            (at(CIVIL, T0 + DAY), at(CIVIL, T0 + 2 * DAY)),
            "change record CR-41",
            "changes.csv",
            2,
        ),
        span(
            PALLET_ARM,
            "configuration_candidate",
            arm_c,
            (at(CIVIL, T0 + DAY), at(CIVIL, T0 + 2 * DAY)),
            "work order WO-77",
            "work_orders.csv",
            7,
        ),
        span(
            PALLET_ARM,
            "configuration_unknown",
            record("work order WO-80"),
            (at(CIVIL, T0 + 2 * DAY), at(CIVIL, T0 + 3 * DAY)),
            "work order WO-80",
            "work_orders.csv",
            8,
        ),
        span(
            PALLET_ARM,
            "has_configuration",
            arm_d,
            (at(CIVIL, T0 + 3 * DAY), "open"),
            "requalification RQ-9",
            "requalifications.csv",
            1,
        ),
        # The legged robot changes gait configuration on its own controller clock only.
        span(
            QUAD,
            "has_configuration",
            quad_1,
            (at(QUAD_CLOCK, 0), at(QUAD_CLOCK, 5_000)),
            "commissioning QUAD-03",
            "cmms.csv",
            4,
        ),
        span(
            QUAD,
            "has_configuration",
            quad_2,
            (at(QUAD_CLOCK, 5_000), "open"),
            "change record CR-42",
            "changes.csv",
            3,
        ),
        # A statement about the two configurations themselves (the reading 2.0.0 keeps): it says
        # nothing about which machine changed, and no change is read from it.
        claim(
            nav_new,
            "succeeds",
            nav_old,
            (at(CIVIL, T0 + DAY), "open"),
            records=("release note fleet-nav 4.3.1",),
            evidence=(row("releases.csv", 1),),
            consolidator="memory.release_notes",
            recorded_at=2,
        ),
    ]
    onset = (at(CIVIL, T0 + DAY + 2 * HOUR), at(CIVIL, T0 + DAY + 2 * HOUR + 1))
    for predicate, obj in (
        ("event_kind", text("collision")),
        ("involves", AMR06),
        ("at_site", DOCK),
        ("evidenced_by", record("incident INC-D1-0004")),
    ):
        claims.append(
            claim(
                INCIDENT,
                predicate,
                obj,
                onset,
                records=("incident INC-D1-0004",),
                evidence=(row("incidents.csv", 4),),
                recorded_at=3,
                **events,
            )
        )
    vocabulary = json.loads(VOCABULARY_2_0.read_text(encoding="utf-8"))
    return graph(claims, [], 3, vocabulary, 11, release="2.0.0")


GENERATORS: Final = {
    "arm_cell_configuration": arm_cell_configuration,
    "arm_cell_events": arm_cell_events,
    "dock_fleet_configuration_v2": dock_fleet_configuration_v2,
}


def fixture_bytes(name: str) -> bytes:
    """A fixture's file bytes: two-space indented JSON with sorted keys (a snapshot need not be
    canonical; the compiler hashes its canonical form)."""
    document = GENERATORS[name]()
    return (json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode()


def fixture_path(name: str) -> Path:
    return FIXTURES / f"{name}.graph.json"


if __name__ == "__main__":
    FIXTURES.mkdir(parents=True, exist_ok=True)
    for fixture in GENERATORS:
        fixture_path(fixture).write_bytes(fixture_bytes(fixture))
