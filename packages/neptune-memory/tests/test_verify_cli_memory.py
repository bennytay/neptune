"""``memory verify GRAPH``: a consumer's graph document checked with Memory's codec, one line per
problem (``schema.codec.graph_problems``).

Good documents are the acceptance-corpus snapshot and the published golden graph; every corrupted
copy is one of them with one or more problems put in, and is reported exactly that many times.
"""

from __future__ import annotations

import copy
import io
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import pytest

from memory_golden_fixtures import PUBLISHED
from neptune.identity import canonical_json
from neptune_memory.cli import OK, REFUSED, USAGE, main
from neptune_memory.schema.codec import graph_from_json, graph_problems

if TYPE_CHECKING:
    from collections.abc import Callable

SNAPSHOT: Final = Path(__file__).resolve().parent / "fixtures" / "acceptance_corpus.graph.json"
GOLDEN: Final = PUBLISHED / "golden" / "graph.json"
GOOD: Final = (SNAPSHOT, GOLDEN)


def load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def verify(path: Path) -> tuple[int, list[str], str]:
    out, err = io.StringIO(), io.StringIO()
    status = main(["verify", str(path)], stdout=out, stderr=err)
    return status, out.getvalue().splitlines(), err.getvalue()


def write(tmp_path: Path, document: Any, name: str = "graph.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(document, indent=1), encoding="utf-8")  # canonical or not
    return path


def bad(tmp_path: Path, document: Any) -> list[str]:
    """``memory verify`` refuses ``document``: its lines, each naming the file once."""
    path = write(tmp_path, document)
    status, lines, err = verify(path)
    assert status == REFUSED, lines
    assert err == ""
    assert all(line.startswith(f"{path}: ") for line in lines)
    with pytest.raises((ValueError, TypeError)):
        graph_from_json(document)  # the codec agrees: no problem is verify's alone
    return [line.removeprefix(f"{path}: ") for line in lines]


@pytest.mark.parametrize("path", GOOD, ids=lambda p: p.parent.name)
def test_a_good_document_verifies_with_a_one_line_summary(path: Path) -> None:
    status, lines, err = verify(path)
    graph = graph_from_json(load(path))
    assert (status, err) == (OK, "")
    assert lines == [
        f"{path}: ok: graph-schema 1 document, head {graph.head}, "
        f"{len(graph.resolution.claims)} claims, {len(graph.resolution.findings)} findings, "
        f"{len(graph.builds)} builds, generation {graph.generation}"
    ]


def test_verify_needs_no_graphs_or_tenant_but_the_other_commands_do(tmp_path: Path) -> None:
    out, err = io.StringIO(), io.StringIO()
    assert main(["dump"], stdout=out, stderr=err) == USAGE
    assert "needs --graphs and --tenant" in err.getvalue()
    assert verify(SNAPSHOT)[0] == OK


def test_every_claim_whose_id_does_not_match_its_content_is_a_line(tmp_path: Path) -> None:
    document = load(SNAPSHOT)
    for index in (0, 5, 40):
        document["claims"][index]["provenance"]["consolidator_version"] = "999"
    lines = bad(tmp_path, document)
    assert len(lines) == 3
    for line, index in zip(lines, (0, 5, 40), strict=True):
        assert line.startswith(f"claims[{index}]: claim id ")
        assert "does not match its content" in line


def test_an_unsorted_record_list_is_a_line(tmp_path: Path) -> None:
    document = load(SNAPSHOT)
    index = next(i for i, c in enumerate(document["claims"]) if len(c["provenance"]["records"]) > 1)
    document["claims"][index]["provenance"]["records"].reverse()
    assert bad(tmp_path, document) == [f"claims[{index}]: records must be unique and sorted"]


def test_claims_and_findings_out_of_canonical_order_are_lines(tmp_path: Path) -> None:
    document = load(GOLDEN)
    claims, findings = document["claims"], document["findings"]
    claims[2], claims[3] = claims[3], claims[2]
    findings.reverse()
    lines = bad(tmp_path, document)
    assert [line for line in lines if line.startswith("claims")] == [
        "claims[3] is out of order: claims are ordered by (recorded_at, id)"
    ]
    finding_lines = [line for line in lines if line.startswith("findings")]
    assert finding_lines
    assert all(
        "findings are ordered by (recorded_at, claim, code, others)" in x for x in finding_lines
    )
    assert len(lines) == 1 + len(finding_lines)


def test_a_wrong_generation_is_a_line(tmp_path: Path) -> None:
    document = load(SNAPSHOT)
    right = document["generation"]
    document["generation"] = "sha256:" + "0" * 64
    assert bad(tmp_path, document) == [
        f"generation {'sha256:' + '0' * 64!r} does not match the resolver configuration ({right})"
    ]


def test_problems_of_every_kind_are_reported_together(tmp_path: Path) -> None:
    document = load(GOLDEN)
    document["claims"][0]["confidence"] = {"knowledge": "known", "value": 0.5}
    document["claims"][4], document["claims"][5] = document["claims"][5], document["claims"][4]
    document["generation"] = "sha256:" + "1" * 64
    document["kind"] = "deploy.graph"
    document["extra"] = True
    lines = bad(tmp_path, document)
    assert lines[0] == "graph document: missing keys [], unexpected keys ['extra']"
    assert lines[1] == "kind is 'deploy.graph', not 'memory.graph'"
    assert lines[2].startswith("claims[0]: claim id ")
    assert lines[3] == "claims[5] is out of order: claims are ordered by (recorded_at, id)"
    assert lines[4].startswith("generation 'sha256:1111")
    assert len(lines) == 5


def test_a_consistency_problem_is_the_codecs_own_line(tmp_path: Path) -> None:
    document = load(GOLDEN)
    superseded = next(c for c in document["claims"] if c["supersedes"])
    document["claims"] = [c for c in document["claims"] if c["id"] not in superseded["supersedes"]]
    (line,) = bad(tmp_path, document)
    assert line.startswith("references to claims the document does not hold")


def test_a_wrong_graph_schema_version_is_a_line(tmp_path: Path) -> None:
    document = load(SNAPSHOT)
    document["graph_schema_version"] = 2
    assert bad(tmp_path, document) == ["graph_schema_version 2 is not supported"]


def test_a_document_that_is_not_an_object_is_one_line(tmp_path: Path) -> None:
    assert bad(tmp_path, [load(SNAPSHOT)]) == ["the document must be a JSON object, got list"]


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        (b"{", "invalid input"),
        (b'{"kind": 1, "kind": 2}', "invalid input"),
        (b'{"head": NaN}', "invalid input"),
        (b"\xff\xfe", "cannot read input"),
    ],
)
def test_unreadable_input_is_a_usage_error(tmp_path: Path, text: bytes, reason: str) -> None:
    path = tmp_path / "graph.json"
    path.write_bytes(text)
    status, lines, err = verify(path)
    assert (status, lines) == (USAGE, [])
    assert err.startswith(f"memory: {reason}")


def test_a_missing_file_is_a_usage_error(tmp_path: Path) -> None:
    status, lines, err = verify(tmp_path / "absent.json")
    assert (status, lines) == (USAGE, [])
    assert err.startswith("memory: cannot read input")


def test_verify_never_changes_the_file(tmp_path: Path) -> None:
    path = tmp_path / "graph.json"
    path.write_bytes(SNAPSHOT.read_bytes())
    document = load(path)
    document["generation"] = "sha256:" + "2" * 64
    corrupted = write(tmp_path, document, "corrupted.json")
    before = corrupted.read_bytes()
    assert verify(path)[0] == OK
    assert verify(corrupted)[0] == REFUSED
    assert corrupted.read_bytes() == before
    assert path.read_bytes() == SNAPSHOT.read_bytes()


def test_graph_problems_is_empty_exactly_when_the_codec_accepts() -> None:
    good = load(GOLDEN)
    assert graph_problems(good) == ()
    mutations: tuple[Callable[[dict[str, Any]], object], ...] = (
        lambda d: d.pop("head"),
        lambda d: d.update(builds=[]),
        lambda d: d["builds"].reverse(),
        lambda d: d.update(head=1),
        lambda d: d["claims"].append(d["claims"][-1]),
        lambda d: d.update(resolver_config=[]),
    )
    for mutate in mutations:
        document = copy.deepcopy(good)
        mutate(document)
        problems = graph_problems(document)
        assert problems, document.keys()
        with pytest.raises((ValueError, TypeError, KeyError)):
            graph_from_json(document)
    assert graph_problems(canonical_json.loads(canonical_json.dumps(good))) == ()
