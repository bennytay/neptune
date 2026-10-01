"""``inspect`` for MCAP: what the head, the footer and the summary declare, without the data.

It reads what ``plan`` reads first and never scans the data section or decompresses a chunk, so
it costs the same on a megabyte and a terabyte. A file without a usable summary is summarised as
far as its head and tail go, and ``planning`` says that ``plan`` will scan it.
"""

from collections import Counter

from neptune.adapters.contract import AdapterConfig, InspectResult, SourceReader
from neptune.adapters.mcap.layout import Summary, read_head, read_tail
from neptune.adapters.mcap.records import FORMAT_VERSION, MAGIC, TAIL
from neptune.adapters.mcap.report import Reporter
from neptune.adapters.mcap.scan import Place
from neptune.model.finding import FindingCategory, IngestFinding, Severity
from neptune.model.jsonvalue import JsonObject, JsonValue


def _indexed(summary: Summary) -> JsonObject:
    """The chunk indexes in sum, and each channel's extent at chunk granularity."""
    indexes = [index for _, index in summary.chunk_indexes]
    compression = Counter(index.compression.shown for index in indexes)
    bounds: dict[int, list[int]] = {}
    for index in indexes:
        for channel, _ in index.message_index_offsets:
            low, high = bounds.setdefault(channel, [index.start_time, index.end_time])
            bounds[channel] = [min(low, index.start_time), max(high, index.end_time)]
    out: dict[str, JsonValue] = {
        "compression": dict(sorted(compression.items())),
        "count": len(indexes),
        "stored_bytes": sum(index.compressed_size for index in indexes),
        "uncompressed_bytes": sum(index.uncompressed_size for index in indexes),
    }
    if indexes:
        out["message_start_time"] = min(index.start_time for index in indexes)
        out["message_end_time"] = max(index.end_time for index in indexes)
    out["channel_bounds"] = {str(c): b for c, b in sorted(bounds.items())}
    return out


# A hostile summary can hold a million tiny index records; the reply lists the first of each kind
# and counts the rest, so it stays inside the sandbox's reply cap.
MAX_LISTED = 1000


def _listed(out: dict[str, JsonValue], name: str, items: list[JsonValue]) -> None:
    out[name] = items[:MAX_LISTED]
    if len(items) > MAX_LISTED:
        out[f"{name}_omitted"] = len(items) - MAX_LISTED


def _declared(summary: Summary) -> JsonObject:
    counts = {}
    statistics: dict[str, JsonValue] | None = None
    if summary.statistics is not None:
        stats = summary.statistics[1]
        counts = {channel: count for channel, count, _ in stats.channel_message_counts}
        statistics = {
            "attachment_count": stats.attachment_count,
            "channel_count": stats.channel_count,
            "chunk_count": stats.chunk_count,
            "message_count": stats.message_count,
            "message_end_time": stats.message_end_time,
            "message_start_time": stats.message_start_time,
            "metadata_count": stats.metadata_count,
            "schema_count": stats.schema_count,
        }
    channels: list[JsonValue] = []
    for channel_id, (_, channel) in sorted(summary.channels.items()):
        entry: dict[str, JsonValue] = {
            "id": channel_id,
            "message_encoding": channel.message_encoding.shown,
            "metadata": {key.shown: value.shown for key, value in channel.metadata},
            "schema_id": channel.schema_id,
            "topic": channel.topic.shown,
        }
        if channel_id in counts:
            entry["message_count"] = counts[channel_id]
        channels.append(entry)
    out: dict[str, JsonValue] = {
        "chunks": _indexed(summary),
        "records": dict(sorted(summary.records.items())),
    }
    _listed(out, "channels", channels)
    _listed(
        out,
        "attachments",
        [
            {
                "bytes": index.data_size,
                "create_time": index.create_time,
                "log_time": index.log_time,
                "media_type": index.media_type.shown,
                "name": index.name.shown,
                "offset": index.offset,
            }
            for _, index in summary.attachment_indexes
        ],
    )
    _listed(
        out,
        "metadata",
        [
            {"length": index.length, "name": index.name.shown, "offset": index.offset}
            for _, index in summary.metadata_indexes
        ],
    )
    _listed(
        out,
        "schemas",
        [
            {
                "bytes": schema.data[1],
                "encoding": schema.encoding.shown,
                "id": schema_id,
                "name": schema.name.shown,
            }
            for schema_id, (_, schema) in sorted(summary.schemas.items())
        ],
    )
    if statistics is not None:
        out["statistics"] = statistics
    return out


def summarize(source: SourceReader, config: AdapterConfig) -> InspectResult:
    reporter = Reporter(source, config)
    findings: list[IngestFinding] = []
    head = read_head(source)
    out: dict[str, JsonValue] = {"magic": head.magic, "size": source.size}
    if not head.magic:
        findings.append(
            reporter.finding(
                "bad_magic",
                FindingCategory.CORRUPT,
                Severity.ERROR,
                Place(((0, min(source.size, len(MAGIC))),)),
                "the source does not start with MCAP's magic",
                {"size": source.size},
            )
        )
        return InspectResult(out, tuple(findings))
    out["format_version"] = FORMAT_VERSION
    if head.header is not None:
        header = head.header[1]
        out["header"] = {"library": header.library.shown, "profile": header.profile.shown}
    tail = read_tail(source)
    # "indexed" means the summary carries a chunk index; plan uses it only once it passes its checks
    planning = "scan"
    if tail.footer is None:
        out["summary"] = "no footer"
        start = max(len(MAGIC), source.size - TAIL)
        findings.append(
            reporter.finding(
                "truncated",
                FindingCategory.CORRUPT,
                Severity.WARNING,
                Place(((start, source.size - start),)),
                "the file does not end with a footer and the closing magic; plan scans it",
                {"size": source.size},
            )
        )
    else:
        footer = tail.footer[1]
        out["footer"] = {
            "summary_crc": footer.summary_crc,
            "summary_offset_start": footer.summary_offset_start,
            "summary_start": footer.summary_start,
        }
        if tail.problem is not None:
            reason, place = tail.problem
            out["summary"] = f"unusable: {reason}"
            findings.append(
                reporter.finding(
                    "summary_unusable",
                    FindingCategory.CORRUPT,
                    Severity.WARNING,
                    place,
                    f"the summary is not usable ({reason}); plan scans the data section",
                    {"reason": reason},
                )
            )
        elif tail.summary is None:
            out["summary"] = "absent"
        else:
            out["summary"] = "usable"
            out |= _declared(tail.summary)
            if tail.summary.chunk_indexes:
                planning = "indexed"
    out["planning"] = planning
    return InspectResult(out, tuple(findings))
