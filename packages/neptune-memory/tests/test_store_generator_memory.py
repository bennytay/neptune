"""The MVL-104 synthetic deployment generator: deterministic, every embodiment, model invariants."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from itertools import pairwise
from typing import TYPE_CHECKING

import pytest

from neptune_memory.store.bench.generator import (
    CLAIM_COLUMNS,
    EMBODIMENTS,
    DeploymentSpec,
    generate,
    robot_ids,
    write_dataset,
)

if TYPE_CHECKING:
    from pathlib import Path

    from neptune_memory.store.records import ClaimRecord

SMALL = DeploymentSpec(claims=20_000, robots=24, sites=4)


@pytest.fixture(scope="module")
def claims() -> list[ClaimRecord]:
    return list(generate(SMALL))


def _digest(spec: DeploymentSpec, tmp: Path) -> str:
    c, e = tmp / "c.csv", tmp / "e.csv"
    write_dataset(spec, c, e)
    return hashlib.sha256(c.read_bytes() + b"\0" + e.read_bytes()).hexdigest()


def test_same_spec_same_bytes(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    assert _digest(SMALL, tmp_path / "a") == _digest(SMALL, tmp_path / "b")


def test_seed_changes_the_data(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    other = DeploymentSpec(claims=SMALL.claims, robots=24, sites=4, seed=7)
    assert _digest(SMALL, tmp_path / "a") != _digest(other, tmp_path / "b")


def test_pinned_digest_guards_cross_version_drift(tmp_path: Path) -> None:
    """Benchmark numbers in ADR 0004 are reproducible only if this stays fixed."""
    tiny = DeploymentSpec(claims=2_000, robots=6, sites=2)
    assert _digest(tiny, tmp_path) == PINNED


PINNED = "2813c92b97f54b166e1faa69bdac063a5f37eb212d3f963c3a23e86d5c57f782"


def test_every_embodiment_is_in_the_fleet() -> None:
    kinds = {kind for _, kind, _, _ in robot_ids(DeploymentSpec(claims=1, robots=12))}
    assert kinds == {e[0] for e in EMBODIMENTS}
    full = [kind for _, kind, _, _ in robot_ids(DeploymentSpec(claims=1))]
    assert {k: full.count(k) for k in set(full)} == {e[0]: e[1] for e in EMBODIMENTS}


def test_realised_count_tracks_the_target(claims: list[ClaimRecord]) -> None:
    assert 0.9 * SMALL.claims <= len(claims) <= 1.25 * SMALL.claims
    assert [c.claim_id for c in claims] == list(range(1, len(claims) + 1))


def test_supersession_links_close_exactly_when_the_correction_is_recorded(
    claims: list[ClaimRecord],
) -> None:
    by_id = {c.claim_id: c for c in claims}
    corrections = [c for c in claims if c.supersedes is not None]
    assert 0.03 < len(corrections) / len(claims) < 0.09
    for new in corrections:
        old = by_id[new.supersedes]  # type: ignore[index]
        assert old.superseded_at == new.recorded_at
        assert (old.subject, old.predicate, old.valid_from, old.valid_to) == (
            new.subject,
            new.predicate,
            new.valid_from,
            new.valid_to,
        )
        assert old.transform_id != new.transform_id  # a correction is new lineage
    assert sum(c.superseded_at is not None for c in claims) == len(corrections)


def test_every_claim_has_provenance_and_an_assertion_kind(claims: list[ClaimRecord]) -> None:
    for c in claims:
        assert c.source_id.startswith("ev:") and "@" in c.transform_id
        assert c.assertion_kind in ("observed", "stated")


def test_visible_intervals_never_overlap_per_subject_and_predicate(
    claims: list[ClaimRecord],
) -> None:
    """The functional-predicate invariant the indexed as-of thread relies on."""
    span = SMALL.span
    groups: dict[tuple[str, str], list[ClaimRecord]] = defaultdict(list)
    for c in claims:
        groups[(c.subject, c.predicate)].append(c)
    for known_at in (span // 3, span, 2 * span):
        for group in groups.values():
            live = sorted(
                (
                    c
                    for c in group
                    if c.recorded_at <= known_at
                    and (c.superseded_at is None or known_at < c.superseded_at)
                ),
                key=lambda c: c.valid_from,
            )
            for a, b in pairwise(live):
                assert a.valid_to is not None and a.valid_to <= b.valid_from


def test_latest_per_predicate_equals_brute_force(claims: list[ClaimRecord]) -> None:
    """Python mirror of ``as_of_thread_sql``'s algorithm against the reference predicate."""
    span = SMALL.span
    robots = sorted({c.subject for c in claims if c.subject.startswith("robot:")})
    for i, robot in enumerate(robots):
        valid_at = (i + 1) * span // (len(robots) + 2)
        known_at = valid_at + (span if i % 2 else 7 * 86_400 * 10**9)
        mine = [c for c in claims if c.subject == robot]
        brute = {
            c.claim_id
            for c in mine
            if c.visible(valid_clock="fleet_utc", valid_at=valid_at, known_at=known_at)
        }
        fast = set()
        for pred in {c.predicate for c in mine}:
            cands = [
                c
                for c in mine
                if c.predicate == pred
                and c.valid_from <= valid_at
                and c.recorded_at <= known_at
                and (c.superseded_at is None or c.superseded_at > known_at)
            ]
            if cands:
                top = max(cands, key=lambda c: c.valid_from)
                if top.valid_to is None or top.valid_to > valid_at:
                    fast.add(top.claim_id)
        assert fast == brute


def test_csv_has_the_claim_columns_and_empty_cells_for_null(tmp_path: Path) -> None:
    c, e = tmp_path / "c.csv", tmp_path / "e.csv"
    summary = write_dataset(DeploymentSpec(claims=500, robots=6, sites=2), c, e)
    lines = c.read_text(encoding="utf-8").splitlines()
    assert lines[0].split(",") == list(CLAIM_COLUMNS)
    assert len(lines) == summary.claims + 1
    assert all(len(line.split(",")) == len(CLAIM_COLUMNS) for line in lines)
    kinds = {line.split(",")[1] for line in e.read_text(encoding="utf-8").splitlines()[1:]}
    assert kinds == {"robot", "site", "component", "episode", "calibration"}


@pytest.mark.parametrize(
    "kw",
    [
        {"claims": 0},
        {"robots": 0},
        {"sites": 0},
        {"years": 0},
        {"supersede_rate": 1.0},
        {"supersede_rate": -0.1},
    ],
)
def test_spec_rejects_malformed(kw: dict[str, float]) -> None:
    with pytest.raises(ValueError, match="must be"):
        DeploymentSpec(**{"claims": 10, **kw})  # type: ignore[arg-type]
