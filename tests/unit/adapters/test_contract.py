"""The adapter ABI's types (ADR 0024): descriptors, config, probes, chunks and plans."""

from dataclasses import replace
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from neptune.adapters.contract import (
    ABI_VERSION,
    EVIDENCE_KINDS,
    AdapterDescriptor,
    Chunk,
    ChunkId,
    ConfigError,
    ConfigOption,
    ContractError,
    Documented,
    FormatSpec,
    InspectResult,
    Magic,
    Plan,
    ProbeHints,
    ProbeReason,
    ProbeResult,
    Resources,
    ShortReadError,
    chunk_from_json,
    chunk_id,
    configure,
    make_chunk,
    read_pieces,
)
from neptune.discovery.reader import BytesReader
from neptune.identity.ids import config_hash
from neptune.model.ids import ContentId
from neptune.model.kinds import RECORD_KINDS


def descriptor(**changes: Any) -> AdapterDescriptor:
    base = AdapterDescriptor(
        id="demo",
        version="1.2.3",
        abi=ABI_VERSION,
        summary="A demo adapter.",
        formats=(FormatSpec("Demo", extensions=(".demo",), magic=(Magic(0, b"DEMO"),)),),
        record_kinds=("document_record",),
        config=(
            ConfigOption("mode", "fast", "how to read", choices=("fast", "slow")),
            ConfigOption("ratio", 0.5, "a ratio"),
            ConfigOption("strict", False, "refuse oddities"),
        ),
        libraries=(("demo-lib", "2.0"),),
        finding_codes=(Documented("demo.bad", "something was bad"),),
        locator_steps=(Documented("demo:frame", "a frame of the demo format"),),
        conventions=(),
        resources=Resources(max_memory=1024, streaming=True),
        security=(),
    )
    return replace(base, **changes)


# --- Descriptor --------------------------------------------------------------------------------


def test_a_valid_descriptor_describes_itself_as_json() -> None:
    data = descriptor().to_json()
    assert data["id"] == "demo"
    assert data["version"] == "1.2.3"
    assert data["libraries"] == {"demo-lib": "2.0"}
    assert data["formats"] == [
        {
            "extensions": [".demo"],
            "magic": [{"data_hex": "44454d4f", "offset": 0}],
            "media_types": [],
            "name": "Demo",
        }
    ]


@pytest.mark.parametrize(
    "changes",
    [
        {"id": "Demo"},
        {"id": "demo:x"},
        {"version": "1.2"},
        {"version": "v1.2.3"},
        {"summary": "two\nlines"},
        {"formats": ()},
        {"record_kinds": ()},
        {"record_kinds": ("transform_record",)},
        {"record_kinds": ("ingest_finding",)},
        {"record_kinds": ("source_artifact",)},
        {"record_kinds": ("run", "document_record")},
        {"record_kinds": ("no_such_kind",)},
        {"finding_codes": (Documented("other.bad", "another producer's code"),)},
        {"finding_codes": (Documented("demo.Bad", "not a token"),)},
        {"finding_codes": (Documented("demo.b", "b"), Documented("demo.a", "a"))},
        {"locator_steps": (Documented("other:frame", "another producer's step"),)},
        {"locator_steps": (Documented("demo.frame", "a dot, not a colon"),)},
        {"config": (ConfigOption("b", 1, "b"), ConfigOption("a", 1, "a"))},
        {"libraries": (("z", "1"), ("a", "1"))},
    ],
)
def test_a_descriptor_refuses_what_the_contract_forbids(changes: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        descriptor(**changes)


def test_evidence_kinds_are_every_kind_but_the_ledger_transforms_and_findings() -> None:
    runtime_kinds = {
        "source_artifact",
        "source_revision",
        "source_absence",
        "transform_record",
        "ingest_finding",
    }
    assert set(RECORD_KINDS) - runtime_kinds == EVIDENCE_KINDS


def test_formats_take_lowercase_dotted_extensions_and_nonempty_magic() -> None:
    with pytest.raises(ContractError):
        FormatSpec("Demo", extensions=("DEMO",))
    with pytest.raises(ContractError):
        FormatSpec("Demo", extensions=(".Demo",))
    with pytest.raises(ContractError):
        Magic(0, b"")
    with pytest.raises(ContractError):
        Magic(-1, b"x")


# --- Config ------------------------------------------------------------------------------------


def test_omitting_an_option_and_giving_its_default_are_one_config() -> None:
    omitted = configure(descriptor())
    explicit = configure(descriptor(), {"mode": "fast", "ratio": 0.5, "strict": False})
    assert omitted == explicit
    assert omitted.transform.id == explicit.transform.id
    assert omitted.values == {"mode": "fast", "ratio": 0.5, "strict": False}
    assert omitted.transform.config_hash == config_hash(omitted.values)


def test_the_transform_names_the_adapter_version_and_libraries() -> None:
    transform = configure(descriptor()).transform
    assert (transform.adapter_id, transform.adapter_version) == ("demo", "1.2.3")
    assert transform.libraries == (("demo-lib", "2.0"),)
    assert transform.upstream == ()


def test_a_changed_setting_is_another_transform() -> None:
    assert (
        configure(descriptor(), {"mode": "slow"}).transform.id
        != configure(descriptor()).transform.id
    )


def test_a_new_adapter_version_is_another_transform() -> None:
    assert (
        configure(descriptor(version="1.2.4")).transform.id != configure(descriptor()).transform.id
    )


@pytest.mark.parametrize(
    "values",
    [
        {"unknown": 1},
        {"mode": "medium"},
        {"mode": 1},
        {"ratio": "0.5"},
        {"ratio": True},
        {"strict": 0},
        {"strict": "yes"},
        {"ratio": float("nan")},
    ],
)
def test_configure_refuses_unknown_options_and_bad_values(values: dict[str, Any]) -> None:
    with pytest.raises(ConfigError):
        configure(descriptor(), values)


def test_an_integer_is_accepted_for_a_float_and_stored_as_one() -> None:
    config = configure(descriptor(), {"ratio": 1})
    assert config.values["ratio"] == 1.0
    assert isinstance(config.values["ratio"], float)
    assert config == configure(descriptor(), {"ratio": 1.0})


def test_typed_accessors_check_the_setting_type() -> None:
    config = configure(descriptor())
    assert config.text("mode") == "fast"
    assert config.number("ratio") == 0.5
    assert config.flag("strict") is False
    with pytest.raises(ConfigError):
        config.integer("mode")
    with pytest.raises(ConfigError):
        config.flag("ratio")


def test_an_option_default_must_be_one_of_its_choices_of_its_type() -> None:
    with pytest.raises(ContractError):
        ConfigOption("mode", "other", "x", choices=("fast", "slow"))
    with pytest.raises(ContractError):
        ConfigOption("mode", "fast", "x", choices=("fast", 1))
    with pytest.raises(ContractError):
        ConfigOption("mode", "fast", "x", choices=("fast", "fast"))
    with pytest.raises(ValueError):
        ConfigOption("Mode", "fast", "x")


# --- Probe and inspect -------------------------------------------------------------------------


@pytest.mark.parametrize("confidence", [-0.1, 1.5, 1, float("nan")])
def test_confidence_is_a_float_in_the_unit_interval(confidence: Any) -> None:
    with pytest.raises(ContractError):
        ProbeResult(confidence, ())


def test_probe_reasons_are_coded_by_their_adapter() -> None:
    assert ProbeReason("demo.magic", "magic matched").to_json() == {
        "code": "demo.magic",
        "message": "magic matched",
    }
    for code in ("magic", "Demo.magic", "demo.", "demo.Magic"):
        with pytest.raises(ValueError):
            ProbeReason(code, "x")


def test_a_probe_result_omits_an_unknown_version_from_its_json() -> None:
    assert ProbeResult(0.9, ()).to_json() == {"confidence": 0.9, "reasons": []}
    assert ProbeResult(0.9, (), "2.0").to_json()["version"] == "2.0"


def test_hints_hold_a_name_and_a_size() -> None:
    assert ProbeHints("", 0).name == ""
    with pytest.raises(ContractError):
        ProbeHints("a", -1)


def test_an_inspect_summary_is_canonical_json() -> None:
    assert InspectResult({"size": 3}).summary == {"size": 3}
    for summary in ({"x": float("nan")}, {"x": None}, {1: 2}):
        with pytest.raises(ContractError):
            InspectResult(summary)  # type: ignore[arg-type]


# --- Chunks and plans --------------------------------------------------------------------------

SOURCE = BytesReader(b"0123456789")


def test_a_chunk_id_covers_source_transform_and_context() -> None:
    config = configure(descriptor())
    chunk = make_chunk(SOURCE, config, {"start": 0}, 10)
    assert chunk.id.startswith("chunk:sha256:")
    assert chunk == make_chunk(SOURCE, config, {"start": 0}, 10)
    other_source = BytesReader(b"different")
    other_config = configure(descriptor(), {"mode": "slow"})
    assert make_chunk(SOURCE, config, {"start": 1}, 10).id != chunk.id
    assert make_chunk(other_source, config, {"start": 0}, 10).id != chunk.id
    assert make_chunk(SOURCE, other_config, {"start": 0}, 10).id != chunk.id


def test_cost_is_not_part_of_a_chunk_id() -> None:
    config = configure(descriptor())
    assert make_chunk(SOURCE, config, {}, 1).id == make_chunk(SOURCE, config, {}, 2).id


def test_a_chunk_with_a_wrong_id_is_refused() -> None:
    chunk = make_chunk(SOURCE, configure(descriptor()), {"start": 0}, 10)
    with pytest.raises(ContractError):
        replace(chunk, context={"start": 1})
    with pytest.raises(ContractError):
        replace(chunk, id=ChunkId("chunk:sha256:" + "0" * 64))


def test_a_chunk_reads_back_from_its_json() -> None:
    chunk = make_chunk(SOURCE, configure(descriptor()), {"start": 0, "x": [1, "a"]}, 10)
    assert chunk_from_json(chunk.to_json()) == chunk
    data = dict(chunk.to_json())
    for broken in (
        {**data, "extra": 1},
        {key: value for key, value in data.items() if key != "cost"},
        {**data, "id": "rec:sha256:" + "0" * 64},
        {**data, "cost": True},
        {**data, "context": {"start": 1}},
    ):
        with pytest.raises(ValueError):
            chunk_from_json(broken)


@given(st.dictionaries(st.text(), st.integers() | st.text(), max_size=4))
def test_chunk_ids_are_deterministic(context: dict[str, int | str]) -> None:
    config = configure(descriptor())
    transform = config.transform.id
    assert chunk_id(SOURCE.content_id, transform, context) == chunk_id(
        SOURCE.content_id, transform, dict(reversed(context.items()))
    )


def test_a_plan_has_at_least_one_chunk_and_no_repeats() -> None:
    chunk = make_chunk(SOURCE, configure(descriptor()), {}, 0)
    assert Plan((chunk,)).chunks == (chunk,)
    with pytest.raises(ContractError):
        Plan(())
    with pytest.raises(ContractError):
        Plan((chunk, chunk))


def test_a_chunk_context_is_canonical_json() -> None:
    config = configure(descriptor())
    with pytest.raises(ContractError):
        make_chunk(SOURCE, config, {"x": float("inf")}, 0)
    with pytest.raises(ContractError):
        make_chunk(SOURCE, config, {"x": 1}, -1)
    assert isinstance(make_chunk(SOURCE, config, {}, 0), Chunk)


# --- Reading -----------------------------------------------------------------------------------


@given(st.binary(max_size=200), st.integers(1, 50), st.data())
def test_read_pieces_yields_exactly_the_range(data: bytes, size: int, draw: st.DataObject) -> None:
    source = BytesReader(data)
    start = draw.draw(st.integers(0, len(data)))
    end = draw.draw(st.integers(start, len(data)))
    pieces = list(read_pieces(source, start, end, size))
    assert b"".join(pieces) == data[start:end]
    assert all(0 < len(piece) <= size for piece in pieces)


def test_read_pieces_refuses_a_range_outside_the_source() -> None:
    for start, end in ((0, 11), (5, 4), (-1, 3)):
        with pytest.raises(ValueError):
            list(read_pieces(SOURCE, start, end))


def test_a_bytes_reader_serves_its_bytes_and_their_content_id() -> None:
    source = BytesReader(b"abc")
    assert (source.size, source.read(1, 5), source.read(3, 1)) == (3, b"bc", b"")
    assert BytesReader(b"abc", source.content_id).content_id == source.content_id
    with pytest.raises(ValueError):
        BytesReader(b"abd", source.content_id)
    for offset, length in ((4, 1), (-1, 1), (0, -1)):
        with pytest.raises(ValueError):
            source.read(offset, length)


class _CutAfterHash:
    """A reader over a file cut after it was hashed: declares the artifact, serves what is left."""

    def __init__(self, declared: bytes, present: int) -> None:
        self._declared = BytesReader(declared)
        self._present = present

    @property
    def content_id(self) -> ContentId:
        return self._declared.content_id

    @property
    def size(self) -> int:
        return self._declared.size

    def read(self, offset: int, length: int) -> bytes:
        return self._declared.read(offset, length)[: max(self._present - offset, 0)]


def test_a_short_read_is_a_structured_error_naming_the_unserved_range() -> None:
    source = _CutAfterHash(bytes(range(100)), 42)
    assert b"".join(read_pieces(source, 0, 40, 7)) == bytes(range(40))
    with pytest.raises(ShortReadError) as info:
        list(read_pieces(source, 30, 100, 7))
    error = info.value
    assert (error.source, error.offset, error.length) == (source.content_id, 42, 58)
    assert not isinstance(error, ValueError)  # a caller's bad range is one; a short read is not
    assert str(error) == f"{source.content_id} served no bytes at 42; 58 declared bytes unread"
