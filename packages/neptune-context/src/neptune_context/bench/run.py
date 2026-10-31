"""Run a question set through the arms, judge each answer, write one JSON line per answer."""

from __future__ import annotations

import json
import statistics
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from neptune_context.bench import agent, arms, cites, judge, readers, sites

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


def make_arm(name: str, built: Path, raw: Path, api: Any, settings: Settings) -> arms.Arm:
    if name == "files":
        return arms.Files(raw)
    if name == "neptune":
        return arms.Neptune(built)
    if name == "context":
        return arms.Context(
            raw, chars_per_token(api, settings.model, raw), budget=settings.context_budget
        )
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

PRICES = {"claude-opus-5-5": (4.0, 20.0, 8.0, 0.20)}  # $/MTok: input, output, cache write 1h, read


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
        facts = [j for r in mine for j in r["judged"]]
        unsure = [j for j in facts if j["state"] in judge.UNSURE]
        costs = [c for c in (cost(r) for r in mine) if c is not None]
        out[name] = {
            "questions": len(mine),
            "correct": sum(all(j["verdict"] == "stated" for j in r["judged"]) for r in mine),
            "facts": len(facts),
            "facts_stated": sum(j["verdict"] == "stated" for j in facts),
            "unsure_facts": len(unsure),
            "made_up": sum(j["made_up"] for j in unsure),
            "citations": sum(r["cites"]["listed"] - r["cites"]["unverifiable"] for r in mine),
            "citations_verified": sum(r["cites"]["verified"] for r in mine),
            "median_tokens": statistics.median(tokens(r["usage"]) for r in mine),
            "median_tool_calls": statistics.median(r["tool_calls"] for r in mine),
            "cost_usd": round(sum(costs), 2) if costs else None,
        }
    return out


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
