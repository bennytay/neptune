"""Camera, IMU and sensor-extrinsic calibration files as ``Calibration`` records (ADR 0055).

What it reads, each claimed by the keys it must hold (``_formats``), never by name:

- ROS ``camera_info`` YAML (``camera_calibration_parsers``) and ``sensor_msgs/CameraInfo`` dumped
  as YAML or JSON;
- Kalibr ``camchain`` / ``camchain-imucam`` and IMU YAML;
- OpenCV ``cv::FileStorage`` YAML and XML (tutorial, stereo and Autoware-style names).

What it emits: one ``Calibration`` per calibrated subject, its numbers under their declared names in
source order; for Kalibr's ``T_cam_imu`` and ``T_cn_cnm1`` a ``FrameTransform`` in one
``FrameGraph`` for the file; findings for a transform whose frames or numbers the file does not
give, a matrix that does not hold its declared size, a camera without an extrinsic, a frame graph
with a loop, a repeat or a gap. Nothing is converted: units, quaternion order, matrix layout and
direction are as the file declares them, or ``Unknown``.

TF from bags and MCAP is not read here: the stream adapters do not decode ``/tf_static`` payloads.
Binding a calibration to hardware and runs (MVL-38) and aligning its frames with a URDF's (MVL-37)
are theirs; so is whether two transforms agree, which is computed (``validate/``, ``derived/``).
"""

import re
import sys
from collections.abc import Iterator
from typing import Final
from xml.parsers import expat

import yaml

from neptune.adapters.calibration._codes import ADAPTER_ID, code
from neptune.adapters.calibration._emit import Emitter
from neptune.adapters.calibration._formats import (
    HINT,
    CalibrationFormat,
    Recognised,
    recognise,
)
from neptune.adapters.calibration._items import (
    Item,
    XmlLimits,
    XmlRefused,
    from_document,
    read_xml,
)
from neptune.adapters.contract import (
    ABI_VERSION,
    PROBE_HEAD_SIZE,
    SIGNATURE,
    VERIFIED,
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
from neptune.adapters.structured.load import (
    MIB,
    Problem,
    Settings,
    load,
    problem_finding,
    problems,
)
from neptune.adapters.structured.reader import read_text
from neptune.adapters.structured.text import InvalidEncoding, decode, detect
from neptune.adapters.structured.tree import Limits
from neptune.identity.findings import ingest_finding
from neptune.model.finding import FindingCategory, IngestFinding, Severity
from neptune.model.machine import Calibration
from neptune.model.provenance import ByteRange, EvidenceRef, Span
from neptune.model.reference import FrameGraph, FrameTransform

PYTHON: Final = f"{sys.version_info.major}.{sys.version_info.minor}"
OPENCV_HEADER: Final = "%YAML:"
MAX_DEPTH: Final = 200
_XML_START: Final = re.compile(rb"(?:\xef\xbb\xbf)?\s*<")


DESCRIPTOR: Final = AdapterDescriptor(
    id=ADAPTER_ID,
    version="0.1.0",
    abi=ABI_VERSION,
    summary="Camera, IMU and sensor-extrinsic calibration files: ROS, Kalibr and OpenCV.",
    formats=(
        FormatSpec(
            "ROS camera_info calibration, Kalibr and OpenCV YAML",
            media_types=("application/yaml",),
            extensions=(".yaml", ".yml"),
        ),
        FormatSpec(
            "ROS CameraInfo as JSON", media_types=("application/json",), extensions=(".json",)
        ),
        FormatSpec(
            "OpenCV FileStorage XML", media_types=("application/xml",), extensions=(".xml",)
        ),
    ),
    record_kinds=(Calibration.kind, FrameGraph.kind, FrameTransform.kind),
    config=(
        ConfigOption(
            "max_array_values",
            100_000,
            "an array of more numbers than this is not read; its parameter is Unknown"
            " (calibration.array_too_large)",
        ),
        ConfigOption(
            "max_bytes", 8 * MIB, "a file larger than this is not read (calibration.too_large)"
        ),
        ConfigOption(
            "max_depth",
            MAX_DEPTH,
            "a document nested deeper than this is not read (calibration.too_deep)",
        ),
        ConfigOption(
            "max_items",
            200_000,
            "a document of more values or elements than this is not read"
            " (calibration.too_many_values)",
        ),
        ConfigOption(
            "max_path_ratio",
            64,
            "a document whose values' paths total more code points than this many times its own"
            " (at least 4 KiB) is not read (calibration.paths_too_long)",
        ),
        ConfigOption(
            "max_scalar_length",
            MIB,
            "a scalar or key of more code points than this is not read; its parameter is"
            " Unknown (calibration.value_not_read)",
        ),
        ConfigOption(
            "yaml_version",
            "declared",
            "the YAML version a document without a %YAML directive is typed by: declared reads"
            " both 1.1 and 1.2 and keeps both readings where they differ",
            choices=("1.1", "1.2", "declared"),
        ),
    ),
    libraries=(("expat", expat.EXPAT_VERSION), ("python", PYTHON), ("pyyaml", yaml.__version__)),
    finding_codes=(
        Documented(
            code("ambiguous_value"),
            "scalars YAML 1.1 and 1.2 read differently (1e-3 is text in 1.1); the parameter is"
            " Ambiguous with each reading (ambiguous, warning)",
        ),
        Documented(
            code("array_too_large"),
            "an array of more than max_array_values numbers; its parameter is Unknown (limit,"
            " warning)",
        ),
        Documented(
            code("byte_order_mark"),
            "a JSON file starts with a byte-order mark its format does not define; read past"
            " (inconsistent, info)",
        ),
        Documented(
            code("dtd_refused"),
            "an XML file declares a DTD or an entity, which is never read; nothing is read"
            " (unsupported, error)",
        ),
        Documented(
            code("duplicate_key"),
            "keys repeat in a mapping of an entry; each parameter is kept, named by its position"
            " (inconsistent, warning)",
        ),
        Documented(
            code("duplicate_subject"),
            "several calibrations of one file state the same subject; each is kept and none"
            " replaces another (inconsistent, warning)",
        ),
        Documented(
            code("entries_not_read"),
            "top-level keys of a recognised document that belong to no calibration entry (a"
            " Kalibr file's other keys); they are not read (unsupported, info)",
        ),
        Documented(
            code("extrinsic_missing"),
            "a Kalibr camera declares no T_cam_imu or T_cn_cnm1 where other cameras of the file"
            " do; the first camera has no T_cn_cnm1 (missing, info)",
        ),
        Documented(
            code("extrinsic_not_read"),
            "an extrinsic matrix is not four rows of four finite numbers; no transform is"
            " emitted and its rows stay parameters (unrepresentable, warning)",
        ),
        Documented(
            code("frame_graph_disconnected"),
            "the file's transforms form separate groups of frames (missing, warning)",
        ),
        Documented(
            code("frame_loop"),
            "transforms join frames the others already connect: more than one declared path"
            " between frames, which may disagree (inconsistent, info)",
        ),
        Documented(
            code("frame_transform_repeated"),
            "several transforms join the same two frames; each is kept (inconsistent, warning)",
        ),
        Documented(
            code("frame_unresolved"),
            "an extrinsic names a frame the file does not (T_cn_cnm1 without the camera before"
            " it), or names no frame (OpenCV R, T, CameraExtrinsicMat); no transform is emitted,"
            " the numbers stay parameters (missing, warning or info)",
        ),
        Documented(
            code("invalid_encoding"),
            "the bytes are not valid UTF-8 (or the encoding their mark names); nothing is read"
            " (corrupt, error)",
        ),
        Documented(
            code("mixed_line_endings"),
            "the file mixes LF, CR LF and lone CR line breaks (inconsistent, info)",
        ),
        Documented(
            code("no_document"),
            "the file is empty, blank or only comments (missing, info)",
        ),
        Documented(
            code("non_finite_value"),
            "parameters holding NaN or an infinity, kept as declared (inconsistent, warning)",
        ),
        Documented(
            code("not_calibration"),
            "a document of the file is none of the formats read, or an entry states nothing"
            " (missing, info or warning)",
        ),
        Documented(
            code("paths_too_long"),
            "a document whose values' paths total more than max_path_ratio times its size; not"
            " read (limit, error)",
        ),
        Documented(
            code("shape_mismatch"),
            "a matrix with rows, cols and data whose data holds other than rows x cols x"
            " channels numbers (inconsistent, warning)",
        ),
        Documented(
            code("syntax_error"),
            "the text is not YAML, JSON, TOML or XML from the cited place on; not read"
            " (corrupt, error)",
        ),
        Documented(
            code("too_deep"),
            "a document nested deeper than max_depth; not read (limit, error)",
        ),
        Documented(
            code("too_large"),
            "a file over max_bytes; not read (limit, error)",
        ),
        Documented(
            code("too_many_values"),
            "a document of more than max_items values or elements; not read (limit, error)",
        ),
        Documented(
            code("value_not_read"),
            "values no record holds as text or numbers (a YAML alias, an application tag, a"
            " number beyond binary64, a scalar over max_scalar_length); each parameter is"
            " Unknown (unsupported, warning)",
        ),
        Documented(
            code("yaml_version_unsupported"),
            "a %YAML directive names a version other than 1.1 or 1.2; typed as yaml_version says"
            " (unsupported, warning)",
        ),
    ),
    locator_steps=(),
    conventions=(
        Documented(
            "chunks",
            "one chunk per file: calibrations are small and a file's frame graph is read whole",
        ),
        Documented(
            "claims",
            "VERIFIED where the whole file parses and holds a format's required keys (ROS:"
            " camera_matrix{data} and one of distortion_model, distortion_coefficients,"
            " projection_matrix, image_width; ROS message: K, P, distortion_model; Kalibr: a"
            " camN with camera_model and intrinsics, or an imuN with a noise density; OpenCV:"
            " the %YAML:1.0 header, an opencv-matrix or an opencv_storage root, and a camera"
            " matrix or distortion name); SIGNATURE for the head of a file over 64 KiB; never"
            " from a name. This beats the config adapter's STRUCTURE claim on the same bytes",
        ),
        Documented(
            "direction",
            "Kalibr T_a_b maps b's coordinates into a's (Kalibr's documented meaning): the entry"
            " is the parent, the frame the key names the child, direction child_to_parent,"
            " Known citing the matrix. Extrinsics with unnamed frames, or whose direction no key"
            " states, are never transforms",
        ),
        Documented(
            "frames",
            "Kalibr's frames are the entry's key (cam0) and, from the key, imu for T_cam_imu or"
            " the camera before it (camN-1, if the file has it) for T_cn_cnm1; one FrameGraph"
            " per file, scope (), cited as the whole file. Names are verbatim: /imu and imu are"
            " two frames. No Frame record: no format here declares axes",
        ),
        Documented(
            "kinds",
            "calibration, frame_graph, frame_transform; no record kind is new",
        ),
        Documented(
            "matrices",
            "a 4x4 extrinsic is a HomogeneousMatrix of sixteen floats, row-major by its nesting,"
            " translation unit Unknown (no file states one), validity static. Rotation is never"
            " split out, normalised or converted",
        ),
        Documented(
            "opencv",
            "the %YAML:1.0 header is not YAML: its % is read as # (same length, so spans stay"
            " exact). A matrix is its rows, cols, dt and data parameters. XML values are typed"
            " as OpenCV reads them (an integer, a real, .nan and .inf, else text); a locator is"
            " the element's byte range, a data array's numbers cited by its element",
        ),
        Documented(
            "parameters",
            "named by key path from the entry, '/'-joined with RFC 6901 escapes; a sequence of"
            " numbers is one parameter in source order, any other sequence is its items by"
            " position; ints are read with float(); text keeps its text, a null is KnownAbsent."
            " Units are Unknown for numbers, NotApplicable for text",
        ),
        Documented(
            "subjects",
            "ROS: camera_name (message form: header.frame_id) as written, Unknown if absent;"
            " Kalibr: the camN or imuN key; OpenCV: Unknown. Machine, hardware revision and"
            " times are Unknown: binding is MVL-38's",
        ),
        Documented(
            "yaml",
            "read by the structured readers the config adapter shares (ADR 0055): events only,"
            " aliases never expanded, the declared YAML version or both readings",
        ),
    ),
    resources=Resources(max_memory=1024 * MIB, streaming=False),
    security=(
        "Never constructs YAML objects and never expands an alias: only parser events are read.",
        "XML: a DTD or entity is refused, nesting and element counts are bounded; expat only.",
        "Reads at most max_bytes, nests at most max_depth, holds at most max_items values and"
        " max_array_values numbers per array; everything past them is a finding.",
    ),
)


def _settings(config: AdapterConfig) -> Settings:
    return Settings(
        config.integer("max_bytes"),
        config.integer("max_depth"),
        config.integer("max_path_ratio"),
        config.integer("max_scalar_length"),
        config.text("yaml_version"),
    )


def _opencv_rewrite(text: str) -> str:
    """OpenCV's ``%YAML:1.0`` header is no YAML directive: read its ``%`` as ``#``."""
    return "#" + text[1:] if text.startswith(OPENCV_HEADER) else text


def _xml_limits(config: AdapterConfig, settings: Settings, size: int) -> XmlLimits:
    return XmlLimits(
        settings.max_depth,
        config.integer("max_items"),
        config.integer("max_array_values"),
        settings.max_scalar_length,
        settings.max_path_ratio * max(size, 4096),
    )


def _is_xml(head: bytes) -> bool:
    return _XML_START.match(head) is not None


# --- Reading a source --------------------------------------------------------------------------


class _Documents:
    """The calibration documents of one source, and the problems reading it met."""

    def __init__(self) -> None:
        self.recognised: list[tuple[Recognised, Item]] = []
        self.problems: list[Problem] = []
        self.unrecognised: list[tuple[int, ByteRange | Span]] = []


def _problem(
    name: str, category: FindingCategory, severity: Severity, size: int, message: str
) -> Problem:
    return Problem(name, category, severity, ByteRange(0, size), message)


def _read(source: SourceReader, config: AdapterConfig) -> _Documents:
    found = _Documents()
    settings = _settings(config)
    head = source.read(0, min(source.size, 64))
    if _is_xml(head):
        _read_xml(source, config, settings, found)
        return found
    loaded = load(source, settings, None, _opencv_rewrite)
    found.problems.extend(problems(source.size, loaded, settings))
    parse = loaded.parse
    if parse is None:
        return found
    opencv = loaded.text.startswith("#YAML:") and _opencv_header(head)
    for document in parse.documents:
        if len(document.nodes) > config.integer("max_items"):
            found.problems.append(
                Problem(
                    "too_many_values",
                    FindingCategory.LIMIT,
                    Severity.ERROR,
                    Span(*document.extent),
                    f"document {document.index} holds more than max_items"
                    f" ({config.integer('max_items')}) values; it is not read",
                    {"document": document.index, "max_items": config.integer("max_items")},
                )
            )
            continue
        root = from_document(document)
        if root is None:
            continue
        recognised = recognise(root, opencv=opencv)
        if recognised is None:
            found.unrecognised.append((document.index, Span(*document.extent)))
        else:
            found.recognised.append((recognised, root))
    return found


def _opencv_header(head: bytes) -> bool:
    decoded = decode(head, final=False)
    return not isinstance(decoded, InvalidEncoding) and decoded.text.startswith(OPENCV_HEADER)


def _read_xml(
    source: SourceReader, config: AdapterConfig, settings: Settings, found: _Documents
) -> None:
    size = source.size
    if size > settings.max_bytes:
        found.problems.append(
            _problem(
                "too_large",
                FindingCategory.LIMIT,
                Severity.ERROR,
                size,
                f"the file holds {size} bytes, over max_bytes ({settings.max_bytes}); not read",
            )
        )
        return
    data = b"".join(read_pieces(source, 0, size))
    try:
        root = read_xml(data, _xml_limits(config, settings, size))
    except XmlRefused as refused:
        category = {
            "syntax_error": FindingCategory.CORRUPT,
            "dtd_refused": FindingCategory.UNSUPPORTED,
        }.get(refused.name, FindingCategory.LIMIT)
        found.problems.append(
            _problem(refused.name, category, Severity.ERROR, size, f"not read: {refused.message}")
        )
        return
    except RecursionError:
        found.problems.append(
            _problem("too_deep", FindingCategory.LIMIT, Severity.ERROR, size, "nesting too deep")
        )
        return
    recognised = recognise(root, opencv=root.name == "opencv_storage", xml=True)
    if recognised is None:
        found.unrecognised.append((0, ByteRange(0, size)))
    else:
        found.recognised.append((recognised, root))


def _findings(
    source: SourceReader, config: AdapterConfig, found: _Documents
) -> Iterator[IngestFinding]:
    for problem in found.problems:
        yield problem_finding(ADAPTER_ID, source.content_id, config.transform, problem)
    for index, where in found.unrecognised:
        yield ingest_finding(
            code=code("not_calibration"),
            category=FindingCategory.MISSING,
            severity=Severity.INFO,
            subject=EvidenceRef(source.content_id, (where,)),
            transform=config.transform,
            message=f"document {index} is none of the calibration formats read"
            " (ROS camera_info, Kalibr, OpenCV FileStorage)",
            details={"document": index},
        )


# --- The adapter -------------------------------------------------------------------------------


class CalibrationAdapter:
    """ROS, Kalibr and OpenCV calibration files; one chunk per file."""

    descriptor = DESCRIPTOR

    def probe(self, head: bytes, hints: ProbeHints) -> ProbeResult:
        if not head:
            return ProbeResult(0.0, (ProbeReason(code("empty"), "the source is empty"),))
        complete = len(head) >= hints.size
        if _is_xml(head):
            return self._probe_xml(head, complete)
        decoded = decode(head, final=complete)
        if isinstance(decoded, InvalidEncoding):
            reason = ProbeReason(code("not_text"), f"byte {decoded.offset} is not valid text")
            return ProbeResult(0.0, (reason,))
        text = decoded.text
        if HINT.search(text) is None:
            return ProbeResult(0.0, (ProbeReason(code("no_keys"), "no calibration key"),))
        if not complete:  # a head ends where it ends: read to its last whole line
            cut = max(text.rfind("\n"), text.rfind("\r"))
            text = text[: cut + 1] if cut >= 0 else text
        opencv = text.startswith(OPENCV_HEADER)
        limits = Limits(MAX_DEPTH, MIB)
        parse = read_text(_opencv_rewrite(text), decoded.encoding, limits, "declared")
        for document in parse.documents if parse is not None else ():
            root = from_document(document)
            recognised = recognise(root, opencv=opencv) if root is not None else None
            if recognised is not None:
                return _claim(recognised.format, complete)
        return ProbeResult(0.0, (ProbeReason(code("not_calibration"), "no format's keys"),))

    @staticmethod
    def _probe_xml(head: bytes, complete: bool) -> ProbeResult:
        if b"<opencv_storage" not in head:
            return ProbeResult(0.0, (ProbeReason(code("not_calibration"), "not opencv_storage"),))
        if not complete:
            text = head.decode("utf-8", "replace")
            if HINT.search(text) is None:
                return ProbeResult(0.0, (ProbeReason(code("no_keys"), "no calibration key"),))
            return _claim(CalibrationFormat.OPENCV_XML, complete)
        try:
            root = read_xml(head, XmlLimits(MAX_DEPTH, 200_000, 100_000, MIB, 64 * len(head)))
        except (XmlRefused, RecursionError):
            return ProbeResult(0.0, (ProbeReason(code("not_xml"), "not read as XML"),))
        recognised = recognise(root, opencv=True, xml=True)
        if recognised is None:
            return ProbeResult(0.0, (ProbeReason(code("not_calibration"), "no calibration key"),))
        return _claim(recognised.format, complete)

    def inspect(self, source: SourceReader, config: AdapterConfig) -> InspectResult:
        head = source.read(0, min(source.size, PROBE_HEAD_SIZE))
        encoding, bom = detect(head)
        return InspectResult(
            {
                "bom": bom > 0,
                "encoding": str(encoding),
                "opencv_header": _opencv_header(head),
                "size": source.size,
                "xml": _is_xml(head),
            }
        )

    def plan(self, source: SourceReader, config: AdapterConfig) -> Plan:
        # One chunk, and no read: a file is read once, by the chunk, so its findings are the
        # chunk's. A source nothing of which is a calibration is that chunk's findings only.
        return Plan((make_chunk(source, config, {"part": "file"}, source.size),))

    def ingest(self, source: SourceReader, chunk: Chunk, config: AdapterConfig) -> ChunkOutput:
        found = _read(source, config)
        emitter = Emitter(
            source.content_id, source.size, config, config.integer("max_array_values")
        )
        for recognised, root in found.recognised:
            emitter.document(recognised, root)
        output = emitter.finish()
        findings = (*_findings(source, config, found), *output.findings)
        return ChunkOutput(records=tuple(output.records), findings=findings)


def _claim(fmt: CalibrationFormat, complete: bool) -> ProbeResult:
    read = "the source" if complete else "the head of the source"
    reason = ProbeReason(code(str(fmt)), f"{read} holds the keys of a {fmt} calibration")
    return ProbeResult(VERIFIED if complete else SIGNATURE, (reason,))


__all__ = ["DESCRIPTOR", "CalibrationAdapter"]
