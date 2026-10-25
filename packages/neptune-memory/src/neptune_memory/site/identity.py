"""Identity proposals (spec §7): "these two names may be one thing", never merged on their own.

Two entities of the same type that carry the same ``serial`` from different sources get one inferred
``same_as`` fact citing both; the subject is the first of the two refs ignoring case
(``robot:amr-02 same_as robot:MR-1142``). Only a ``confirm`` yes (``confirm.py``) makes it a
stated fact.
"""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING

from neptune_memory.site.facts import Fact, Ref, Value
from neptune_memory.site.log import FactLog, read

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

PROPOSER = "identity"


def propose_same_as(facts: Iterable[Fact]) -> list[Fact]:
    """One inferred ``same_as`` per pair of same-type entities sharing a serial across sources."""
    by_serial: dict[str, dict[str, Fact]] = defaultdict(dict)
    for fact in facts:
        if fact.predicate == "serial" and isinstance(fact.object, Value):
            by_serial[str(fact.object.value)].setdefault(fact.subject, fact)

    proposals: list[Fact] = []
    for serial in sorted(by_serial):
        subjects = sorted(by_serial[serial], key=lambda ref: (ref.casefold(), ref))
        for i, left in enumerate(subjects):
            for right in subjects[i + 1 :]:
                a, b = by_serial[serial][left], by_serial[serial][right]
                if a.source == b.source or left.split(":")[0] != right.split(":")[0]:
                    continue
                proposals.append(
                    Fact(
                        subject=left,
                        predicate="same_as",
                        object=Ref(right),
                        valid_from=None,
                        valid_to=None,
                        kind="inferred",
                        source=PROPOSER,
                        citations=(*a.citations, *b.citations),
                    )
                )
    return proposals


def propose(site_dir: Path) -> list[Fact]:
    """Append any proposals the log does not hold yet; return the new ones."""
    facts = [e.fact for e in read(site_dir) if e.fact is not None]
    known = {f.id for f in facts if f.predicate == "same_as"}
    new = [p for p in propose_same_as(facts) if p.id not in known]
    if new:
        with FactLog(site_dir) as log:
            for proposal in new:
                log.fact(proposal)
    return new
