"""The local engine (ADR 0007 §5): the SDK's ``Engine`` seam over a Memory reader and a Ledger.

``LocalEngine.query`` resolves the snapshot once, asks every retrieval channel, fuses their
answers, cuts the fused list to the budget and assembles one ``ContextPacket``. It never ranks a
channel ahead of another (fusion adds ranks) and never writes anything. Parts of a query no
channel serves yet are explicit ``not_covered`` gaps, so an answer never reads as complete when
it is not. ``hydrate`` is the Ledger's ``resolve``.

By default the engine runs the graph channel only; lexical (MVL-142) and vector (MVL-143)
channels are passed in ``channels``. ``explain`` clauses (``why``, ``diff``) are answered by the
``Explainer`` (ADR 0010): its claim and evidence hits are fused with the channels' as peers of the
graph and catalog answers, and its trails give the packet their structure. The client validates
the query and checks the packet answers it (``answer.answer_problems``); this engine re-validates
too, because it may be called directly.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Final

from neptune_memory.schema.codec import graph_from_json
from neptune_memory.schema.interval import ledger_tx

from neptune.identity.canonical_json import dumps
from neptune.identity.hashing import content_id
from neptune.model.ids import ConfigHash
from neptune.model.knowledge import Known
from neptune_context.answer import domain_id
from neptune_context.explain.explainer import Explainer
from neptune_context.explain.history import IndexedReader
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
from neptune_context.retrieve.channel import ChannelAnswer, Retrieval, Snapshot, answer
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
    from neptune_context.explain.history import ClaimHistory
    from neptune_context.explain.run import Caps
    from neptune_context.packets.trails import Trail
    from neptune_context.query.model import Query
    from neptune_context.retrieve.channel import RetrievalChannel

ENGINE_ID: Final = "neptune-context.local"
ENGINE_VERSION: Final = "1"
MAX_GRAPH_BYTES: Final = 1024 * 1024 * 1024  # a graph document read whole into memory


class LocalEngine:
    """An in-process engine over ``memory`` and, optionally, a Ledger ``catalog``.

    ``history`` looks claims up by id for ``why`` (ADR 0010 §2); by default ``memory`` itself
    when it offers ``ClaimHistory`` (``read_graph``'s reader does), else ``why`` is a gap.
    """

    def __init__(
        self,
        memory: MemoryReader,
        catalog: CatalogApi | None = None,
        *,
        channels: Sequence[RetrievalChannel] | None = None,
        history: ClaimHistory | None = None,
        explain_caps: Caps | None = None,
    ) -> None:
        self._memory = memory
        self._catalog = catalog
        self._explainer = Explainer(memory, catalog, history=history, caps=explain_caps)
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
            "channels": [c.config for c in sorted(self._channels, key=lambda c: str(c.channel))],
            "explain": self._explainer.config,
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
        answers = [_retrieve(channel, request) for channel in self._channels]
        trails: tuple[Trail, ...] = ()
        if query.explain:
            explained = self._explainer.explain(request)
            answers = _with(answers, explained.answers)
            trails = explained.trails
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
            trails=trails,
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


def _retrieve(channel: RetrievalChannel, request: Retrieval) -> ChannelAnswer:
    """``channel.retrieve``; a channel that raises loses its own answer (a gap), not others'."""
    try:
        return channel.retrieve(request)
    except Exception as exc:  # partial success: one failing channel is a gap
        detail = f"the {channel.channel} channel failed: {type(exc).__name__}: {exc}"
        return ChannelAnswer(
            channel.channel,
            gaps=(Gap(GapCode.NOT_COVERED, "", channel.channel, (), detail[:2000]),),
        )


def _with(answers: Sequence[ChannelAnswer], extra: Sequence[ChannelAnswer]) -> list[ChannelAnswer]:
    """``answers`` with the explainer's folded in: an explain answer joins the channel answer of
    the same channel (best score per item, as within one channel), else stands as its own."""
    out = {a.channel: a for a in answers}
    for more in extra:
        held = out.get(more.channel)
        if held is None:
            out[more.channel] = more
            continue
        out[more.channel] = answer(
            more.channel,
            [(hit.relevance.score, hit) for hit in (*held.hits, *more.hits)],
            gaps=(*held.gaps, *more.gaps),
            findings=(*held.findings, *more.findings),
            superseded=(*held.superseded, *more.superseded),
        )
    return [out[channel] for channel in sorted(out, key=str)]


def _no_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    out: dict[str, object] = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"duplicate key {key!r}")
        out[key] = value
    return out


def _no_constant(token: str) -> object:
    raise ValueError(f"{token} is not JSON")


def read_graph(path: Path) -> IndexedReader:
    """A Memory graph document at ``path``, read strictly (bounded size, no duplicate keys, no NaN)
    and decoded by Memory's codec (ids, order, generation all checked) into Memory's reference
    reader, indexed by claim id for ``why`` (``IndexedReader``). Raises ``ValueError`` or
    ``OSError``; nothing else."""
    with path.open("rb") as handle:
        data = handle.read(MAX_GRAPH_BYTES + 1)
    if len(data) > MAX_GRAPH_BYTES:
        raise ValueError(f"{path.name} is larger than {MAX_GRAPH_BYTES} bytes")
    try:
        document = json.loads(data, object_pairs_hook=_no_duplicates, parse_constant=_no_constant)
        return IndexedReader(graph_from_json(document))
    except RecursionError as exc:
        raise ValueError(f"{path.name} is nested too deeply") from exc
    except (TypeError, KeyError) as exc:
        raise ValueError(f"{path.name} is not a Memory graph document: {exc}") from exc


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
    return tuple(gaps[k] for k in sorted(gaps))
