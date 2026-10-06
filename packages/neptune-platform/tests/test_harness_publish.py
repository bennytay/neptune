"""Which Linear gate issues a report goes to, and when nothing is posted."""

import json
from pathlib import Path
from typing import Any

import pytest
from harness import contracts, publish
from harness.run import run


@pytest.fixture(scope="module")
def green(tmp_path_factory: pytest.TempPathFactory) -> Path:
    run_dir = tmp_path_factory.mktemp("green")
    run(run_dir, owner_tests=False)
    return run_dir / "report.json"


def _red(green: Path, tmp_path: Path) -> Path:
    document = json.loads(green.read_text(encoding="utf-8"))
    document["ok"] = False
    path = tmp_path / "red.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


class Posted:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, refuse: bool = False) -> None:
        self.calls: list[tuple[str, str, str]] = []
        tool = contracts.load_tool()

        def post(issue: str, body: str, key: str) -> None:
            if refuse:
                raise tool.ContractError("Linear refused the comment")
            self.calls.append((issue, body, key))

        monkeypatch.setattr(tool, "post_comment", post)


def test_the_gate_issues_are_the_stage_owners_and_the_harness_owners(green: Path) -> None:
    document: Any = json.loads(green.read_text(encoding="utf-8"))
    packages = contracts.registry().packages()
    issues = publish.gate_issues(document, packages)
    # deploy (its stage since platform ADR 0008), ledger, memory, context and platform; Learn owns
    # no stage
    owners = ("neptune-deploy", "neptune-ledger", "neptune-memory", "neptune-context")
    assert issues == sorted(packages[name]["gate_issue"] for name in (*owners, "neptune-platform"))
    assert packages["neptune-learn"]["gate_issue"] not in issues


def test_a_manual_run_posts_the_report_to_every_gate(
    green: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    posted = Posted(monkeypatch)
    code = publish.main(
        ["--report", str(green), "--link", "https://example.test/run/1"], {"LINEAR_API_KEY": "k"}
    )
    assert code == 0 and len(posted.calls) == 5
    assert {key for _, _, key in posted.calls} == {"k"}
    body = posted.calls[0][1]
    assert "Integration harness: green" in body and "Run: https://example.test/run/1" in body


def test_without_a_key_nothing_is_posted(green: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    posted = Posted(monkeypatch)
    assert publish.main(["--report", str(green)], {}) == 0
    assert posted.calls == []


def test_the_nightly_run_stays_quiet_when_green_and_posts_when_red(
    green: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    posted = Posted(monkeypatch)
    env = {"LINEAR_API_KEY": "k"}
    assert publish.main(["--report", str(green), "--only-failed"], env) == 0
    assert posted.calls == []
    assert publish.main(["--report", str(_red(green, tmp_path)), "--only-failed"], env) == 0
    assert len(posted.calls) == 5 and "RED" in posted.calls[0][1]


def test_a_refused_comment_fails_the_step_but_tries_every_gate(
    green: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    Posted(monkeypatch, refuse=True)
    assert publish.main(["--report", str(green)], {"LINEAR_API_KEY": "k"}) == 1
    assert capsys.readouterr().err.count("Linear refused the comment") == 5


def test_a_dry_run_names_the_targets_and_posts_nothing(
    green: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    posted = Posted(monkeypatch)
    assert publish.main(["--report", str(green), "--dry-run"], {}) == 0
    assert posted.calls == [] and capsys.readouterr().out.count("would post to MVL-") == 5
