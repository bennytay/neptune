"""``inspect`` for ROS 1 bags: what the head and the index declare, without the data.

It reads what ``plan`` reads first (the Bag Header and, when it points at one, the index) and never
scans the data section or decompresses a chunk, so it costs the same on a megabyte and a terabyte.
A bag without a usable index is summarised as far as its head goes, and ``planning`` says that
``plan`` will scan it.
"""

from neptune.adapters.contract import AdapterConfig, InspectResult, SourceReader
from neptune.adapters.rosbag1.layout import INDEX_PROBLEMS, Layout, read_head, read_index
from neptune.adapters.rosbag1.records import FORMAT_VERSION, MAGIC
from neptune.adapters.rosbag1.report import Reporter, limits
from neptune.adapters.rosbag1.scan import Place
from neptune.model.finding import FindingCategory, IngestFinding, Severity
from neptune.model.jsonvalue import JsonValue

# A hostile index can declare thousands of connections; the reply lists the first and counts the
# rest, so it stays inside the sandbox's reply cap.
MAX_LISTED = 1000


def _connections(layout: Layout) -> dict[str, JsonValue]:
    counts = layout.stated.counts if layout.stated is not None else {}
    listed: list[JsonValue] = []
    for conn, declared in sorted(layout.declared.items()):
        entry: dict[str, JsonValue] = {"id": conn, **dict(declared.brief)}
        if conn in counts:
            entry["message_count"] = counts[conn][0]
        listed.append(entry)
    out: dict[str, JsonValue] = {"connections": listed[:MAX_LISTED]}
    if len(listed) > MAX_LISTED:
        out["connections_omitted"] = len(listed) - MAX_LISTED
    return out


def summarize(source: SourceReader, config: AdapterConfig) -> InspectResult:
    reporter = Reporter(source, config)
    bounds = limits(config)
    findings: list[IngestFinding] = []
    head = read_head(source, bounds)
    out: dict[str, JsonValue] = {"magic": head.problem != "magic", "size": source.size}
    if head.problem == "magic":
        findings.append(
            reporter.finding(
                "bad_magic",
                FindingCategory.CORRUPT,
                Severity.ERROR,
                Place(((0, min(source.size, len(MAGIC))),)),
                "the source does not start with the ROS 1 bag 2.0 magic",
                {"size": source.size},
            )
        )
        return InspectResult(out, tuple(findings))
    out["format_version"] = FORMAT_VERSION
    out["planning"] = "scan"
    if head.header is None:
        out["bag_header"] = "unusable" if head.problem != "missing" else "absent"
        out["index"] = "absent"
        return InspectResult(out, tuple(findings))
    header = head.header
    out["bag_header"] = {
        "chunk_count": header.chunk_count,
        "conn_count": header.conn_count,
        "index_pos": header.index_pos,
    }
    found = read_index(source, reporter, head, bounds, 10**18)
    if isinstance(found, Layout):
        out["index"] = "usable"
        out["planning"] = "indexed"
        out["chunks"] = len(found.units)
        stated = found.stated
        if stated is not None:
            out["messages"] = sum(total for total, _ in stated.counts.values())
        out |= _connections(found)
        findings += found.findings
    else:
        out["index"] = f"unusable: {found.reason}"
        findings.append(
            reporter.finding(
                "index_invalid",
                FindingCategory.INCONSISTENT,
                Severity.INFO if found.reason == "unclosed" else Severity.WARNING,
                found.place,
                f"the bag's index {INDEX_PROBLEMS[found.reason]}; plan scans the data section",
                {"reason": found.reason},
            )
        )
    return InspectResult(out, tuple(findings))
