"""``extract-photos`` records a model's reading of each photo; no key keeps the recording."""

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from neptune_memory.site.__main__ import main
from neptune_memory.site.corpus import generate
from neptune_memory.site.derived import photos


class _Claude:
    """A stand-in for ``anthropic.Anthropic()`` that answers the forced ``record_notes`` call."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.messages = self

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        regions = [
            {"box": [10, 120, 200, 160], "text": "second"},
            {"box": [10, 20, 200, 60], "text": "first"},
        ]
        return SimpleNamespace(
            content=[SimpleNamespace(type="tool_use", input={"regions": regions})]
        )


def test_site_photos_extract_with_a_client(tmp_path: Path) -> None:
    generate(tmp_path, site="harbor")
    claude = _Claude()
    written = photos.extract_photos(tmp_path, client=claude)
    assert [p.name for p in written] == ["whiteboard_2026-11-04.extraction.json"]
    record = json.loads(written[0].read_text(encoding="utf-8"))
    assert record["model"] == "claude-opus-5-5"
    assert record["prompt_sha256"] == photos.prompt_sha256()
    assert [r["text"] for r in record["regions"]] == ["first", "second"]  # reading order
    assert [r["id"] for r in record["regions"]] == [1, 2]
    sent = claude.calls[0]
    assert sent["model"] == "claude-opus-5-5"
    assert sent["messages"][0]["content"][0]["source"]["media_type"] == "image/png"
    # The same answer records the same bytes.
    first = written[0].read_bytes()
    photos.extract_photos(tmp_path, client=_Claude())
    assert written[0].read_bytes() == first


def test_site_photos_skip_cleanly_without_a_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    generate(tmp_path, site="harbor")
    recorded = tmp_path / "static/photos/whiteboard_2026-11-04.extraction.json"
    before = recorded.read_bytes()
    assert main(["extract-photos", str(tmp_path)]) == 0
    assert "keeping the recorded extractions" in capsys.readouterr().out
    assert recorded.read_bytes() == before
    assert photos.extract_photos(tmp_path) == []
