"""The probe engine: bytes decide, ties and absences are findings, crashes are isolated."""

import importlib.util
import io
import sys
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from types import ModuleType
from typing import Final

import pytest

from neptune.adapters.builtin import builtin_adapters
from neptune.adapters.contract import (
    ABI_VERSION,
    GENERIC,
    SIGNATURE,
    VERIFIED,
    AdapterConfig,
    AdapterDescriptor,
    Chunk,
    ChunkOutput,
    FormatSpec,
    InspectResult,
    Magic,
    Plan,
    ProbeHints,
    ProbeReason,
    ProbeResult,
    Resources,
    SourceReader,
)
from neptune.adapters.registry import AdapterRegistry, SelectionStatus
from neptune.adapters.text import TextAdapter
from neptune.discovery.containers import ProbePolicy
from neptune.discovery.probe import (
    FINDING_CODES,
    PROBE_ID,
    PROBE_VERSION,
    ProbeEngine,
    SourceProbe,
    ask_in_process,
    hint_name,
)
from neptune.discovery.reader import BytesReader
from neptune.identity import canonical_json
from neptune.identity.findings import check_ingest_finding
from neptune.identity.provenance import check_transform_record
from neptune.model.finding import FindingCategory, Severity
from neptune.model.ids import ExternalObjectRef
from neptune.model.jsonvalue import JsonValue
from neptune.model.provenance import ByteRange, EvidenceRef
from neptune.model.source import LocalPath, RawLocalPath

FIXTURES: Final = Path(__file__).parents[2] / "fixtures"


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


TALLY: Final = _load("tally_adapter", FIXTURES / "adapters" / "tally_adapter.py")
GENERATOR: Final = _load("make_probe_fixtures", FIXTURES / "probe" / "make_probe_fixtures.py")
TALLY_BYTES: Final = b"TALLY1\n10 1\n20 2\n"


def descriptor(adapter_id: str, *formats: FormatSpec) -> AdapterDescriptor:
    return AdapterDescriptor(
        id=adapter_id,
        version="1.0.0",
        abi=ABI_VERSION,
        summary=f"The {adapter_id} stub.",
        formats=formats or (FormatSpec(adapter_id),),
        record_kinds=("document_record",),
        config=(),
        libraries=(),
        finding_codes=(),
        locator_steps=(),
        conventions=(),
        resources=Resources(0, True),
        security=(),
    )


@dataclass
class Stub:
    """An adapter whose probe is whatever the test says."""

    descriptor: AdapterDescriptor
    result: ProbeResult | Exception | object

    def probe(self, head: bytes, hints: ProbeHints) -> ProbeResult:
        if isinstance(self.result, Exception):
            raise self.result
        return self.result  # type: ignore[return-value]

    def inspect(self, source: SourceReader, config: AdapterConfig) -> InspectResult:
        raise NotImplementedError

    def plan(self, source: SourceReader, config: AdapterConfig) -> Plan:
        raise NotImplementedError

    def ingest(self, source: SourceReader, chunk: Chunk, config: AdapterConfig) -> ChunkOutput:
        raise NotImplementedError


def claims(adapter_id: str, confidence: float, *formats: FormatSpec) -> Stub:
    reason = ProbeReason(f"{adapter_id}.magic", "the stub's reason")
    return Stub(descriptor(adapter_id, *formats), ProbeResult(confidence, (reason,)))


def engine(*extra: object, policy: ProbePolicy | None = None) -> ProbeEngine:
    return ProbeEngine(AdapterRegistry([*builtin_adapters(), TALLY.TallyAdapter(), *extra]), policy)  # type: ignore[list-item]


def probe(
    data: bytes, name: str = "", *extra: object, policy: ProbePolicy | None = None
) -> SourceProbe:
    return engine(*extra, policy=policy).probe(BytesReader(data), name)


def codes(probed: SourceProbe) -> list[str]:
    return [finding.code.removeprefix(f"{PROBE_ID}.") for finding in probed.findings]


# --- Acceptance: the bytes decide --------------------------------------------------------------


def test_renamed_and_extensionless_sources_are_detected_from_their_bytes() -> None:
    log = (FIXTURES / "text" / "operator_log").read_bytes()
    assert probe(log, "operator_log").adapter == "text"
    assert probe(TALLY_BYTES, "renamed").adapter == "tally"
    assert probe(TALLY_BYTES, "lift.tally").adapter == "tally"
    assert probe(TALLY_BYTES, "misleading.txt").adapter == "tally"
    for index, (name, (expected, data)) in enumerate(GENERATOR.SIGNATURE_FILES.items()):
        renamed = f"{index:02d}_{Path(name).stem}"
        named, nameless = probe(data, name), probe(data, renamed)
        assert named.sniff == nameless.sniff
        assert named.selection == nameless.selection
        assert nameless.sniff.signatures[0].name == expected


def test_a_name_that_lies_is_an_info_finding_and_the_bytes_still_win() -> None:
    probed = probe(TALLY_BYTES, "misleading.txt")
    assert probed.adapter == "tally"
    (finding,) = probed.findings
    assert (finding.code, finding.category, finding.severity) == (
        f"{PROBE_ID}.name_mismatch",
        FindingCategory.INCONSISTENT,
        Severity.INFO,
    )
    assert finding.details == {"extension": ".txt", "name_suggests": ["text"], "selected": "tally"}
    assert probe(TALLY_BYTES, "lift.tally").findings == ()
    assert probe(TALLY_BYTES, "renamed").findings == ()
    assert probe(TALLY_BYTES, "data.unknown").findings == ()  # nobody declares .unknown


def test_the_longest_declared_extension_is_the_one_compared() -> None:
    gz = FormatSpec("Tarball", extensions=(".tar.gz",))
    archiver = claims("archiver", 0.0, gz)
    probed = probe((FIXTURES / "text" / "notes.txt").read_bytes(), "run.tar.gz", archiver)
    assert probed.adapter == "text"
    assert probed.findings[0].details["extension"] == ".tar.gz"


# --- Acceptance: conflicts are findings, never guesses ----------------------------------------


def test_a_tie_at_the_top_is_ambiguous_and_names_every_tied_adapter() -> None:
    rival = claims("rival", SIGNATURE, FormatSpec("Rival", magic=(Magic(0, b"TALLY1\n"),)))
    probed = probe(TALLY_BYTES, "lift.tally", rival)
    assert probed.selection.status is SelectionStatus.AMBIGUOUS
    assert probed.adapter is None
    (finding,) = probed.findings
    assert (finding.code, finding.category, finding.severity) == (
        f"{PROBE_ID}.ambiguous",
        FindingCategory.AMBIGUOUS,
        Severity.ERROR,
    )
    assert finding.subject == EvidenceRef(probed.source, (ByteRange(0, len(TALLY_BYTES)),))
    assert finding.details == {
        "adapters": ["rival", "tally"],
        "confidence": SIGNATURE,
        "reasons": {"rival": ["rival.magic"], "tally": ["tally.magic"]},
        "signatures": [
            {"adapter": "rival", "name": "Rival"},
            {"adapter": "tally", "name": "Tally"},
        ],
        "text": "utf8",
    }
    assert "rival, tally" in finding.message and "manifest" in finding.message
    assert [c.adapter for c in probed.selection.candidates] == ["rival", "tally", "text"]


def test_a_tie_below_the_top_is_not_ambiguous() -> None:
    weak = claims("weak", GENERIC)
    probed = probe(TALLY_BYTES, "x", weak)
    assert (probed.selection.status, probed.adapter) == (SelectionStatus.SELECTED, "tally")
    assert probed.findings == ()


def test_an_unclaimed_source_is_a_finding_that_says_what_was_seen() -> None:
    data = GENERATOR.SIGNATURE_FILES["capture.pcap"][1]
    probed = probe(data, "capture.pcap")
    assert probed.selection.status is SelectionStatus.UNSUPPORTED
    (finding,) = probed.findings
    assert (finding.code, finding.category, finding.severity) == (
        f"{PROBE_ID}.unsupported",
        FindingCategory.UNSUPPORTED,
        Severity.ERROR,
    )
    assert finding.message == "no adapter claims the source (pcap signature; binary)"
    assert finding.details == {
        "declined": {
            "mcap": ["mcap.no_magic"],
            "tabular": ["tabular.binary"],
            "tally": [],
            "text": ["text.nul"],
        },
        "signatures": [{"name": "pcap"}],
        "text": "binary",
    }
    assert finding.subject == EvidenceRef(probed.source, (ByteRange(0, len(data)),))
    assert [(c.adapter, c.confidence) for c in probed.probes] == [
        ("mcap", 0.0),
        ("tabular", 0.0),
        ("tally", 0.0),
        ("text", 0.0),
    ]


def test_an_unclaimed_source_whose_name_belongs_to_an_adapter_says_it_declined() -> None:
    probed = probe(b"\x00\x01\x02 not a tally", "lift.tally")
    (finding,) = probed.findings
    assert finding.message.endswith("; the name suggests tally, which declined")
    assert finding.details["name_suggests"] == ["tally"]
    assert finding.details["extension"] == ".tally"


def test_an_unclaimed_source_with_no_text_adapter_is_named_by_its_text_class() -> None:
    probed = ProbeEngine(AdapterRegistry([TALLY.TallyAdapter()])).probe(BytesReader(b"notes\n"))
    assert probed.findings[0].message == "no adapter claims the source (no known signature; utf8)"


# --- Isolation -----------------------------------------------------------------------------------


def test_a_crashing_probe_is_a_finding_and_the_others_still_choose() -> None:
    broken = Stub(descriptor("broken"), RuntimeError("boom at 0x7f3a"))
    probed = probe(TALLY_BYTES, "lift.tally", broken)
    assert probed.adapter == "tally"
    (finding,) = probed.findings
    assert (finding.code, finding.category, finding.severity) == (
        f"{PROBE_ID}.adapter_failed",
        FindingCategory.FAILED,
        Severity.ERROR,
    )
    assert finding.details == {"adapter": "broken", "error": "RuntimeError", "version": "1.0.0"}
    assert "0x7f3a" not in finding.message  # the exception's text never enters a record
    assert [c.adapter for c in probed.probes] == ["mcap", "tabular", "tally", "text"]


def test_a_probe_returning_the_wrong_type_is_a_failure_too() -> None:
    wrong = Stub(descriptor("wrong"), 0.9)
    probed = probe(TALLY_BYTES, "x", wrong)
    assert probed.adapter == "tally"
    assert probed.findings[0].details["error"] == "TypeError"


def test_a_reader_that_cannot_give_the_head_is_an_error_not_a_finding() -> None:
    class Short(BytesReader):
        def read(self, offset: int, length: int) -> bytes:
            return super().read(offset, length)[:3]

    with pytest.raises(ValueError, match="head bytes"):
        engine().probe(Short(TALLY_BYTES))


# --- The engine as a producer -------------------------------------------------------------------


def test_the_engine_has_a_transform_whose_config_is_the_policy() -> None:
    default, strict = engine(), engine(policy=ProbePolicy(max_members=5))
    for probing in (default, strict):
        check_transform_record(probing.transform)
        assert (probing.transform.adapter_id, probing.transform.adapter_version) == (
            PROBE_ID,
            PROBE_VERSION,
        )
        assert probing.transform.config == probing.policy.to_json()
    assert default.transform.id != strict.transform.id
    first = default.probe(BytesReader(b"\x00 binary"), "b").findings[0]
    second = strict.probe(BytesReader(b"\x00 binary"), "b").findings[0]
    assert first.transform == default.transform.id and second.transform == strict.transform.id
    assert first.id != second.id  # another policy is another lineage
    assert first.message == second.message


def test_every_finding_is_well_formed_declared_and_cites_the_source() -> None:
    declared = {code.name for code in FINDING_CODES}
    seen: set[str] = set()
    corpus = sorted((FIXTURES / "probe" / "containers").iterdir())
    corpus += [FIXTURES / "probe" / "signatures" / "cloud.zst", FIXTURES / "text" / "notes.txt"]
    rival = claims("rival", SIGNATURE, FormatSpec("Rival", magic=(Magic(0, b"TALLY1\n"),)))
    broken = Stub(descriptor("broken"), RuntimeError("boom"))
    for path in corpus:
        probed = probe(path.read_bytes(), path.name, broken)
        for finding in probed.findings:
            check_ingest_finding(finding)
            assert finding.code in declared
            assert finding.transform == engine(broken).transform.id
            assert isinstance(finding.subject, EvidenceRef)
            assert finding.subject.source == probed.source
            seen.add(finding.code)
    probed = probe(TALLY_BYTES, "lift.tally", rival)
    seen.update(f.code for f in probed.findings)
    probed = probe(TALLY_BYTES, "misleading.txt")
    seen.update(f.code for f in probed.findings)
    zipped = (FIXTURES / "probe" / "containers" / "members.zip").read_bytes()
    reader = BytesReader(zipped)
    fallback = engine().probe_head(
        reader.content_id, reader.size, "bundle", zipped, ask_in_process, {"signal": "SIGSEGV"}
    )
    seen.update(f.code for f in fallback.findings)
    assert seen == declared  # every documented code is exercised, and nothing undocumented is


def test_the_policy_is_validated() -> None:
    for bad in (
        {"max_members": 0},
        {"max_depth": -1},
        {"scan_bytes": 1024},
        {"max_ratio": 0},
        {"max_members": True},
    ):
        with pytest.raises(ValueError):
            ProbePolicy(**bad)
    assert ProbePolicy().to_json() == {
        "max_depth": 2,
        "max_members": 1000,
        "max_ratio": 1000,
        "scan_bytes": 1024 * 1024,
    }


# --- Determinism and explanation ------------------------------------------------------------------


def test_probing_twice_gives_identical_canonical_json() -> None:
    for path in sorted((FIXTURES / "probe" / "containers").iterdir()):
        data = path.read_bytes()
        first = canonical_json.dumps(probe(data, path.name).to_json())
        second = canonical_json.dumps(probe(data, path.name).to_json())
        assert first == second


def obj(value: JsonValue) -> Mapping[str, JsonValue]:
    assert isinstance(value, Mapping)
    return value


def arr(value: JsonValue) -> Sequence[JsonValue]:
    assert isinstance(value, Sequence) and not isinstance(value, str)
    return value


def test_the_explanation_holds_every_probe_the_selection_the_sniff_and_the_container() -> None:
    probed = probe((FIXTURES / "probe" / "containers" / "members.zip").read_bytes(), "members.zip")
    explanation = probed.to_json()
    assert set(explanation) == {
        "container",
        "findings",
        "name",
        "probes",
        "selection",
        "size",
        "sniff",
        "source",
    }
    assert explanation["selection"] == {"candidates": [], "status": "unsupported"}
    assert [obj(p)["adapter"] for p in arr(explanation["probes"])] == [
        "mcap",
        "tabular",
        "tally",
        "text",
    ]
    members = arr(obj(explanation["container"])["members"])
    assert obj(obj(obj(members[2])["probe"])["selection"])["adapter"] == "tally"
    assert obj(members[4])["name"] == "../escape.txt"
    text = probe(TALLY_BYTES, "lift.tally").to_json()
    assert obj(text["selection"])["adapter"] == "tally"
    assert "container" not in text


def test_names_that_are_not_utf8_are_kept_as_hex_in_explanations() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        info = zipfile.ZipInfo("placeholder", (1980, 1, 1, 0, 0, 0))
        info.filename = "x"
        archive.writestr(info, TALLY_BYTES)
    data = buffer.getvalue().replace(b"x", b"\xff", 2)  # both headers: a one-byte non-UTF-8 name
    probed = probe(data, "names.zip")
    assert probed.container is not None
    member = probed.container.members[0]
    assert member.name == b"\xff"
    assert member.to_json()["name_hex"] == "ff"
    canonical_json.dumps(probed.to_json())


def test_hint_names_are_the_last_component_as_text() -> None:
    assert hint_name(LocalPath("runs/day1/imu.mcap")) == "imu.mcap"
    assert hint_name(RawLocalPath(b"runs/bad\xff/log\xfe")) == "log�"
    assert hint_name(ExternalObjectRef("s3", "bucket/key/file.bag", "v1")) == "file.bag"


def test_the_committed_fixtures_are_what_the_generator_builds() -> None:
    built = GENERATOR.build()
    on_disk = {
        str(path.relative_to(FIXTURES / "probe")): path.read_bytes()
        for path in (FIXTURES / "probe").rglob("*")
        if path.is_file() and path.suffix != ".py" and "__pycache__" not in path.parts
    }
    # On failure, run `uv run python tests/fixtures/probe/make_probe_fixtures.py`.
    assert on_disk == built


def test_the_text_adapter_alone_still_claims_damaged_text_so_nothing_is_silently_dropped() -> None:
    probed = ProbeEngine(AdapterRegistry([TextAdapter()])).probe(BytesReader(b"abc\xff"), "x")
    assert probed.adapter == "text"
    assert probed.sniff.text == "damaged_utf8"


def test_what_a_descriptor_declares_does_not_decide_the_probe_does() -> None:
    # A stub whose descriptor claims tally files every way a descriptor can (summary, extension,
    # magic) yet whose probe declines the bytes does not get the source. Its declared magic is
    # sniffed, an observation in the explanation, and nothing more.
    claims_tally = FormatSpec("Tally too", extensions=(".tally",), magic=(Magic(0, b"TALLY1\n"),))
    declines = Stub(
        replace(descriptor("declines", claims_tally), summary="Reads every tally file."),
        ProbeResult(0.0, ()),
    )
    probed = probe(TALLY_BYTES, "lift.tally", declines)
    assert probed.adapter == "tally"
    assert probed.findings == ()
    assert ("Tally too", "declines") in {(s.name, s.adapter) for s in probed.sniff.signatures}
    # The converse: a stub declaring nothing about tally files, whose probe verifies the bytes,
    # takes the source from the adapter the name and the magic point to; the name is a footnote.
    quiet = claims("quiet", VERIFIED, FormatSpec("Quiet"))
    probed = probe(TALLY_BYTES, "lift.tally", quiet)
    assert probed.adapter == "quiet"
    assert codes(probed) == ["name_mismatch"]
