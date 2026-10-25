"""Identity proposals and the confirm queue: the happy path (spec §7)."""

from pathlib import Path

import pytest

from neptune_memory.site.__main__ import main
from neptune_memory.site.confirm import confirm, confirmed_facts, pending, stated_from_confirm
from neptune_memory.site.facts import Citation, Fact, Kind, Ref, Value
from neptune_memory.site.identity import propose
from neptune_memory.site.log import FactLog, read

FLEET = Citation("feed/fleet.jsonl", "line 41", '"serialNumber": "SN-88412"')
CMMS = Citation("static/cmms/assets.csv", 'row 3 col "Serial"', "SN-88412")


def _serial(subject: str, source: str, kind: Kind, cite: Citation) -> Fact:
    return Fact(subject, "serial", Value("SN-88412"), None, None, kind, source, (cite,))


def _site(tmp_path: Path) -> Path:
    with FactLog(tmp_path) as log:
        log.fact(_serial("robot:amr-02", "fleet", "observed", FLEET))
        log.fact(_serial("robot:MR-1142", "cmms", "stated", CMMS))
        log.fact(
            Fact(
                "robot:amr-01",
                "serial",
                Value("SN-10001"),
                None,
                None,
                "observed",
                "fleet",
                (FLEET,),
            )
        )
    return tmp_path


def test_site_identity_one_proposal_citing_both(tmp_path: Path) -> None:
    site = _site(tmp_path)
    [proposal] = propose(site)
    assert (proposal.predicate, proposal.kind) == ("same_as", "inferred")
    assert isinstance(proposal.object, Ref)
    assert {proposal.subject, proposal.object.ref} == {"robot:amr-02", "robot:MR-1142"}
    assert set(proposal.citations) == {FLEET, CMMS}
    assert propose(site) == []  # idempotent
    assert pending(site) == [proposal]
    assert confirmed_facts(site) == []  # never auto-merged


def test_site_confirm_yes_gives_stated_copy(tmp_path: Path) -> None:
    site = _site(tmp_path)
    [proposal] = propose(site)
    entry = confirm(site, proposal.id, "yes")
    [stated] = confirmed_facts(site)
    assert stated == stated_from_confirm(proposal, entry, entry.seq)
    assert (stated.kind, stated.source) == ("stated", "confirm")
    assert (stated.subject, stated.object) == (proposal.subject, proposal.object)
    assert stated.citations[:-1] == proposal.citations
    assert stated.citations[-1].locator == f"confirm line {entry.seq}"
    assert pending(site) == []
    assert [e.type for e in read(site)][-1] == "confirm"


def test_site_confirm_no_gives_nothing(tmp_path: Path) -> None:
    site = _site(tmp_path)
    [proposal] = propose(site)
    confirm(site, proposal.id, "no")
    assert confirmed_facts(site) == []
    assert pending(site) == []
    assert propose(site) == []  # not asked again


def test_site_confirm_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    site = _site(tmp_path)
    assert main(["confirm", str(site)]) == 0
    out = capsys.readouterr().out
    assert "1 pending" in out
    assert "robot:amr-02" in out
    [proposal] = pending(site)
    assert main(["confirm", str(site), "--yes", proposal.id]) == 0
    assert "0 pending" in capsys.readouterr().out
    assert len(confirmed_facts(site)) == 1
