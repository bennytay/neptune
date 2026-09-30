import pytest

from neptune.identity.hashing import content_id
from neptune.identity.ids import adapter_record_id, config_hash, record_id
from neptune.model.ids import ConfigHash, ContentId, RecordId, parse_record_id

SOURCE = content_id(b"source bytes")
CONFIG = config_hash({"decode_payloads": True, "max_messages": 1000})


def make_id(**overrides: object) -> RecordId:
    args: dict[str, object] = {
        "kind": "stream",
        "source": SOURCE,
        "locator": {"kind": "record_range", "topic": "/imu"},
        "adapter_id": "mcap",
        "adapter_version": "1.0.0",
        "config": CONFIG,
    }
    args.update(overrides)
    return adapter_record_id(**args)  # type: ignore[arg-type]


def test_record_id_is_deterministic_and_prefixed() -> None:
    assert make_id() == make_id()
    assert parse_record_id(make_id()) == make_id()


@pytest.mark.parametrize(
    "override",
    [
        {"kind": "channel"},
        {"source": content_id(b"other bytes")},
        {"locator": {"kind": "record_range", "topic": "/gps"}},
        {"adapter_id": "mcap-alt"},
        {"adapter_version": "1.0.1"},
        {"config": config_hash({"decode_payloads": False, "max_messages": 1000})},
    ],
)
def test_every_input_changes_the_id(override: dict[str, object]) -> None:
    assert make_id(**override) != make_id()


def test_adapter_version_bump_creates_new_lineage() -> None:
    v1 = {make_id(locator={"topic": t}) for t in ("/a", "/b")}
    v2 = {make_id(locator={"topic": t}, adapter_version="2.0.0") for t in ("/a", "/b")}
    assert v1.isdisjoint(v2)


def test_record_id_is_not_a_content_id_of_the_same_payload() -> None:
    # Tier prefixes keep the id spaces apart even if the hashed bytes coincide.
    assert not make_id().startswith("sha256:")


def test_config_hash_ignores_key_order() -> None:
    assert config_hash({"a": 1, "b": 2}) == config_hash({"b": 2, "a": 1})
    assert config_hash({}) == str(content_id(b"{}"))


@pytest.mark.parametrize("kind", ["", "Stream", "1stream", "stream kind"])
def test_rejects_malformed_kind(kind: str) -> None:
    with pytest.raises(ValueError, match="kind"):
        record_id(kind, {})


def test_rejects_swapped_or_malformed_ids() -> None:
    with pytest.raises(ValueError, match="content id"):
        make_id(source=ContentId("sha256:ABC"))
    with pytest.raises(ValueError, match="content id"):
        make_id(config=ConfigHash("md5:abc"))
    with pytest.raises(ValueError, match="adapter_id"):
        make_id(adapter_id="MCAP")
    with pytest.raises(ValueError, match="adapter_version"):
        make_id(adapter_version="")
