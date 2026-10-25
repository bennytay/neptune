"""The confirm queue (spec §7): a person answers yes or no on an inferred ``same_as`` proposal.

An answer is a ``confirm`` log entry. A yes becomes a stated copy of the proposal with
``source="confirm"`` (``stated_from_confirm``, used by the fold); a no only clears the queue.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Literal

from neptune_memory.site.facts import Citation, Fact, LogEntry
from neptune_memory.site.log import SEGMENT_SIZE, FactLog, read, segment_path


def proposals(site_dir: Path) -> dict[str, Fact]:
    """Every inferred ``same_as`` proposal in the log, by fact id."""
    return {
        e.fact.id: e.fact
        for e in read(site_dir)
        if e.fact is not None and e.fact.predicate == "same_as" and e.fact.kind == "inferred"
    }


def answers(site_dir: Path) -> dict[str, LogEntry]:
    """The first confirm entry per proposal id."""
    found: dict[str, LogEntry] = {}
    for entry in read(site_dir):
        if entry.confirm is not None:
            found.setdefault(entry.confirm.proposal, entry)
    return found


def pending(site_dir: Path) -> list[Fact]:
    """Proposals nobody has answered yet, in log order."""
    answered = answers(site_dir)
    return [fact for pid, fact in proposals(site_dir).items() if pid not in answered]


def confirm(
    site_dir: Path, proposal_id: str, answer: Literal["yes", "no"], by: str = "operator"
) -> LogEntry:
    """Append a ``confirm`` entry for a proposal in the log."""
    if proposal_id not in proposals(site_dir):
        raise ValueError(f"no proposal {proposal_id} in the log")
    with FactLog(site_dir) as log:
        return log.confirm(proposal_id, answer, by)


def stated_from_confirm(proposal_fact: Fact, confirm_entry: LogEntry, seq: int) -> Fact:
    """The stated copy a yes produces: the proposal's citations plus the confirm line.

    ``seq`` is the confirm entry's log position, which names its line in the segment.
    """
    assert confirm_entry.confirm is not None
    if confirm_entry.confirm.answer != "yes":
        raise ValueError("only a yes becomes a stated fact")
    cite = Citation(
        file=segment_path(Path(".neptune/log"), seq).as_posix(),
        locator=f"confirm line {(seq - 1) % SEGMENT_SIZE + 1}",
        quote=f"{confirm_entry.confirm.by}: yes",
    )
    return replace(
        proposal_fact,
        kind="stated",
        source="confirm",
        citations=(*proposal_fact.citations, cite),
        model=None,
    )


def confirmed_facts(site_dir: Path) -> list[Fact]:
    """The stated copies of every proposal answered yes, in confirm order."""
    found = proposals(site_dir)
    return [
        stated_from_confirm(found[pid], entry, entry.seq)
        for pid, entry in answers(site_dir).items()
        if pid in found and entry.confirm is not None and entry.confirm.answer == "yes"
    ]
