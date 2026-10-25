"""Gold questions over real public robot data (issue #211, docs/site-model/real-data.md).

The site is UCI's "UR3 CobotOps" export, fetched by ``scripts/fetch_public_site.sh site-public``
(the nightly run does that first). Without it these skip. The workbook's ``static/`` is copied
to a temporary site and built from scratch, so the test leaves ``site-public/`` alone.
"""

import json
import os
import shutil
from pathlib import Path
from typing import Any

import pytest

from neptune_memory.site import build, query, views

ROOT = Path(__file__).resolve().parents[4]
SITE = Path(os.environ.get("NEPTUNE_PUBLIC_SITE", ROOT / "site-public"))
WORKBOOK = SITE / "static" / "ur3" / "dataset_02052023.xlsx"
GOLD = json.loads((Path(__file__).parent / "public_gold_questions.json").read_text("utf-8"))
QUESTIONS = {q["id"]: q for q in GOLD["questions"]}
_BUILT: dict[str, Path] = {}

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(
        not WORKBOOK.exists(), reason="run scripts/fetch_public_site.sh site-public first"
    ),
]


def _site(tmp_path_factory: pytest.TempPathFactory) -> Path:
    if "dir" not in _BUILT:
        out = tmp_path_factory.mktemp("site-public")
        shutil.copytree(SITE / "static", out / "static")
        build.build(out)
        _BUILT["dir"] = out
    return _BUILT["dir"]


def _object(item: Any) -> Any:
    obj = item.object
    if obj is None:
        return None
    return obj.ref if hasattr(obj, "ref") else obj.value


def _matches(item: Any, want: dict[str, Any]) -> bool:
    have = {
        "subject": item.subject,
        "predicate": item.predicate,
        "object": _object(item),
        "kind": item.kind,
        "state": item.state,
    }
    return all(have[key] == value for key, value in want.items())


@pytest.mark.parametrize("qid", sorted(QUESTIONS))
def test_site_public_gold_question(tmp_path_factory: pytest.TempPathFactory, qid: str) -> None:
    question = QUESTIONS[qid]
    model = views.load(_site(tmp_path_factory))
    answers = [getattr(query, c["tool"])(model, **c["args"]) for c in question["calls"]]
    items = [item for answer in answers for item in answer.items]
    missing = [w for w in question["expect"] if not any(_matches(i, w) for i in items)]
    assert not missing, f"{qid} lacks {missing}"
    rows = [list(row) for answer in answers for row in getattr(answer, "rows", None) or []]
    for want in question["rows"]:
        assert any(row[: len(want)] == want for row in rows), f"{qid} rows {rows}"
    sources = [c for answer in answers for c in answer.sources.values()]
    assert set(question["cites"]) <= {c.file for c in sources}
    assert set(question["locators"]) <= {c.locator for c in sources}, qid
