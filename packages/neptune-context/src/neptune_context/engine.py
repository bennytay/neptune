"""The local engine (ADR 0007 §5): the SDK's ``Engine`` seam over a Memory reader and a Ledger.

``LocalEngine.query`` resolves the snapshot once, asks every retrieval channel, fuses their
answers, cuts the fused list to the budget and assembles one ``ContextPacket``. It never ranks a
channel ahead of another (fusion adds ranks) and never writes anything. Parts of a query no
channel serves yet are explicit ``not_covered`` gaps, so an answer never reads as complete when
it is not. ``hydrate`` is the Ledger's ``resolve``.

By default the engine runs the graph channel only; lexical (MVL-142) and vector (MVL-143)
channels are passed in ``channels``. The client validates the query and checks the packet
answers it (``answer.answer_problems``); this engine re-validates too, because it may be called
directly.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Final

from neptune_memory.schema.codec import graph_from_json
from neptune_memory.schema.interval import ledger_tx
from neptune_memory.schema.reference import ReferenceReader

from neptune.identity.canonical_json import dumps
from neptune.identity.hashing import content_id
from neptune.model.ids import ConfigHash
from neptune.model.knowledge import Known
from neptune_context.answer import domain_id
from neptune_context.packets.model import (
    BudgetUse,
    Channel,
    ClaimItem,
    ContextPacket,
    During,
    Gap,
    GapCode,
    LedgerSnapshot,
    Limits,
    MemorySnapshot,
)
from neptune_context.packets.model import Engine as ProducedBy
from neptune_context.pins import CATALOG_API_VERSION
from neptune_context.query.codec import query_id
from neptune_context.query.decode import accept
from neptune_context.query.findings import Refused
from neptune_context.retrieve.channel import Retrieval, Snapshot
from neptune_context.retrieve.fusion import RRF_K, cut, fuse
from neptune_context.retrieve.graph import GraphChannel
from neptune_context.sdk.errors import ErrorCode, SdkError

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from neptune_ledger.api import CatalogApi, Resolution
    from neptune_memory.schema.reader import MemoryReader

    from neptune.model.jsonvalue import JsonObject
    from neptune.model.provenance import EvidenceRef
    from neptune_context.query.model import Query
    from neptune_context.retrieve.channel import ChannelAnswer, RetrievalChannel

ENGINE_ID: Final = "neptune-context.local"
ENGINE_VERSION: Final = "1"
MAX_GRAPH_BYTES: Final = 1024 * 1024 * 1024  # a graph document read whole into memory


class LocalEngine:
    """An in-process engine over ``memory`` and, optionally, a Ledger ``catalog``."""

    def __init__(
        self,
        memory: MemoryReader,
        catalog: CatalogApi | None = None,
        *,
        channels: Sequence[RetrievalChannel] | None = None,
    ) -> None:
        self._memory = memory
        self._catalog = catalog
        self._channels: tuple[RetrievalChannel, ...] = (
            (GraphChannel(memory, catalog),) if channels is None else tuple(channels)
        )
        names = [str(c.channel) for c in self._channels]
        if not names or len(set(names)) != len(names):
            raise SdkError(ErrorCode.INVALID_ARGUMENT, "an engine runs each channel once")

    @property
    def config(self) -> JsonObject:
        """What decides this engine's answers: its channels' settings and fusion's."""
        return {
            "channels": [
                getattr(c, "config", {"channel": str(c.channel)})
                for c in sorted(self._channels, key=lambda c: str(c.channel))
            ],
            "fusion": {"k": RRF_K, "rule": "rrf"},
        }

    @property
    def produced_by(self) -> ProducedBy:
        return ProducedBy(ENGINE_ID, ENGINE_VERSION, ConfigHash(content_id(dumps(self.config))))

    def snapshot(self, query: Query) -> Snapshot:
        """``as_of`` (the query's, or the head), the head every source knows, Memory's snapshot."""
        memory_head = int(self._memory.head)
        head = memory_head if self._catalog is None else max(memory_head, self._catalog_head())
        as_of = head if query.as_of == "head" else int(query.as_of)
        if as_of > head:
            raise SdkError(
                ErrorCode.NOT_FOUND,
                f"as_of {as_of} is beyond the latest transaction this engine knows ({head})",
            )
        return Snapshot(ledger_tx(as_of), ledger_tx(head), ledger_tx(min(as_of, memory_head)))

    def _catalog_head(self) -> int:
        """The catalog's latest committed point: the point a one-row ``query`` at head reports."""
        from neptune_ledger.api import QueryBudget, QuerySpec, query_meta

        assert self._catalog is not None
        meta = query_meta(
            self._catalog.query(QuerySpec(kinds=("stream",), limit=1, budget=QueryBudget(1)))
        )
        if isinstance(meta.as_of, Known):
            return int(meta.as_of.value.tx_seq)
        return 0

    def query(self, query: Query) -> ContextPacket:
        accepted = accept(query)
        if isinstance(accepted, Refused):
            first = accepted.findings[0]
            raise SdkError(
                ErrorCode.QUERY_REFUSED,
                f"{len(accepted.findings)} finding(s); first: {first.code} at {first.at}",
                findings=accepted.findings,
            )
        query = accepted
        snapshot = self.snapshot(query)
        request = Retrieval(query, snapshot)
        answers = [channel.retrieve(request) for channel in self._channels]
        limits = Limits(
            query.budget.items, query.budget.tokens, query.budget.bytes, query.budget.latency_ms
        )
        fitted = cut(fuse(answers), limits)
        items = fitted.kept
        claims = {i.claim.id for i in items if isinstance(i, ClaimItem)}
        findings = {f.id: f for a in answers for f in a.findings if {f.claim, *f.others} & claims}
        superseded = {s.claim: s for a in answers for s in a.superseded if s.claim in claims}
        during = None
        if query.during is not None:
            during = During(
                domain_id(query.during.clock),  # type: ignore[arg-type]
                query.during.start,
                query.during.end,
            )
        return ContextPacket(
            query_id=query_id(query),
            as_of=snapshot.as_of,
            head=snapshot.head,
            during=during,
            memory=MemorySnapshot(
                self._memory.graph_schema_version, self._memory.generation, snapshot.memory_as_of
            ),
            ledger=LedgerSnapshot(CATALOG_API_VERSION),
            produced_by=self.produced_by,
            inference_included=query.include_inferred,
            budget=BudgetUse.measured(
                limits, items, dropped=fitted.dropped, exhausted=fitted.exhausted
            ),
            items=items,
            superseded_since=tuple(superseded[k] for k in sorted(superseded)),
            findings=tuple(
                sorted(
                    findings.values(),
                    key=lambda f: (f.recorded_at, f.claim, str(f.code), f.others),
                )
            ),
            gaps=_gaps(query, answers),
        )

    def hydrate(self, evidence: EvidenceRef, *, as_of: int | None) -> Resolution:
        if self._catalog is None:
            raise SdkError(ErrorCode.UNAVAILABLE, "no Ledger catalog is attached to this engine")
        from neptune_ledger.api import CodecError, EvidenceAnchor, from_json

        try:
            anchor = from_json(EvidenceAnchor, evidence.to_json())
        except CodecError as exc:
            raise SdkError(
                ErrorCode.INVALID_ARGUMENT, f"not a Ledger evidence anchor: {exc}"
            ) from exc
        return self._catalog.resolve(anchor, as_of=as_of)


def read_graph(path: Path) -> ReferenceReader:
    """A Memory graph document at ``path``, read strictly by Memory's codec (ids, order,
    generation all checked), as Memory's reference reader. Raises ``ValueError`` or ``OSError``."""
    data = path.read_bytes()
    if len(data) > MAX_GRAPH_BYTES:
        raise ValueError(f"{path.name} is larger than {MAX_GRAPH_BYTES} bytes")
    return ReferenceReader(graph_from_json(json.loads(data)))


def _gaps(query: Query, answers: Sequence[ChannelAnswer]) -> tuple[Gap, ...]:
    """Every channel's gaps, plus the query members no channel serves yet."""
    gaps = {g.sort_key(): g for a in answers for g in a.gaps}
    served = {a.channel for a in answers}
    if query.text is not None and not served & {Channel.LEXICAL, Channel.VECTOR}:
        gap = Gap(
            GapCode.NOT_COVERED,
            "/text",
            None,
            (),
            "no lexical or vector channel is attached (MVL-142, MVL-143): the text clause is"
            " not searched",
        )
        gaps[gap.sort_key()] = gap
    for index, _ in enumerate(query.explain):
        gap = Gap(
            GapCode.NOT_COVERED,
            f"/explain/{index}",
            None,
            (),
            "why and diff trails are not assembled by this engine yet (MVL-149)",
        )
        gaps[gap.sort_key()] = gap
    return tuple(gaps[k] for k in sorted(gaps))
