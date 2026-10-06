"""URDF and Xacro robot descriptions: Neptune's embodiment adapter (ADR 0039).

What it emits for a description whose root is ``<robot>`` (``describe`` in ``description``):

- one ``HardwareConfiguration`` (``machine`` and ``revision`` ``NotCovered``: a URDF names a
  robot model, never a machine) and one ``FrameGraph`` for the whole source;
- per ``<link>``: a ``link`` component and its ``Frame``; per ``<joint>``: a ``joint`` component
  framed at its child link and the ``FrameTransform`` of its origin; per transmission
  ``<actuator>``, per ``<sensor>`` and per ``<sensor>`` in a ``<gazebo>`` block: a component;
- a ``HardwareSpecification`` per component (and one for the robot) holding what its element
  declares: joint type, axis, limits, dynamics and mimic, inertia, visual and collision geometry
  with mesh references verbatim, materials, sensor settings and plugins;
- a ``DescriptionExtension`` per ``<gazebo>``, ``<ros2_control>`` or other top-level block,
  kept opaque.

A Xacro file (the ``xacro`` prefix is declared or used) is expanded first (``xacro``), without a
ROS installation, as far as the file alone decides it; one ``DescriptionExpansion`` records the
expansion's digest, size and arguments. Its records cite the expansion:
``[ByteRange(0, size), urdf:expansion, ByteRange(element)]``. A URDF's records cite their
element's bytes. Every problem is a finding; one malformed or hostile file never fails a job.

Probing reads the head's root element: ``<robot>`` is ``STRUCTURE``. A head that does not show a
root but whose name ends ``.urdf`` or ``.xacro`` is claimed ``NAME_ONLY``, so a damaged
description is still read and reported. Planning gives one chunk: a description is read whole,
and the records never depend on how it is planned.
"""

from typing import TYPE_CHECKING, Final

from neptune.adapters.contract import (
    ABI_VERSION,
    NAME_ONLY,
    PROBE_HEAD_SIZE,
    STRUCTURE,
    AdapterConfig,
    AdapterDescriptor,
    Chunk,
    ChunkOutput,
    ConfigOption,
    Documented,
    FormatSpec,
    InspectResult,
    Plan,
    ProbeHints,
    ProbeReason,
    ProbeResult,
    Resources,
    SourceReader,
    make_chunk,
    read_pieces,
)
from neptune.adapters.urdf import xacro
from neptune.adapters.urdf.description import describe
from neptune.adapters.urdf.xmltree import Element, TooLarge, XmlError, parse, serialize
from neptune.identity.findings import ingest_finding
from neptune.identity.hashing import content_id
from neptune.identity.provenance import evidence_record_id
from neptune.model.finding import FindingCategory, IngestFinding, Severity
from neptune.model.knowledge import (
    AssertionKind,
    Knowledge,
    Known,
    NotApplicable,
    NotCovered,
    Unknown,
)
from neptune.model.machine import DeclaredParameter, DescriptionExpansion, ParameterValue
from neptune.model.provenance import (
    AdapterLocator,
    ByteRange,
    EvidenceRef,
    Provenance,
    adapter_locator,
)

if TYPE_CHECKING:
    from neptune.model.ids import RecordId

__all__ = ["DESCRIPTOR", "EXPANSION_STEP", "UrdfAdapter"]

ADAPTER_ID: Final = "urdf"
BOM: Final = b"\xef\xbb\xbf"
XACRO_NAMESPACE: Final = b"ros.org/wiki/xacro"
MAX_FINDINGS: Final = 1000
# The deepest nesting read, whatever max_depth says: the reader, expander and writer recurse once
# a level, and stay well inside Python's recursion limit.
MAX_DEPTH: Final = 128
EXPANSION_STEP: Final[AdapterLocator] = adapter_locator("urdf:expansion", {"language": "xacro"})

_ERRORS: Final[dict[str, tuple[FindingCategory, Severity]]] = {
    "doctype_refused": (FindingCategory.UNSUPPORTED, Severity.ERROR),
    "encoding_unsupported": (FindingCategory.UNSUPPORTED, Severity.ERROR),
    "limit_exceeded": (FindingCategory.LIMIT, Severity.ERROR),
    "xml_malformed": (FindingCategory.CORRUPT, Severity.ERROR),
}


def _code(name: str, description: str) -> Documented:
    return Documented(f"{ADAPTER_ID}.{name}", description)


DESCRIPTOR: Final = AdapterDescriptor(
    id=ADAPTER_ID,
    version="0.1.0",
    abi=ABI_VERSION,
    summary="URDF and Xacro robot descriptions: hardware, components, frames and transforms.",
    formats=(
        FormatSpec("URDF", extensions=(".urdf",)),
        FormatSpec("Xacro", extensions=(".xacro",)),
    ),
    record_kinds=(
        "description_expansion",
        "description_extension",
        "frame",
        "frame_graph",
        "frame_transform",
        "hardware_component",
        "hardware_configuration",
        "hardware_specification",
    ),
    config=(
        ConfigOption(
            "max_bytes",
            16 * 1024 * 1024,
            "a source, or a Xacro expansion, larger than this many bytes is reported, not read",
        ),
        ConfigOption(
            "max_depth",
            64,
            f"elements nested deeper than this, and never more than {MAX_DEPTH}, stop the read",
        ),
        ConfigOption(
            "max_elements", 50_000, "a document or expansion with more elements is not read"
        ),
        ConfigOption(
            "max_expansion_ratio",
            64,
            "a Xacro expansion more than this many times the source's size is reported, not read",
        ),
    ),
    libraries=(),
    finding_codes=(
        _code("doctype_refused", "a DOCTYPE or entity declaration; nothing is read (error)"),
        _code(
            "element_repeated",
            "an element the URDF specification allows once appears again; none of them is a"
            " parameter (inconsistent, warning)",
        ),
        _code(
            "encoding_unsupported",
            "the XML declaration names an encoding other than UTF-8; nothing is read (error)",
        ),
        _code(
            "findings_capped",
            f"more than {MAX_FINDINGS} findings; the first are kept and counted (limit, warning)",
        ),
        _code(
            "joint_invalid",
            "a joint's parent and child are one link; no transform (inconsistent, warning)",
        ),
        _code(
            "limit_exceeded",
            "max_depth, max_elements, max_bytes, max_expansion_ratio or a fixed bound stopped the"
            " read (limit, error)",
        ),
        _code(
            "link_undeclared",
            "a joint or gazebo reference names a link the description does not declare"
            " (inconsistent, warning)",
        ),
        _code(
            "name_repeated",
            "two links or two joints share a name; both are recorded (inconsistent, warning)",
        ),
        _code(
            "name_unrepresentable",
            "a link name is too long to name a frame; the frame is Unknown (warning)",
        ),
        _code(
            "no_links",
            "the robot declares no link; no configuration is recorded (missing, warning)",
        ),
        _code("not_robot", "the root element is not <robot>; nothing is read (error)"),
        _code(
            "origin_invalid",
            "a joint origin is not three finite numbers each; no transform (corrupt, warning)",
        ),
        _code(
            "required_missing",
            "an attribute or element the URDF specification requires is absent (missing, warning)",
        ),
        _code("too_large", "the source is larger than max_bytes; it is not read (limit, error)"),
        _code(
            "value_unparsable",
            "a value the specification makes numbers is not numbers; it is Unknown (corrupt,"
            " warning)",
        ),
        _code(
            "xacro_condition_undecided",
            "a xacro:if or xacro:unless the file cannot decide; its content is dropped (missing,"
            " error)",
        ),
        _code(
            "xacro_include_not_followed",
            "xacro:include names another file, never read here; what it defines is not covered"
            " (missing, error)",
        ),
        _code(
            "xacro_invalid",
            "a Xacro construct is invalid (bad expression, call or definition): the value is"
            " left as declared, or the construct dropped (corrupt)",
        ),
        _code(
            "xacro_not_covered",
            "a substitution needs a ROS installation or the environment ($(find), $(env),"
            " $(optenv), $(dirname), $(cwd), an argument without a default): NotCovered"
            " (missing, warning)",
        ),
        _code(
            "xacro_undefined",
            "a property, macro or block the file does not define: left as declared, or dropped"
            " (missing)",
        ),
        _code(
            "xacro_unsupported",
            "a construct Neptune does not expand (xacro:element, xacro:attribute, other Python):"
            " left as declared, or dropped (unsupported)",
        ),
        _code("xml_malformed", "the bytes are not well-formed XML; nothing is read (error)"),
    ),
    locator_steps=(
        Documented(
            "urdf:expansion",
            "the Xacro expansion of the bytes the previous step addresses, as Neptune writes it"
            " (DescriptionExpansion gives its digest and size); the next step is a ByteRange"
            " into it. Field language: xacro",
        ),
    ),
    conventions=(
        Documented(
            "chunks",
            "one chunk per source, context {part: description}: a description is read whole",
        ),
        Documented(
            "citations",
            "a record cites its element, start tag to end tag: ByteRange in the source, or"
            " [ByteRange(0, size), urdf:expansion, ByteRange] in a Xacro expansion. Units,"
            " transform direction and Euler convention cite the <robot> element (the URDF"
            " specification); a parameter cites the element holding it",
        ),
        Documented(
            "expansion",
            "UTF-8 XML with a declaration; attributes in source order; whitespace between"
            " elements replaced by a newline and two spaces per level; other text exact;"
            " comments dropped. A value Xacro could not resolve keeps its declared text",
        ),
        Documented(
            "frames",
            "a FrameGraph per source; a frame per link, named as declared; a joint's transform"
            " maps child to parent (the child link's pose in the parent), translation in m,"
            " rpy as extrinsic XYZ Euler angles in rad; no <origin> is the identity",
        ),
        Documented(
            "parameters",
            "attributes are <path>/<attribute> and an element's stripped text is <path>, the"
            " path joining element names below the subject; visual, collision, material,"
            " plugin, transmission joint and actuator, and hardwareInterface are numbered from"
            " 0; Gazebo sensors number any repeated element. Numbers only where the URDF"
            " specification makes them numbers; all else is text. Unstated values are absent:"
            " the specification's defaults are not written",
        ),
        Documented(
            "units",
            "the URDF specification's: m, rad, kg, kg.m^2 for inertia, 1 for axis, scale and"
            " rgba; joint limit, calibration, mimic offset and safety limits in rad (revolute,"
            " continuous) or m (prismatic), effort N.m or N, velocity rad.s^-1 or m.s^-1,"
            " damping N.m.s.rad^-1 or N.s.m^-1, friction N.m or N, Unknown for other joint"
            " types; Gazebo blocks are text",
        ),
    ),
    resources=Resources(max_memory=512 * 1024 * 1024, streaming=False),
    security=(
        "Parses with the standard library's expat; a DOCTYPE or entity declaration stops the"
        " read, so no entity is ever expanded and no external resource is ever fetched.",
        "Nesting, element count, value length and source size are bounded; past a bound the"
        " source is a limit finding.",
        "Xacro expressions are walked as a closed set of Python AST nodes with bounded values;"
        " eval is never called. Includes, $(find) and the environment are never read.",
        "Macro depth, expansion steps and expansion size are bounded.",
    ),
)


def _root_name(head: bytes) -> str | None:
    """The name of the first element in ``head``, after the prolog, or None."""
    data, position = head.removeprefix(BOM), 0
    while True:
        while position < len(data) and data[position] in b" \t\r\n":
            position += 1
        if data.startswith(b"<?", position):
            end, closing = data.find(b"?>", position), 2
        elif data.startswith(b"<!--", position):
            end, closing = data.find(b"-->", position), 3
        elif data.startswith(b"<!", position):
            bracket, close = data.find(b"[", position), data.find(b">", position)
            internal = 0 <= bracket < close
            end, closing = (data.find(b"]>", position), 2) if internal else (close, 1)
        elif data.startswith(b"<", position):
            name = bytearray()
            for byte in data[position + 1 : position + 257]:
                if byte in b" \t\r\n/>":
                    break
                name.append(byte)
            return name.decode("utf-8", errors="replace") or None
        else:
            return None
        if end < 0:
            return None
        position = end + closing


def _finding(
    config: AdapterConfig,
    name: str,
    category: FindingCategory,
    severity: Severity,
    subject: EvidenceRef,
    message: str,
    details: dict[str, int | str] | None = None,
) -> IngestFinding:
    return ingest_finding(
        code=f"{ADAPTER_ID}.{name}",
        category=category,
        severity=severity,
        subject=subject,
        transform=config.transform,
        message=message,
        details=details,
    )


def _bytes_of(source: SourceReader, element: Element) -> EvidenceRef:
    return EvidenceRef(source.content_id, (ByteRange(element.start, element.end - element.start),))


class UrdfAdapter:
    """URDF and Xacro descriptions, read whole: one chunk per source."""

    descriptor = DESCRIPTOR

    def probe(self, head: bytes, hints: ProbeHints) -> ProbeResult:
        root = _root_name(head)
        if root == "robot":
            reasons = [ProbeReason("urdf.robot_root", "the root element is <robot>")]
            if XACRO_NAMESPACE in head or b"<xacro:" in head:
                reasons.append(ProbeReason("urdf.xacro", "the head declares or uses Xacro"))
            return ProbeResult(STRUCTURE, tuple(reasons))
        if head and hints.name.lower().endswith((".urdf", ".xacro")):
            message = "the name ends .urdf or .xacro, but the head shows no <robot> root"
            return ProbeResult(NAME_ONLY, (ProbeReason("urdf.name_only", message),))
        return ProbeResult(0.0, (ProbeReason("urdf.not_robot", "no <robot> root element"),))

    def inspect(self, source: SourceReader, config: AdapterConfig) -> InspectResult:
        head = source.read(0, min(source.size, PROBE_HEAD_SIZE))
        xacro_head = XACRO_NAMESPACE in head or b"<xacro:" in head
        return InspectResult(
            {"root": _root_name(head) or "", "size": source.size, "xacro": xacro_head}
        )

    def plan(self, source: SourceReader, config: AdapterConfig) -> Plan:
        return Plan((make_chunk(source, config, {"part": "description"}, source.size),))

    def ingest(self, source: SourceReader, chunk: Chunk, config: AdapterConfig) -> ChunkOutput:
        whole = EvidenceRef(source.content_id, (ByteRange(0, source.size),))
        max_bytes = config.integer("max_bytes")
        if source.size > max_bytes:
            return ChunkOutput(
                findings=(
                    _finding(
                        config,
                        "too_large",
                        FindingCategory.LIMIT,
                        Severity.ERROR,
                        whole,
                        f"the source holds {source.size} bytes, over max_bytes; it is not read",
                        {"bytes": source.size, "max_bytes": max_bytes},
                    ),
                )
            )
        data = b"".join(read_pieces(source, 0, source.size))
        limits = {"max_depth": min(config.integer("max_depth"), MAX_DEPTH)}
        limits["max_elements"] = config.integer("max_elements")
        try:
            root = parse(data, **limits)
        except XmlError as error:
            category, severity = _ERRORS[error.code]
            finding = _finding(
                config, error.code, category, severity, whole, error.message, error.details
            )
            return ChunkOutput(findings=(finding,))
        if root.tag != "robot":
            finding = _finding(
                config,
                "not_robot",
                FindingCategory.UNSUPPORTED,
                Severity.ERROR,
                _bytes_of(source, root),
                "the root element is not <robot>, so this is no URDF or Xacro description",
                {"root": root.tag[:64]},
            )
            return ChunkOutput(findings=(finding,))
        if not xacro.is_xacro(root):
            description = describe(root, lambda e: _bytes_of(source, e), config.transform)
            findings = list(description.findings.values())
            return ChunkOutput(tuple(description.records), (), _capped(config, whole, findings))
        return self._expand(source, config, root, whole, limits)

    def _expand(
        self,
        source: SourceReader,
        config: AdapterConfig,
        root: Element,
        whole: EvidenceRef,
        limits: dict[str, int],
    ) -> ChunkOutput:
        # The expansion is bounded absolutely and against the source, so a few hundred bytes of
        # macros cannot become max_bytes of records (amplification).
        ratio = config.integer("max_expansion_ratio")
        bound, most = "max_bytes", config.integer("max_bytes")
        if ratio * source.size < most:
            bound, most = "max_expansion_ratio times the source's size", ratio * source.size
        try:
            try:
                # Both stop as soon as their output passes the bound, so memory stays near it.
                expansion = xacro.expand(root, max_chars=most, chars_bound=bound, **limits)
                expanded = serialize(expansion.root, budget=most)
            except RecursionError:  # a safety net: the bounds keep well inside the limit
                raise xacro.ExpansionLimit("nests deeper than Python can follow", 0, root) from None
            except TooLarge:
                raise xacro.ExpansionLimit(f"is larger than {bound}", most, root) from None
        except xacro.ExpansionLimit as limit:
            finding = _finding(
                config,
                "limit_exceeded",
                FindingCategory.LIMIT,
                Severity.ERROR,
                _bytes_of(source, limit.element),
                f"the Xacro expansion {limit.what}; nothing of it is recorded",
                {"limit": limit.limit},
            )
            return ChunkOutput(findings=(finding,))
        transform = config.transform
        record = DescriptionExpansion(
            id=evidence_record_id(DescriptionExpansion.kind, whole, transform),
            # The digest and size are of the expansion the adapter made: observed.
            provenance=Provenance(whole, transform.id, AssertionKind.OBSERVED),
            language="xacro",
            digest=content_id(expanded),
            size=len(expanded),
            arguments=tuple(_argument(source, config, a) for a in expansion.arguments),
        )

        def cite(element: Element) -> EvidenceRef:
            inner = ByteRange(element.start, element.end - element.start)
            return EvidenceRef(source.content_id, (whole.locator[0], EXPANSION_STEP, inner))

        description = describe(expansion.root, cite, transform)
        findings = [
            _finding(
                config,
                problem.name,
                problem.category,
                problem.severity,
                _bytes_of(source, problem.element),
                problem.message,
                problem.details,
            )
            for problem in expansion.problems
        ]
        findings += description.findings.values()
        records = (record, *description.records)
        return ChunkOutput(records, (), _capped(config, whole, findings))


def _argument(
    source: SourceReader, config: AdapterConfig, argument: xacro.Argument
) -> DeclaredParameter:
    """A declared argument and the text the expansion used for it, citing its declaration."""
    cited = Provenance(
        _bytes_of(source, argument.element), config.transform.id, AssertionKind.STATED
    )
    value: Knowledge[ParameterValue]
    if argument.value is None:
        value = NotCovered(cited)
    elif argument.value.strip():
        text: ParameterValue = argument.value
        value = Known(text, cited)
    else:
        # A stated empty default (``default=""``, common for a name prefix) did expand as "", but
        # canonical text is never blank (``check_text``; ``Knowledge`` maps a blank to Unknown), so
        # Known("") cannot be recorded. Unknown here means "declared blank", cited to the
        # declaration; the expansion's digest still covers the "" it used.
        value = Unknown(cited)
    return DeclaredParameter(argument.name, value, NotApplicable())


def _capped(
    config: AdapterConfig, whole: EvidenceRef, findings: list[IngestFinding]
) -> tuple[IngestFinding, ...]:
    """Each finding once, in order; past ``MAX_FINDINGS``, the first ones and a count."""
    unique: dict[RecordId, IngestFinding] = {}
    for finding in findings:
        unique.setdefault(finding.id, finding)
    kept = list(unique.values())
    if len(kept) <= MAX_FINDINGS:
        return tuple(kept)
    capped = _finding(
        config,
        "findings_capped",
        FindingCategory.LIMIT,
        Severity.WARNING,
        whole,
        f"the source gave {len(kept)} findings; the first {MAX_FINDINGS} are kept",
        {"findings": len(kept), "kept": MAX_FINDINGS},
    )
    return (*kept[:MAX_FINDINGS], capped)
