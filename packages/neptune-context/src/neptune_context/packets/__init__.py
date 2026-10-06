"""Context packets: the typed, provenance-carrying answer a query returns (ADR 0003).

- ``model``: ``ContextPacket``, its header, the seven item kinds, budgets, gaps; ``PACKET_VERSION``.
- ``codec``: canonical bytes, ids and the strict reader ``decode`` (findings, never exceptions).
- ``trails``: the structure of a ``why`` or ``diff`` answer (ADR 0010), by claim id.
- ``findings``: ``PacketFinding`` codes; ``schema``: the JSON Schema export;
  ``conformance``: the checks a consumer runs over packets it reads.

A packet item without provenance and ``assertion_kind`` cannot be built; missing evidence is a
``Knowledge`` state or a ``Gap``, never an absent field. The packet names its query only by
``query_id`` (ADR 0002's canonical query hash), so this package never depends on the query model.
"""

from neptune_context.packets.model import PACKET_VERSION

__all__ = ["PACKET_VERSION"]
