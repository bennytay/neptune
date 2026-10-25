"""Factory plumbing without the network: factory-merge's verdict and architecture-section filters
(``scripts/factory-merge.jq``, and the script itself against a fake ``gh``), the exit status of the
Makefile lint loop."""

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).parents[2]
SCRIPTS = ROOT / "scripts"
FIXTURES = ROOT / "tests" / "fixtures" / "factory"
VERDICTS = json.loads((FIXTURES / "factory_verdict_cases.json").read_text())
BODIES = json.loads((FIXTURES / "factory_architecture_bodies.json").read_text())
HEAD: str = VERDICTS["head"]


def _jq(program: str, stdin: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["jq", "-L", str(SCRIPTS), *args, f'include "factory-merge"; {program}'],
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
    )


def _verdict(events: list[dict[str, Any]], head: str = HEAD) -> str | None:
    # The script slurps a stream of {at, association, body} objects, one per comment or review.
    stream = "\n".join(json.dumps(e) for e in events)
    result = _jq("verdict($head)", stream, "-rs", "--arg", "head", head)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip() or None


@pytest.mark.parametrize("case", sorted(VERDICTS["cases"]))
def test_verdict(case: str) -> None:
    spec = VERDICTS["cases"][case]
    assert _verdict(spec["events"]) == spec["expected"]


def test_verdict_is_independent_of_event_order() -> None:
    events = VERDICTS["cases"]["later_revise_overrides_merge"]["events"]
    assert _verdict(list(reversed(events))) == "REVISE"


@pytest.mark.parametrize("case", sorted(BODIES))
def test_architecture_change_filled(case: str) -> None:
    spec = BODIES[case]
    result = _jq("architecture_change_filled", json.dumps({"body": spec["body"]}), "-e")
    assert result.returncode == (0 if spec["filled"] else 1), result.stderr
    assert result.stdout.strip() == json.dumps(spec["filled"])


# --- factory-merge.sh end to end, against a fake `gh` that serves fixture JSON -------------------

FAKE_GH = r"""#!/usr/bin/env bash
# Fake `gh api [--paginate] <path> [--jq <filter>]`: serve $GH_FIXTURES/<file> through the filter.
set -euo pipefail
[[ $1 == api ]] || { echo "fake gh: unsupported: $*" >&2; exit 9; }
shift
[[ $1 == --paginate ]] && shift
path=$1
shift
filter=.
[[ ${1:-} == --jq ]] && filter=$2
case $path in
  */pulls/7) file=pull ;;
  */issues/7/comments) file=comments ;;
  */pulls/7/reviews) file=reviews ;;
  */pulls/7/files) file=files ;;
  */commits\?sha=main*) file=main-commits ;;
  */commits/aaaaaaa*/check-runs*) file=main-pending ;;
  */commits/mmmmmmm*/check-runs*) file=main-check-runs ;;
  */check-runs*) file=check-runs ;;
  */compare/main...*) file=compare-pr ;;
  */compare/*...main) file=compare-main ;;
  *) echo "fake gh: unexpected path $path" >&2; exit 9 ;;
esac
jq -rc "$filter" "$GH_FIXTURES/$file.json"
"""


def _executable(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _factory_merge(
    tmp_path: Path,
    comments: list[dict[str, Any]],
    body: str = "Closes MVL-1",
    files: tuple[str, ...] = ("src/x.py",),
    behind: int = 0,
    main_files: tuple[str, ...] = (),
    main_check: str = "success",
    labels: tuple[str, ...] = (),
) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _executable(bin_dir / "gh", FAKE_GH)
    pull = {
        "state": "open",
        "draft": False,
        "base": {"ref": "main"},
        "head": {"sha": HEAD},
        "mergeable_state": "clean",
        "title": "Do a thing",
        "body": body,
        "labels": [{"name": n} for n in labels],
    }

    def runs(conclusion: str) -> dict[str, Any]:
        return {
            "check_runs": [
                {
                    "status": "completed",
                    "completed_at": "2026-10-01T09:00:00Z",
                    "conclusion": conclusion,
                }
            ]
        }

    served = {
        "pull": pull,
        "comments": comments,
        "reviews": [],
        "files": [{"filename": f} for f in files],
        "check-runs": runs("success"),
        "main-commits": [{"sha": "a" * 40}, {"sha": "m" * 40}],
        "main-pending": {"check_runs": [{"status": "in_progress", "conclusion": None}]},
        "main-check-runs": runs(main_check),
        "compare-pr": {"behind_by": behind, "merge_base_commit": {"sha": "b" * 40}},
        "compare-main": {"files": [{"filename": f} for f in main_files]},
    }
    for name, value in served.items():
        (tmp_path / f"{name}.json").write_text(json.dumps(value))
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "GH_REPO": "owner/repo",
        "GH_FIXTURES": str(tmp_path),
        "DRY_RUN": "1",
        "FACTORY_MERGE_LOCK": str(tmp_path / "merge.lock"),
        "FRESHNESS_REF": "HEAD",
    }
    return subprocess.run(
        ["bash", str(SCRIPTS / "factory-merge.sh"), "7", HEAD[:7]],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _comment(association: str, verdict: str, at: str = "2026-10-01T10:00:00Z") -> dict[str, Any]:
    return {
        "created_at": at,
        "author_association": association,
        "body": f"Review: {verdict} @ {HEAD[:7]}",
    }


def test_factory_merge_accepts_a_trusted_merge(tmp_path: Path) -> None:
    result = _factory_merge(tmp_path, [_comment("OWNER", "MERGE")])
    assert result.returncode == 0, result.stderr
    assert "would squash-merge #7" in result.stderr


def test_factory_merge_ignores_an_outsider_merge(tmp_path: Path) -> None:
    result = _factory_merge(tmp_path, [_comment("NONE", "MERGE")])
    assert result.returncode == 1
    assert "latest: 'none'" in result.stderr


def test_factory_merge_honours_a_later_revise(tmp_path: Path) -> None:
    comments = [_comment("OWNER", "MERGE"), _comment("MEMBER", "REVISE", "2026-10-01T11:00:00Z")]
    result = _factory_merge(tmp_path, comments)
    assert result.returncode == 1
    assert "latest: 'REVISE'" in result.stderr


@pytest.mark.parametrize(("case", "accepted"), [("heading_only", False), ("filled_bullet", True)])
def test_factory_merge_requires_a_filled_architecture_change(
    tmp_path: Path, case: str, accepted: bool
) -> None:
    result = _factory_merge(
        tmp_path, [_comment("OWNER", "MERGE")], BODIES[case]["body"], ("ARCHITECTURE.md",)
    )
    assert result.returncode == (0 if accepted else 1), result.stderr
    assert ("no filled **Architecture change** section" in result.stderr) is not accepted


def test_factory_merge_merges_a_behind_pr_when_main_changed_elsewhere(tmp_path: Path) -> None:
    result = _factory_merge(
        tmp_path,
        [_comment("OWNER", "MERGE")],
        files=("src/neptune/adapters/mcap/adapter.py",),
        behind=3,
        main_files=("docs/site-model/spec.md",),
    )
    assert result.returncode == 0, result.stderr
    assert "3 commit(s) behind main" in result.stderr
    assert "would squash-merge #7" in result.stderr


def test_factory_merge_asks_for_a_refresh_when_main_changed_what_the_pr_reaches(
    tmp_path: Path,
) -> None:
    result = _factory_merge(
        tmp_path,
        [_comment("OWNER", "MERGE")],
        files=("src/neptune/adapters/mcap/adapter.py",),
        behind=1,
        main_files=("src/neptune/model/time.py",),
    )
    assert result.returncode == 1
    assert "needs a refresh (main changed inputs to neptune)" in result.stderr


@pytest.mark.parametrize(
    ("main_check", "labels", "accepted"),
    [
        ("failure", (), False),
        ("cancelled", (), False),
        ("failure", ("fix-main",), True),
        ("success", (), True),
    ],
)
def test_factory_merge_stops_the_line_on_a_red_main(
    tmp_path: Path, main_check: str, labels: tuple[str, ...], accepted: bool
) -> None:
    """main's head is still running, so its parent's completed check decides."""
    result = _factory_merge(
        tmp_path, [_comment("OWNER", "MERGE")], main_check=main_check, labels=labels
    )
    assert result.returncode == (0 if accepted else 1), result.stderr
    assert ("the latest check on main failed" in result.stderr) is not accepted


# --- Makefile: a failing `ruff format --check` stops the all-packages lint loop ------------------

FAKE_UV = r"""#!/usr/bin/env bash
# Fake uv: log the call; fail `ruff format` when run from $FAIL_FORMAT_IN.
echo "$PWD $*" >>"$UV_LOG"
case " $* " in *" format "*) [[ $PWD == "${FAIL_FORMAT_IN:-}" ]] && exit 1 ;; esac
exit 0
"""


def _make_lint(tmp_path: Path, fail_format_in: str) -> tuple[int, list[str]]:
    return _make(tmp_path, "lint", fail_format_in=fail_format_in)


def _make(
    tmp_path: Path, target: str, pkg: str = "", fail_format_in: str = ""
) -> tuple[int, list[str]]:
    workspace = tmp_path / "ws"
    for member in ("alpha", "beta"):
        (workspace / "packages" / member).mkdir(parents=True)
        (workspace / "packages" / member / "pyproject.toml").write_text("")
    shutil.copy(ROOT / "Makefile", workspace / "Makefile")
    _executable(tmp_path / "uv", FAKE_UV)
    log = tmp_path / "uv.log"
    log.touch()
    # An outer `make check PKG=...` (CI) must not leak its package selection into this run.
    env = {k: v for k, v in os.environ.items() if not k.startswith(("MAKE", "MFLAGS", "PKG"))}
    env |= {"UV_LOG": str(log), "FAIL_FORMAT_IN": fail_format_in}
    result = subprocess.run(
        [
            "make",
            "-C",
            str(workspace),
            target,
            f"UV={tmp_path / 'uv'}",
            "COMPILER_ADR=",
            f"PKG={pkg}",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode, log.read_text().splitlines()


def test_make_lint_stops_when_an_earlier_package_fails_format(tmp_path: Path) -> None:
    root = str((tmp_path / "ws").resolve())
    status, calls = _make_lint(tmp_path, fail_format_in=root)
    assert status != 0
    assert len(calls) == 1 and "ruff format --check" in calls[0]  # neither ruff check nor alpha ran


def test_make_lint_runs_every_package_when_all_pass(tmp_path: Path) -> None:
    status, calls = _make_lint(tmp_path, fail_format_in="")
    assert status == 0
    assert len(calls) == 6  # format + check for the compiler, alpha and beta
    assert sum(c.split(" ", 1)[0].endswith("/packages/alpha") for c in calls) == 2
