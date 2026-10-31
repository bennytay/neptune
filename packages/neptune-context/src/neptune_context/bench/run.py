"""Run a question set through the arms, judge each answer, write one JSON line per answer."""

from __future__ import annotations

import json
import statistics
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from neptune_context.bench import agent, arms, cites, judge, readers, sites
from neptune_context.bench.questions import UNANSWERABLE

if TYPE_CHECKING:
    from pathlib import Path

    from neptune_context.bench.questions import Question

ARMS = ("files", "context", "neptune")
SAMPLE_CHARS = 200_000


@dataclass(frozen=True)
class Settings:
    model: str = "claude-opus-5-5"
    effort: str = "medium"
    judge_model: str = "claude-opus-5-5"
    max_tool_calls: int = 30
    workers: int = 4
    context_budget: int = 900_000


def chars_per_token(api: Any, model: str, raw: Path) -> float:
    """Characters per token on a sample of the site's own text."""
    text = "".join(readers.text_of(raw / rel) for rel in sites.files(raw))[:SAMPLE_CHARS]
    count = api.messages.count_tokens(model=model, messages=[{"role": "user", "content": text}])
    return len(text) / max(int(count.input_tokens), 1)


def fitted_context(api: Any, raw: Path, settings: Settings) -> arms.Context:
    """A context arm whose prompt, counted by the API, is within the budget: the estimate from a
    sample is only a start, since feed lines take more tokens per character than documents."""
    ratio = chars_per_token(api, settings.model, raw)
    for _ in range(6):
        arm = arms.Context(raw, ratio, budget=settings.context_budget)
        count = api.messages.count_tokens(
            model=settings.model,
            system=arm.system(),
            messages=[{"role": "user", "content": arm.prompt("?")}],
        )
        tokens = int(count.input_tokens)
        arm.notes["prompt_tokens"] = tokens
        if tokens <= settings.context_budget or not arm.notes.get("feed_lines_kept"):
            return arm
        ratio *= settings.context_budget / tokens * 0.98
    return arm


def make_arm(name: str, built: Path, raw: Path, api: Any, settings: Settings) -> arms.Arm:
    if name == "files":
        return arms.Files(raw)
    if name == "neptune":
        return arms.Neptune(built)
    if name == "context":
        return fitted_context(api, raw, settings)
    raise ValueError(f"unknown arm {name!r}; the arms are {', '.join(ARMS)}")


def answer(
    api: Any, arm: arms.Arm, question: Question, built: Path, settings: Settings
) -> dict[str, Any]:
    transcript = agent.ask(
        api,
        arm,
        question.text,
        model=settings.model,
        effort=settings.effort,
        max_tool_calls=settings.max_tool_calls,
    )
    return {
        "qid": question.id,
        "site": question.site,
        "arm": arm.name,
        "question": question.text,
        "model": transcript.model,
        "effort": settings.effort,
        "judge_model": settings.judge_model,
        "answer": transcript.answer,
        "stop": transcript.stop,
        "tool_calls": transcript.tool_calls,
        "seconds": transcript.seconds,
        "usage": transcript.usage,
        "arm_notes": arm.notes,
        "judged": judge.judge(api, question, transcript.answer, model=settings.judge_model),
        "cites": cites.check(transcript.answer, built),
        "transcript": agent.strip_images(transcript.messages),
    }


def missing(question: Question, why: str) -> list[dict[str, Any]]:
    """Every expected fact graded missing, without a judge (no answer to grade)."""
    return [
        {
            "fact": judge.describe(item),
            "predicate": item["predicate"],
            "state": item.get("state", "known"),
            "verdict": "missing",
            "made_up": False,
            "why": why,
        }
        for item in judge.expected(question)
    ]


def did_not_fit(
    arm: arms.Arm, question: Question, settings: Settings, error: str
) -> dict[str, Any]:
    """A context arm whose site is bigger than the window: no answer, every fact missing."""
    return {
        "qid": question.id,
        "site": question.site,
        "arm": arm.name,
        "question": question.text,
        "model": settings.model,
        "effort": settings.effort,
        "judge_model": settings.judge_model,
        "answer": "",
        "stop": "prompt_too_long",
        "tool_calls": 0,
        "seconds": 0.0,
        "usage": dict.fromkeys(agent.USAGE, 0),
        "arm_notes": {**arm.notes, "error": error[:300]},
        "judged": missing(question, "the site does not fit in the context window"),
        "cites": {"listed": 0, "verified": 0, "unverifiable": 0, "details": []},
        "transcript": [],
    }


def _graded(record: dict[str, Any]) -> list[dict[str, Any]]:
    """The judged facts, less those no reader of the raw files can get (``UNANSWERABLE``)."""
    out = []
    for j in record["judged"]:
        predicate = j.get("predicate") or j["fact"].split(" · ")[1].split(" ")[0].rstrip(".")
        if predicate not in UNANSWERABLE:
            out.append(j)
    return out


def run(
    api: Any,
    questions: list[Question],
    arm_names: tuple[str, ...],
    root: Path,
    out: Path,
    settings: Settings,
    log: Any = print,
) -> None:
    """Every (question, arm) pair, ``settings.workers`` at a time. A context arm answers its
    first question alone, so the rest read its prompt from the cache."""
    out.parent.mkdir(parents=True, exist_ok=True)
    built_arms: dict[tuple[str, str], tuple[arms.Arm, Path]] = {}
    for site in sorted({q.site for q in questions}):
        built, raw = sites.prepare(site, root)
        for name in arm_names:
            built_arms[(site, name)] = (make_arm(name, built, raw, api, settings), built)
    jobs = [(q, name) for q in questions for name in arm_names]
    first: dict[str, tuple[Question, str]] = {}
    for q, name in jobs:
        if name == "context":
            first.setdefault(q.site, (q, name))
    lock = threading.Lock()

    def one(job: tuple[Question, str]) -> None:
        question, name = job
        arm, built = built_arms[(question.site, name)]
        try:
            record = answer(api, arm, question, built, settings)
        except Exception as error:
            if "prompt is too long" in str(error):
                record = did_not_fit(arm, question, settings, str(error))
                with lock, out.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(record) + "\n")
                log(f"{question.id} {name}: the site does not fit in the context window")
                return
            log(f"{question.id} {name}: FAILED {type(error).__name__}: {error}")
            return
        with lock, out.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
        ok = all(j["verdict"] == "stated" for j in record["judged"])
        log(f"{question.id} {name}: {'correct' if ok else 'wrong'} ({record['seconds']} s)")

    with ThreadPoolExecutor(settings.workers) as pool:
        list(pool.map(one, first.values()))
        list(pool.map(one, [j for j in jobs if j not in first.values()]))


# --- summary ---------------------------------------------------------------------------------

# $/MTok: input, output, cache write, cache read. Writes are priced at the 5-minute rate: the
# growing conversation is cached for 5 minutes; only the small system prompt takes the 1-hour rate.
PRICES = {
    "claude-opus-5-5": (4.0, 20.0, 5.0, 0.20),
    "claude-sonnet-5-5": (2.0, 10.0, 2.5, 0.20),
    "claude-haiku-5-5": (0.10, 0.50, 0.125, 0.01),
}


def tokens(usage: dict[str, int]) -> int:
    return sum(usage.values())


def cost(record: dict[str, Any]) -> float | None:
    price = PRICES.get(record["model"])
    if price is None:
        return None
    u = record["usage"]
    return (
        float(
            u["input_tokens"] * price[0]
            + u["output_tokens"] * price[1]
            + u["cache_creation_input_tokens"] * price[2]
            + u["cache_read_input_tokens"] * price[3]
        )
        / 1e6
    )


def rejudge(api: Any, records: list[dict[str, Any]], qs: list[Question], model: str) -> int:
    """Grade recorded answers again where their question's expected facts have changed (a fixed
    answer key); the answers themselves are kept. Returns how many were graded again."""
    by_id = {q.id: q for q in qs}
    done = 0
    for r in records:
        q = by_id.get(r["qid"])
        if q is None or [judge.describe(e) for e in judge.expected(q)] == [
            j["fact"] for j in r["judged"]
        ]:
            continue
        if r["stop"] == "prompt_too_long":
            r["judged"] = missing(q, "the site does not fit in the context window")
        else:
            r["judged"] = judge.judge(api, q, r["answer"], model=model)
        done += 1
    return done


def load(paths: list[Path]) -> list[dict[str, Any]]:
    """Records of one or more results files; a later record of the same (question, arm) wins."""
    records: dict[tuple[str, str], dict[str, Any]] = {}
    for path in paths:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                records[(r["qid"], r["arm"])] = r
    return list(records.values())


def recheck(records: list[dict[str, Any]], root: Path) -> None:
    """Check every answer's citations again against the sites on disk (no model)."""
    for r in records:
        site = root / r["site"]
        if site.exists():
            r["cites"] = cites.check(r["answer"], site)


def summarise(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Per arm: the counts each chart needs."""
    out: dict[str, dict[str, Any]] = {}
    for name in [a for a in ARMS if any(r["arm"] == a for r in records)]:
        mine = [r for r in records if r["arm"] == name]
        fit = [r for r in mine if r["stop"] != "prompt_too_long"]
        facts = [j for r in mine for j in _graded(r)]
        unsure = [j for j in facts if j["state"] in judge.UNSURE]
        costs = [c for c in (cost(r) for r in mine) if c is not None]
        out[name] = {
            "questions": len(mine),
            "correct": sum(all(j["verdict"] == "stated" for j in _graded(r)) for r in mine),
            "did_not_fit": sum(r["stop"] == "prompt_too_long" for r in mine),
            "facts": len(facts),
            "facts_stated": sum(j["verdict"] == "stated" for j in facts),
            "unsure_facts": len(unsure),
            "made_up": sum(j["made_up"] for j in unsure),
            "citations": sum(r["cites"]["listed"] - r["cites"]["unverifiable"] for r in mine),
            "citations_verified": sum(r["cites"]["verified"] for r in mine),
            "median_tokens": _median([tokens(r["usage"]) for r in fit]),
            "median_tool_calls": _median([r["tool_calls"] for r in fit]),
            "cost_usd": round(sum(costs), 2) if costs else None,
        }
    return out


def _median(values: list[int]) -> float:
    return float(statistics.median(values)) if values else 0.0


def table(summary: dict[str, dict[str, Any]]) -> str:
    def frac(a: int, b: int) -> str:
        return f"{a}/{b} ({100 * a / b:.0f}%)" if b else "-"

    rows = [("arm", "correct", "facts stated", "made up", "citations ok", "med tokens", "cost")]
    for name, s in summary.items():
        rows.append(
            (
                name,
                frac(s["correct"], s["questions"]),
                frac(s["facts_stated"], s["facts"]),
                frac(s["made_up"], s["unsure_facts"]),
                frac(s["citations_verified"], s["citations"]),
                f"{s['median_tokens']:,.0f}",
                f"${s['cost_usd']}" if s["cost_usd"] is not None else "-",
            )
        )
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    return "\n".join("  ".join(c.ljust(w) for c, w in zip(r, widths, strict=True)) for r in rows)
