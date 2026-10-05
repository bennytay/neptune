"""A stub engine that answers from recorded packets (ADR 0004 §1), so consumers build before C2.

``StubEngine`` returns the packet recorded for a query's id and nothing else: it never invents an
answer, so a query it has not recorded is ``not_found``, not an empty packet. Load the golden
packets (``tests/golden/packets``) or any directory of packet JSON with ``from_directory``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from neptune_context.packets.codec import decode
from neptune_context.packets.findings import PacketRefused
from neptune_context.query.codec import query_id
from neptune_context.sdk.errors import ErrorCode, SdkError

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping
    from pathlib import Path

    from neptune_ledger.api import Resolution

    from neptune.model.provenance import EvidenceRef
    from neptune_context.packets.model import ContextPacket
    from neptune_context.query.model import Query


class StubEngine:
    """Answers ``query`` with the packet recorded under its ``query_id``; ``hydrate`` with the
    resolution recorded for the exact evidence ref. Anything else is ``not_found``."""

    def __init__(
        self,
        answers: Mapping[str, ContextPacket],
        resolutions: Mapping[EvidenceRef, Resolution] | None = None,
    ) -> None:
        self._answers = dict(answers)
        self._resolutions = dict(resolutions or {})

    @classmethod
    def from_packets(
        cls,
        packets: Iterable[ContextPacket],
        resolutions: Mapping[EvidenceRef, Resolution] | None = None,
    ) -> StubEngine:
        """A stub keyed by each packet's own ``query_id``; two packets for one query are refused."""
        answers: dict[str, ContextPacket] = {}
        for packet in packets:
            if packet.query_id in answers:
                raise SdkError(
                    ErrorCode.INVALID_ARGUMENT,
                    f"two packets answer the same query {packet.query_id}",
                )
            answers[packet.query_id] = packet
        return cls(answers, resolutions)

    @classmethod
    def from_directory(cls, directory: Path) -> StubEngine:
        """Every ``*.json`` file in ``directory`` (sorted by name) read as a packet."""
        if not directory.is_dir():
            raise SdkError(ErrorCode.INVALID_ARGUMENT, f"not a directory: {directory}")
        packets: list[ContextPacket] = []
        for path in sorted(directory.glob("*.json")):
            decoded = decode(path.read_bytes())
            if isinstance(decoded, PacketRefused):
                first = decoded.findings[0]
                raise SdkError(
                    ErrorCode.INVALID_ARGUMENT,
                    f"{path.name} is not a packet: {first.code} at {first.at!r}: {first.message}",
                )
            packets.append(decoded)
        return cls.from_packets(packets)

    @property
    def query_ids(self) -> tuple[str, ...]:
        """The queries this stub can answer, sorted."""
        return tuple(sorted(self._answers))

    def query(self, query: Query) -> ContextPacket:
        found = self._answers.get(query_id(query))
        if found is None:
            raise SdkError(
                ErrorCode.NOT_FOUND, f"the stub has no packet recorded for {query_id(query)}"
            )
        return found

    def hydrate(self, evidence: EvidenceRef, *, as_of: int | None) -> Resolution:
        del as_of  # a stub holds one snapshot; the resolution says which
        found = self._resolutions.get(evidence)
        if found is None:
            raise SdkError(ErrorCode.NOT_FOUND, "the stub has no resolution recorded for this ref")
        return found
