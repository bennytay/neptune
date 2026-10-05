"""The model seam: replay refuses what it has not recorded, recordings are strict, the live client's
request is the schema contract, and nothing here needs the network or the optional SDK."""

from __future__ import annotations

import ast
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from neptune_context.query.plan import (
    AnthropicClient,
    ModelRequest,
    ModelResponse,
    ModelUnavailable,
    PlanStatus,
    Recording,
    RecordingClient,
    RecordingMissing,
    ReplayClient,
    anthropic_arguments,
    dump_recordings,
    load_recordings,
    output_schema,
)
from planner_golden_context import GOLDEN
from planner_helpers_context import ScriptedModel

SRC = Path(__file__).resolve().parents[1] / "src" / "neptune_context"


def request(user: str = "q") -> ModelRequest:
    return ModelRequest("claude-sonnet-5-5", "system", user, {"type": "object"})


def recording(sha: str = "a" * 64, text: str | None = "{}") -> Recording:
    return Recording(sha, "claude-sonnet-5-5", "end", text, "synthetic")


def test_replay_answers_a_recorded_request_and_refuses_the_rest() -> None:
    req = request()
    client = ReplayClient({req.sha256: recording(req.sha256, "hello")})
    assert client.complete(req) == ModelResponse("hello", "end", "claude-sonnet-5-5")
    with pytest.raises(RecordingMissing, match="re-record"):
        client.complete(request("another question"))
    assert issubclass(RecordingMissing, ModelUnavailable)


def test_a_missing_recording_is_a_visible_failed_plan() -> None:
    from neptune_context.query.plan import plan
    from planner_helpers_context import world

    index, profiles = world()
    result = plan(
        "Which runs does AMR-07 appear in?",
        "head",
        profiles["agent"],
        resolver=index,
        client=ReplayClient({}),
    )
    assert result.status is PlanStatus.FAILED
    assert [f.code.value for f in result.findings] == ["model_unavailable"]


def test_the_request_hash_covers_every_input() -> None:
    base = request()
    assert request().sha256 == base.sha256
    for changed in (
        ModelRequest("other", "system", "q", {"type": "object"}),
        ModelRequest("claude-sonnet-5-5", "other", "q", {"type": "object"}),
        ModelRequest("claude-sonnet-5-5", "system", "other", {"type": "object"}),
        ModelRequest("claude-sonnet-5-5", "system", "q", {"type": "array"}),
        ModelRequest("claude-sonnet-5-5", "system", "q", {"type": "object"}, 1),
    ):
        assert changed.sha256 != base.sha256


def test_recordings_round_trip_in_a_byte_stable_order(tmp_path: Path) -> None:
    items = [recording("b" * 64), recording("a" * 64, None)]
    text = dump_recordings(items)
    assert text == dump_recordings(reversed(items))
    path = tmp_path / "r.jsonl"
    path.write_text(text, encoding="utf-8")
    loaded = load_recordings(path)
    assert list(loaded) == ["a" * 64, "b" * 64] and loaded["a" * 64].text is None
    assert "null" not in text


@pytest.mark.parametrize(
    "line",
    [
        "not json",
        "[]",
        json.dumps({"request_sha256": "short", "model": "m", "stop": "end", "recorded_by": "live"}),
        json.dumps(
            {"request_sha256": "a" * 64, "model": "m", "stop": "weird", "recorded_by": "live"}
        ),
        json.dumps(
            {"request_sha256": "a" * 64, "model": "m", "stop": "end", "recorded_by": "guess"}
        ),
        json.dumps(
            {
                "request_sha256": "a" * 64,
                "model": "m",
                "stop": "end",
                "recorded_by": "live",
                "text": 3,
            }
        ),
    ],
)
def test_malformed_recordings_are_refused_with_their_line(tmp_path: Path, line: str) -> None:
    path = tmp_path / "r.jsonl"
    path.write_text(line + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"r\.jsonl:1"):
        load_recordings(path)


def test_a_request_recorded_twice_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "r.jsonl"
    path.write_text(dump_recordings([recording()]) * 2, encoding="utf-8")
    with pytest.raises(ValueError, match="twice"):
        load_recordings(path)


def test_the_golden_recordings_load() -> None:
    assert len(load_recordings(GOLDEN / "recordings.jsonl")) == 99


def test_recording_client_keeps_what_the_live_client_returned() -> None:
    inner = ScriptedModel("{}")
    wrapped = RecordingClient(inner)
    req = request()
    wrapped.complete(req)
    assert wrapped.client_id == "scripted"
    (kept,) = list(wrapped)
    assert (kept.request_sha256, kept.text, kept.recorded_by) == (req.sha256, "{}", "live")


def test_the_live_request_is_schema_constrained_and_sets_no_sampling() -> None:
    args = anthropic_arguments(request())
    assert args["model"] == "claude-sonnet-5-5"
    assert args["output_config"]["format"] == {"type": "json_schema", "schema": {"type": "object"}}
    assert not {"temperature", "top_p", "top_k", "tools", "tool_choice"} & set(args)
    assert args["messages"] == [{"role": "user", "content": "q"}]


def test_the_live_client_reads_text_and_maps_stop_reasons() -> None:
    def sdk(stop: str, blocks: list[SimpleNamespace]) -> SimpleNamespace:
        message = SimpleNamespace(stop_reason=stop, content=blocks, model="claude-sonnet-5-5")
        return SimpleNamespace(messages=SimpleNamespace(create=lambda **_: message))

    thinking, text = SimpleNamespace(type="thinking"), SimpleNamespace(type="text", text="{}")
    assert AnthropicClient(sdk("end_turn", [thinking, text])).complete(request()) == ModelResponse(
        "{}", "end", "claude-sonnet-5-5"
    )
    assert AnthropicClient(sdk("max_tokens", [text])).complete(request()).stop == "max_tokens"
    refused = AnthropicClient(sdk("refusal", [])).complete(request())
    assert (refused.text, refused.stop) == (None, "refusal")
    assert AnthropicClient(sdk("pause_turn", [text])).complete(request()).stop == "other"


def test_the_sdk_is_imported_lazily_so_it_stays_optional() -> None:
    for path in SRC.rglob("*.py"):
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            names = (
                [a.name for a in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else []
            )
            assert not any(n == "anthropic" or n.startswith("anthropic.") for n in names), path


def test_the_default_model_is_sonnet_five_five() -> None:
    from neptune_context.query.plan import DEFAULT_MODEL

    assert DEFAULT_MODEL == "claude-sonnet-5-5"
    assert output_schema()["type"] == "object"
