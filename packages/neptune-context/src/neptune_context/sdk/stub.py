"""A stub engine that answers from recorded packets (ADR 0004 §1), so consumers build before C2.

``StubEngine`` returns the packet recorded for a query's id and nothing else: it never invents an
answer, so a query it has not recorded is ``not_found``, not an empty packet. Load the golden
packets (``tests/golden/packets``) or any directory of packet JSON with ``from_directory``.
"""

from __future__ import annotations

import os
import stat
from typing import TYPE_CHECKING

from neptune_context.packets.codec import decode
from neptune_context.packets.findings import PacketRefused
from neptune_context.packets.model import MAX_PACKET_BYTES
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
        """Every regular ``*.json`` file in ``directory`` (sorted by name) read as a packet.

        Symbolic links and anything that is not a regular file are skipped, never followed; a
        file's size is checked before a byte is read (the packet maximum), and the read itself is
        bounded, so a file that grows after the check is still refused. A directory with no
        packet file is refused: a stub that can answer nothing is a mistake, not a server.
        """
        if not directory.is_dir():
            raise SdkError(ErrorCode.INVALID_ARGUMENT, f"not a directory: {directory}")
        packets: list[ContextPacket] = []
        for path in sorted(directory.iterdir()):
            data = _packet_file(path)
            if data is None:
                continue
            decoded = decode(data)
            if isinstance(decoded, PacketRefused):
                first = decoded.findings[0]
                raise SdkError(
                    ErrorCode.INVALID_ARGUMENT,
                    f"{path.name} is not a packet: {first.code} at {first.at!r}: {first.message}",
                )
            packets.append(decoded)
        if not packets:
            raise SdkError(ErrorCode.INVALID_ARGUMENT, f"no packet files in {directory}")
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


def _packet_file(path: Path) -> bytes | None:
    """The bytes of ``path`` if it is a regular ``.json`` file within the packet maximum; ``None``
    for a file to skip (a symlink, a directory, a device, another suffix)."""
    if path.suffix != ".json":
        return None
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        return None
    if info.st_size > MAX_PACKET_BYTES:
        raise SdkError(
            ErrorCode.INVALID_ARGUMENT,
            f"{path.name} is {info.st_size} bytes, over the packet maximum {MAX_PACKET_BYTES}",
        )
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise SdkError(ErrorCode.INVALID_ARGUMENT, f"{path.name}: {error.strerror}") from None
    with os.fdopen(descriptor, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            return None  # replaced by something else between the check and the open
        data = handle.read(MAX_PACKET_BYTES + 1)
    if len(data) > MAX_PACKET_BYTES:
        raise SdkError(ErrorCode.INVALID_ARGUMENT, f"{path.name} is over the packet maximum")
    return data
