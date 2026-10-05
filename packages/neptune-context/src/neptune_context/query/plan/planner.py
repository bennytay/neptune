"""``plan``: English to a typed, validated, visible Query; never an answer (ADR 0005).

One call asks one model once. The model's text is read with the MVL-108 decoder; a refusal there is
an ``INVALID`` plan with the validator's own findings, never a second attempt. A valid query is then
checked against what the question and the Ledger actually state: entities must be declared
(ambiguity is reported with every candidate), every clock must be the question's, an entity's
declared primary clock or the caller's declared default, and each default the planner applied is
stated as an info finding. Parts that fail those checks are removed from the draft and reported as
blocking findings, so the draft shown is always one a person can edit and run, and ``executable``
is true only when nothing blocks.
"""

from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

from neptune_context.query import decode
from neptune_context.query.model import (
    HEAD,
    INT64_MAX,
    MAX_TEXT_CHARS,
    QUERY_VERSION,
    AsOf,
    Budget,
    Caller,
    CivilTime,
    Clock,
    Diff,
    DomainClock,
    Explain,
    FrameRef,
    Instant,
    Query,
    Subject,
    Why,
    default_include_inferred,
)
from neptune_context.query.plan import prompt
from neptune_context.query.plan.client import (
    DEFAULT_MAX_TOKENS,
    DEFAULT_MODEL,
    ModelClient,
    ModelUnavailable,
)
from neptune_context.query.plan.resolver import Entity, EntityResolver, Mention
from neptune_context.query.plan.result import (
    ModelLineage,
    PlanFinding,
    PlannedQuery,
    PlanStatus,
    Severity,
    response_sha256,
)
from neptune_context.query.plan.result import (
    PlanFindingCode as Code,
)
from neptune_context.query.schema import SCHEMA_ID
from neptune_context.query.validate import validate

if TYPE_CHECKING:
    from neptune_context.query.findings import Refused

MAX_QUESTION_CHARS: Final = MAX_TEXT_CHARS


@dataclass(frozen=True)
class Defaults:
    """What the caller declares up front; every use of one is stated back as an info finding.

    ``caller`` picks ``include_inferred`` (an agent sees labelled inferences, a control policy
    does not). ``clock`` is the caller's declared default world clock and ``civil_time`` the civil
    clock to use when a question names its timescale; ``frames`` and ``length_unit`` are the
    caller's declared frames and unit. None is guessed: a question needing one that is not here
    or declared by an entity is returned as ``NEEDS_INPUT``.
    """

    caller: Caller
    budget: Budget = field(default_factory=lambda: Budget(items=100))
    clock: Clock | None = None
    civil_time: CivilTime | None = None
    frames: tuple[FrameRef, ...] = ()
    length_unit: str | None = None

    @property
    def include_inferred(self) -> bool:
        return default_include_inferred(self.caller)


_RELATIVE_TIME: Final = re.compile(
    r"\b(?:yesterday|today|tonight|overnight|last\s+(?:night|week|month|year|hour|\d+\s+\w+)"
    r"|this\s+(?:morning|afternoon|evening|week|month)"
    r"|past\s+(?:\d+\s+)?(?:minutes?|hours?|days?|weeks?|months?|years?)|recently)\b",
    re.IGNORECASE,
)
_ANCHOR: Final = re.compile(r"\d{4}-\d{2}-\d{2}|\d{6,}")  # an explicit ISO date or a tick count
_INFERENCE: Final = re.compile(r"infer|evidence only|observed only", re.IGNORECASE)
_TIMESCALES: Final = {"utc": "utc", "tai": "tai", "gps": "gps", "posix": "posix", "unix": "posix"}
_OWN_CLOCK: Final = re.compile(
    r"\b(?:own|native|primary|source|device|vehicle|robot|machine|onboard|its)\s+clock\b",
    re.IGNORECASE,
)
_UNIT_WORDS: Final = {
    "m": ("meter", "meters", "metre", "metres"),
    "mm": ("millimeter", "millimeters", "millimetre", "millimetres"),
    "cm": ("centimeter", "centimeters", "centimetre", "centimetres"),
    "km": ("kilometer", "kilometers", "kilometre", "kilometres"),
    "ft": ("foot", "feet"),
    "in": ("inch", "inches"),
}


def _info(code: Code, at: str, message: str, *details: str) -> PlanFinding:
    return PlanFinding(code, Severity.INFO, at, message, details)


def _block(code: Code, at: str, message: str, *details: str) -> PlanFinding:
    return PlanFinding(code, Severity.BLOCKING, at, message, details)


def _clock_name(clock: Clock) -> str:
    if isinstance(clock, DomainClock):
        return f"domain clock {clock.domain_id}"
    return f"civil {clock.timescale}/{clock.epoch} at {clock.resolution} s per tick"


def _unit_stated(text: str, unit: str) -> bool:
    """The unit is written next to a number (``15 m``) or spelled out (``metres``)."""
    pattern = rf"\d\s*{re.escape(unit)}(?![A-Za-z])"
    if re.search(pattern, text):
        return True
    return any(
        re.search(rf"(?<![A-Za-z]){word}(?![A-Za-z])", text, re.IGNORECASE)
        for word in _UNIT_WORDS.get(unit, ())
    )


def _quoted(text: str, token: str) -> bool:
    return re.search(rf"(?<![0-9A-Za-z]){re.escape(token)}(?![0-9A-Za-z])", text) is not None


class _Review:
    """Checks one decoded query against the question, the resolver and the defaults."""

    def __init__(
        self,
        text: str,
        as_of: AsOf,
        defaults: Defaults,
        mentions: tuple[Mention, ...],
        resolver: EntityResolver,
        snapshot: int | None,
    ) -> None:
        self.text = text
        self.as_of = as_of
        self.defaults = defaults
        self.mentions = mentions
        self.resolver = resolver
        self.snapshot = snapshot
        self.findings: list[PlanFinding] = []
        self.entities: dict[str, Entity] = {}
        self.named = {
            _TIMESCALES[w.lower()]
            for w in re.findall(r"[A-Za-z]+", text)
            if w.lower() in _TIMESCALES
        }

    # -- entities -----------------------------------------------------------------------------

    def _lookup(self, subject: Subject, at: str) -> None:
        if subject.declared_id is None or subject.declared_id in self.entities:
            return
        entity = self.resolver.lookup(subject.declared_id, as_of=self.snapshot)
        if entity is None:
            self.findings.append(
                _block(
                    Code.UNKNOWN_ENTITY,
                    at,
                    f"no declared identifier {subject.declared_id!r} exists in the Ledger",
                    subject.declared_id,
                )
            )
            return
        self.entities[subject.declared_id] = entity
        if entity.kind != subject.kind:
            self.findings.append(
                _block(
                    Code.ENTITY_KIND_MISMATCH,
                    at,
                    f"{subject.declared_id!r} is declared as a {entity.kind}, not a {subject.kind}",
                    subject.declared_id,
                    entity.kind,
                )
            )

    def entities_of(self, query: Query) -> None:
        for index, subject in enumerate(sorted(query.subjects, key=_subject_key)):
            self._lookup(subject, f"/subjects/{index}")
        for index, item in enumerate(query.explain):
            if isinstance(item, Diff):
                self._lookup(item.subject, f"/explain/{index}/subject")
        if query.site is not None:
            for declared in (query.site.site, *sorted(query.site.zones)):
                at = "/site"
                if (
                    declared not in self.entities
                    and self.resolver.lookup(declared, as_of=self.snapshot) is None
                ):
                    self.findings.append(
                        _block(
                            Code.UNKNOWN_ENTITY,
                            at,
                            f"no declared identifier {declared!r} exists in the Ledger",
                            declared,
                        )
                    )
        for mention in self.mentions:
            if mention.ambiguous:
                ids = tuple(c.declared_id for c in mention.candidates)
                self.findings.append(
                    _block(
                        Code.AMBIGUOUS_ENTITY,
                        "/subjects",
                        f"{mention.text!r} could mean any of {len(ids)} declared entities; "
                        "choose one before running",
                        *ids,
                    )
                )

    # -- clocks -------------------------------------------------------------------------------

    def _primary_clocks(self) -> list[Clock]:
        return [e.primary_clock for e in self.entities.values() if e.primary_clock is not None]

    def classify_clock(self, clock: Clock, at: str) -> bool:
        """True when ``clock`` is the question's, an entity's declared one or the caller's."""
        if isinstance(clock, CivilTime) and clock.timescale in self.named:
            declared = self.defaults.civil_time
            if clock == declared:
                return True
            have = (
                "declares no civil clock"
                if declared is None
                else f"declares {_clock_name(declared)}"
            )
            self.findings.append(
                _block(
                    Code.CLOCK_NOT_DECLARED,
                    at,
                    f"the model used {_clock_name(clock)} but the caller {have}; a civil "
                    "clock's epoch and tick length are never guessed",
                    clock.timescale,
                )
            )
            return False
        if _OWN_CLOCK.search(self.text) and clock in self._primary_clocks():
            return True
        primary = [
            e
            for e in self.entities.values()
            if e.primary_clock is not None and e.primary_clock == clock
        ]
        if primary:
            who = ", ".join(sorted(e.declared_id for e in primary))
            self.findings.append(
                _info(
                    Code.CLOCK_DEFAULTED_TO_PRIMARY,
                    at,
                    f"the question names no clock; used the primary clock of {who} "
                    f"({_clock_name(clock)})",
                    who,
                )
            )
            return True
        if clock == self.defaults.clock:
            self.findings.append(
                _info(
                    Code.CLOCK_DEFAULTED_TO_CALLER,
                    at,
                    f"no clock named; used the caller's default {_clock_name(clock)}",
                )
            )
            return True
        if isinstance(clock, CivilTime):
            self.findings.append(
                _block(
                    Code.CLOCK_NOT_STATED,
                    at,
                    f"the question does not name the {clock.timescale} timescale; "
                    "say which clock the times are on",
                    clock.timescale,
                )
            )
        else:
            self.findings.append(
                _block(
                    Code.CLOCK_NOT_DECLARED,
                    at,
                    f"{_clock_name(clock)} is not the primary clock of any entity in the query "
                    "or the caller's default",
                    clock.domain_id,
                )
            )
        return False

    def pool(self) -> list[Entity]:
        """Entities whose declarations the query may rely on: those it names and those the
        question mentions (a frame or bridge is declared for an entity, wherever it is used)."""
        mentioned = [c for m in self.mentions for c in m.candidates]
        return [*self.entities.values(), *mentioned]

    # -- the review ---------------------------------------------------------------------------

    def run(self, query: Query) -> Query:
        """Checks ``query``; returns it with every blocked part removed (findings explain each)."""
        out = self._review_defaults(query)
        self.entities_of(out)
        explain = self._review_explain(out)
        out = dataclasses.replace(out, explain=tuple(item for _, item in explain))
        out = self._review_time(out)
        return self._review_space(out)

    def _review_defaults(self, query: Query) -> Query:
        out = query
        defaults = self.defaults
        if query.as_of != self.as_of:
            stated = isinstance(query.as_of, int) and _quoted(self.text, str(query.as_of))
            if not stated:
                self.findings.append(
                    _info(
                        Code.AS_OF_OVERRIDDEN,
                        "/as_of",
                        f"the model set as_of {query.as_of!r} but the question states no "
                        f"transaction; used the caller's {self.as_of!r}",
                    )
                )
                out = dataclasses.replace(out, as_of=self.as_of)
        elif self.as_of == HEAD:
            self.findings.append(
                _info(
                    Code.AS_OF_DEFAULT,
                    "/as_of",
                    "as_of is the reader's head; the packet records the transaction it resolved to",
                )
            )
        if out.include_inferred != defaults.include_inferred and not _INFERENCE.search(self.text):
            self.findings.append(
                _info(
                    Code.INCLUDE_INFERRED_OVERRIDDEN,
                    "/include_inferred",
                    f"the model set include_inferred {out.include_inferred} but the question does "
                    f"not mention inference; used the {defaults.caller} default",
                )
            )
            out = dataclasses.replace(out, include_inferred=defaults.include_inferred)
        if out.include_inferred == defaults.include_inferred:
            self.findings.append(
                _info(
                    Code.INCLUDE_INFERRED_DEFAULT,
                    "/include_inferred",
                    f"include_inferred is {out.include_inferred} ({defaults.caller} default)",
                )
            )
        limits = [
            v
            for v in (out.budget.items, out.budget.tokens, out.budget.bytes, out.budget.latency_ms)
            if v is not None
        ]
        if out.budget != defaults.budget and not all(_quoted(self.text, str(v)) for v in limits):
            self.findings.append(
                _info(
                    Code.BUDGET_OVERRIDDEN,
                    "/budget",
                    "the model set a budget the question does not state; used the caller's default",
                )
            )
            out = dataclasses.replace(out, budget=defaults.budget)
        if out.budget == defaults.budget:
            self.findings.append(
                _info(
                    Code.BUDGET_DEFAULT,
                    "/budget",
                    f"budget is the caller's default: {out.budget.items} items",
                )
            )
        return out

    def _review_explain(self, query: Query) -> list[tuple[int, Explain]]:
        """Each explain item with its index in the model's tuple (pointers stay meaningful)."""
        kept: list[tuple[int, Explain]] = []
        relative = self._unresolved_phrase()
        for index, item in enumerate(query.explain):
            at = f"/explain/{index}"
            if isinstance(item, Why) and not _quoted(self.text, item.claim_id):
                self.findings.append(
                    _block(
                        Code.CLAIM_NOT_QUOTED,
                        at,
                        "a claim id must be quoted in the question; it cannot be inferred",
                        item.claim_id,
                    )
                )
                continue
            if isinstance(item, Diff):
                clocks = [p.clock for p in (item.before, item.after) if isinstance(p, Instant)]
                if clocks and relative is not None:
                    self._unresolved(relative, at)
                    continue
                if not all([self.classify_clock(c, at) for c in clocks]):
                    continue
            kept.append((index, item))
        return kept

    def _unresolved_phrase(self) -> str | None:
        """A relative time phrase with no explicit date or tick count to anchor it, if any."""
        bare = self.text
        for mention in self.mentions:
            bare = bare.replace(mention.text, " ")
        match = _RELATIVE_TIME.search(bare)
        return match.group(0) if match and not _ANCHOR.search(bare) else None

    def _unresolved(self, phrase: str, at: str) -> None:
        self.findings.append(
            _block(
                Code.TIME_PHRASE_UNRESOLVED,
                at,
                f"{phrase!r} is relative to now, which the planner does not read; "
                "state the interval",
                phrase,
            )
        )

    def _review_time(self, query: Query) -> Query:
        out = query
        phrase = self._unresolved_phrase()
        during = out.during
        if during is not None:
            if phrase is not None:
                self._unresolved(phrase, "/during")
                out = dataclasses.replace(out, during=None)
            elif not self.classify_clock(during.clock, "/during/clock"):
                out = dataclasses.replace(out, during=None)
        known = {b for e in self.pool() for b in e.clock_bridges}
        declared = {b for b in out.clock_bridges if b in known}
        for bridge in sorted(out.clock_bridges - declared, key=lambda b: b.mapping_id):
            self.findings.append(
                _block(
                    Code.BRIDGE_NOT_DECLARED,
                    "/clock_bridges",
                    f"clock mapping {bridge.mapping_id} is not declared for the query's entities",
                    bridge.mapping_id,
                )
            )
        used: set[Clock] = {out.during.clock} if out.during is not None else set()
        for item in out.explain:
            if isinstance(item, Diff):
                used |= {p.clock for p in (item.before, item.after) if isinstance(p, Instant)}
        # A bridge exists to place one used clock on another; with its clocks gone it is dropped.
        declared = {b for b in declared if b.source in used or b.target in used}
        return dataclasses.replace(out, clock_bridges=frozenset(declared))

    def _review_space(self, query: Query) -> Query:
        pooled = {f for e in self.pool() for f in e.frames}
        keep = []
        for region in sorted(
            query.regions, key=lambda r: (r.frame.graph_id, r.frame.frame_id, r.unit)
        ):
            if region.frame not in pooled and region.frame not in self.defaults.frames:
                self.findings.append(
                    _block(
                        Code.FRAME_NOT_DECLARED,
                        "/regions",
                        f"frame {region.frame.frame_id!r} of {region.frame.graph_id} is not "
                        "declared for the query's entities or the caller; frames are never guessed",
                        region.frame.frame_id,
                    )
                )
                continue
            stated = _unit_stated(self.text, region.unit)
            if not stated and region.unit != self.defaults.length_unit:
                self.findings.append(
                    _block(
                        Code.UNIT_NOT_STATED,
                        "/regions",
                        f"the question states no unit {region.unit!r}; units are never guessed",
                        region.unit,
                    )
                )
                continue
            if not stated:
                self.findings.append(
                    _info(
                        Code.UNIT_DEFAULTED_TO_CALLER,
                        "/regions",
                        f"the question states no unit; used the caller's {region.unit!r}",
                        region.unit,
                    )
                )
            if region.frame not in pooled:
                self.findings.append(
                    _info(
                        Code.FRAME_DEFAULTED_TO_CALLER,
                        "/regions",
                        f"frame {region.frame.frame_id!r} is the caller's declared frame, not "
                        "one declared for the query's entities",
                        region.frame.frame_id,
                    )
                )
            keep.append(region)
        out = dataclasses.replace(query, regions=frozenset(keep))
        known_bridges = {b for e in self.pool() for b in e.frame_bridges}
        bridges = {b for b in out.frame_bridges if b in known_bridges}
        for bridge in sorted(out.frame_bridges - bridges, key=lambda b: b.transform_id):
            self.findings.append(
                _block(
                    Code.BRIDGE_NOT_DECLARED,
                    "/frame_bridges",
                    f"frame transform {bridge.transform_id} is not declared for the entities",
                    bridge.transform_id,
                )
            )
        if len(keep) != len(query.regions):
            bridges = set()  # bridges existed to relate the regions that were removed
        return dataclasses.replace(out, frame_bridges=frozenset(bridges))


def _subject_key(subject: Subject) -> tuple[str, str, int]:
    return (subject.kind, subject.declared_id or "", subject.same_as_depth)


def _failed(
    status: PlanStatus,
    text: str,
    lineage: ModelLineage,
    mentions: tuple[Mention, ...],
    code: Code,
    message: str,
    refusal: Refused | None = None,
) -> PlannedQuery:
    return PlannedQuery(
        status, text, None, lineage, mentions, (_block(code, "/", message),), refusal
    )


def _bad_input(text: str, as_of: object) -> str | None:
    if not isinstance(text, str) or not text.strip():
        return "the question is blank"
    if len(text) > MAX_QUESTION_CHARS:
        return f"the question is longer than {MAX_QUESTION_CHARS} characters"
    if any((ord(ch) < 32 and ch not in "\n\t") or ord(ch) == 127 for ch in text):
        return "the question has control characters"
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return "the question is not valid Unicode"
    if as_of != HEAD and (
        isinstance(as_of, bool) or not isinstance(as_of, int) or not 0 <= as_of <= INT64_MAX
    ):
        return "as_of is 'head' or a Ledger transaction (an integer, 0 to 2^63-1)"
    return None


def plan(
    text: str,
    as_of: AsOf,
    defaults: Defaults,
    *,
    resolver: EntityResolver,
    client: ModelClient,
    model: str = DEFAULT_MODEL,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> PlannedQuery:
    """Plan one question: ask the model once and return what it proposed, checked and stated.

    ``as_of`` is the snapshot the query is for (``"head"`` or a Ledger transaction); the planner
    never resolves ``"head"`` itself. Failure is a ``PlannedQuery`` with findings, not an exception;
    only a programming error (a resolver or client that raises something else) propagates.
    """
    lineage = ModelLineage(
        model, client.client_id, prompt.template_sha256(), SCHEMA_ID, QUERY_VERSION
    )
    problem = _bad_input(text, as_of)
    if problem is not None:
        return _failed(PlanStatus.FAILED, str(text), lineage, (), Code.BAD_INPUT, problem)
    snapshot = None if as_of == HEAD else int(as_of)
    mentions = tuple(resolver.find(text, as_of=snapshot))
    request = prompt.build_request(
        text, as_of, defaults, mentions, model=model, max_tokens=max_tokens
    )
    lineage = dataclasses.replace(lineage, request_sha256=request.sha256)
    try:
        response = client.complete(request)
    except ModelUnavailable as error:
        return _failed(
            PlanStatus.FAILED, text, lineage, mentions, Code.MODEL_UNAVAILABLE, str(error)
        )
    lineage = dataclasses.replace(
        lineage,
        model_id=response.model,
        response_sha256=None if response.text is None else response_sha256(response.text),
    )
    if response.stop == "refusal":
        return _failed(
            PlanStatus.FAILED,
            text,
            lineage,
            mentions,
            Code.MODEL_REFUSED,
            "the model declined to plan this question",
        )
    if response.stop == "max_tokens":
        return _failed(
            PlanStatus.FAILED,
            text,
            lineage,
            mentions,
            Code.MODEL_TRUNCATED,
            f"the model's output was cut off at {max_tokens} tokens",
        )
    if response.text is None or not response.text.strip():
        return _failed(
            PlanStatus.INVALID,
            text,
            lineage,
            mentions,
            Code.MODEL_OUTPUT_INVALID,
            "the model returned no query",
        )
    decoded = decode.loads(response.text)
    if not isinstance(decoded, Query):
        codes = ", ".join(sorted({str(f.code) for f in decoded.findings}))
        return _failed(
            PlanStatus.INVALID,
            text,
            lineage,
            mentions,
            Code.MODEL_OUTPUT_INVALID,
            f"the model's output is not a valid query ({codes}); it was not re-planned",
            decoded,
        )
    review = _Review(text, as_of, defaults, mentions, resolver, snapshot)
    query = review.run(decoded)
    findings = list(review.findings)
    blocking = [f for f in findings if f.severity is Severity.BLOCKING]
    result: Query | None = query
    if blocking:
        invalid = validate(query)
        if invalid:
            codes = ", ".join(sorted({str(f.code) for f in invalid}))
            findings.append(
                _block(
                    Code.DRAFT_WITHDRAWN,
                    "/",
                    f"no valid draft remains once unsupported parts are removed: {codes}",
                )
            )
            result = None
    findings.sort(key=_finding_order)
    status = _status(findings)
    return PlannedQuery(status, text, result, lineage, mentions, tuple(findings))


def _finding_order(finding: PlanFinding) -> tuple[int, str, str, str]:
    return (
        finding.severity is Severity.INFO,
        finding.at,
        str(finding.code),
        "\0".join(finding.details),
    )


def choose(planned: PlannedQuery, mention_text: str, declared_id: str) -> PlannedQuery:
    """Settle one ambiguous mention with the user's choice, without asking the model again.

    Only a ``NEEDS_CHOICE`` plan can be settled. Every subject the draft carries for a candidate of
    that mention (in ``subjects`` and in a ``Diff``) becomes ``declared_id`` (which must be one of
    the candidates), or the chosen subject is added when the draft had none; the ambiguity finding
    gives way to an info finding that the user chose, and the status is recomputed. The lineage is
    unchanged: the plan is still the model's, edited by a person.
    """
    if planned.status is not PlanStatus.NEEDS_CHOICE or planned.query is None:
        raise ValueError(f"only a needs_choice plan can be settled, not {planned.status}")
    mention = next((m for m in planned.mentions if m.text == mention_text and m.ambiguous), None)
    if mention is None:
        raise ValueError(f"{mention_text!r} is not an ambiguous mention of this plan")
    chosen = next((c for c in mention.candidates if c.declared_id == declared_id), None)
    if chosen is None:
        raise ValueError(f"{declared_id!r} is not a candidate for {mention_text!r}")
    candidates = tuple(c.declared_id for c in mention.candidates)

    def swap(subject: Subject) -> Subject:
        if subject.declared_id in candidates:
            return Subject(chosen.kind, chosen.declared_id, subject.same_as_depth)
        return subject

    query = planned.query
    explain = tuple(
        dataclasses.replace(item, subject=swap(item.subject)) if isinstance(item, Diff) else item
        for item in query.explain
    )
    subjects = frozenset(swap(s) for s in query.subjects)
    named = {s.declared_id for s in subjects} | {
        i.subject.declared_id for i in explain if isinstance(i, Diff)
    }
    if chosen.declared_id not in named:
        subjects |= {Subject(chosen.kind, chosen.declared_id)}
    query = dataclasses.replace(query, subjects=subjects, explain=explain)
    findings = [
        f
        for f in planned.findings
        if not (f.code is Code.AMBIGUOUS_ENTITY and f.details == candidates)
    ]
    findings.append(
        _info(
            Code.ENTITY_CHOSEN,
            "/subjects",
            f"{mention_text!r} was settled by the user as {declared_id}",
            declared_id,
        )
    )
    invalid = validate(query)
    result: Query | None = query
    if invalid:
        codes = ", ".join(sorted({str(f.code) for f in invalid}))
        findings.append(
            _block(Code.DRAFT_WITHDRAWN, "/", f"the chosen entity leaves no valid draft: {codes}")
        )
        result = None
    findings.sort(key=_finding_order)
    mentions = tuple(Mention(m.text, (chosen,)) if m is mention else m for m in planned.mentions)
    return dataclasses.replace(
        planned,
        status=_status(findings),
        query=result,
        mentions=mentions,
        findings=tuple(findings),
    )


def _status(findings: list[PlanFinding]) -> PlanStatus:
    blocking = [f for f in findings if f.severity is Severity.BLOCKING]
    if not blocking:
        return PlanStatus.READY
    only_ambiguity = all(f.code is Code.AMBIGUOUS_ENTITY for f in blocking)
    return PlanStatus.NEEDS_CHOICE if only_ambiguity else PlanStatus.NEEDS_INPUT
