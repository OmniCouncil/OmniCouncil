#!/usr/bin/env python3
"""Accuracy / latency / cost evaluation: single models vs. majority vote vs. Judge vs. Co-work.

Uses MMLU-Pro (TIGER-Lab/MMLU-Pro, MIT license): 10-option multiple choice, graded by exact letter match,
so no LLM grader (and no extra quota) is needed for grading.

One Judge run per question yields, for free:
  * every worker's own answer       → "single:<name>" baselines
  * a plain majority vote of those  → "majority" baseline (does the Judge beat a free vote?)
  * the Judge's verdict             → "judge"
Co-work runs separately (optionally on a subset, since it costs ~3× the calls).

    python evals/run_eval.py --n 20 --cowork 10 --concurrency 2
    python evals/run_eval.py --summarize evals/results/<run>/results.jsonl

Every question costs real subscription quota (Judge ≈ workers + 1–2 calls; Co-work ≈ workers × rounds + 1–2).
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import json
import random
import re
import statistics
import sys
import time
import urllib.request
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from omnicouncil import i18n  # noqa: E402
from omnicouncil.config import load_config  # noqa: E402
from omnicouncil.cowork import run_cowork_loop  # noqa: E402
from omnicouncil.orchestrate import orchestrate  # noqa: E402
from omnicouncil.pool import POOL  # noqa: E402

HERE = Path(__file__).resolve().parent
CACHE = HERE / ".cache"
ROWS_API = "https://datasets-server.huggingface.co/rows?dataset=TIGER-Lab/MMLU-Pro&config=default&split=test&offset={o}&length=1"
TOTAL_ROWS = 12032
LETTERS = "ABCDEFGHIJ"

INSTRUCTION = ("Answer the multiple-choice question. Think it through briefly, then finish with a final line "
               "in exactly this form: Answer: <letter>")


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------


def load_questions(n: int, seed: int) -> list[dict]:
    """A reproducible random sample of MMLU-Pro test questions (cached locally)."""
    CACHE.mkdir(exist_ok=True)
    offsets = random.Random(seed).sample(range(TOTAL_ROWS), n)
    out = []
    for o in offsets:
        f = CACHE / f"mmlu_pro_{o}.json"
        if not f.exists():
            with urllib.request.urlopen(ROWS_API.format(o=o), timeout=30) as resp:
                f.write_text(json.dumps(json.load(resp)["rows"][0]["row"]), encoding="utf-8")
        out.append(json.loads(f.read_text(encoding="utf-8")))
    return out


def format_question(row: dict) -> str:
    options = "\n".join(f"{LETTERS[i]}. {opt}" for i, opt in enumerate(row["options"]))
    return f"{row['question']}\n\n{options}\n\n{INSTRUCTION}"


# ---------------------------------------------------------------------------
# Grading
# ---------------------------------------------------------------------------

_ANSWER_LINE = re.compile(r"answer\s*(?:is|:|=)?\s*[*_`(\[]*\s*([A-J])(?![A-Za-z])", re.IGNORECASE)
_LONE_LETTER = re.compile(r"^\s*[*_`(\[]*([A-J])[)\].:*_`]*\s*$", re.MULTILINE)


def extract_choice(text: str, n_options: int) -> Optional[str]:
    """The last explicit 'Answer: X'; otherwise the last line that is just a letter. None if neither."""
    valid = LETTERS[:n_options]
    hits = [m.upper() for m in _ANSWER_LINE.findall(text or "") if m.upper() in valid]
    if hits:
        return hits[-1]
    lone = [m.upper() for m in _LONE_LETTER.findall(text or "") if m.upper() in valid]
    return lone[-1] if lone else None


def majority(choices: list[Optional[str]]) -> Optional[str]:
    """Strict plurality of the parsed single-model answers; None on a tie or with no answers."""
    counts = collections.Counter(c for c in choices if c)
    if not counts:
        return None
    (top, n), *rest = counts.most_common()
    return None if rest and rest[0][1] == n else top


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------


class CallCounter:
    """Counts model calls from orchestration events."""

    CALL_EVENTS = {"worker_done", "leader_done", "review_done"}

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, kind: str, payload: dict) -> None:
        if kind in self.CALL_EVENTS:
            self.calls += 1


async def run_one(row: dict, cfg, do_cowork: bool, sem: asyncio.Semaphore) -> dict:
    q = format_question(row)
    n_opt = len(row["options"])
    rec = {"question_id": row["question_id"], "category": row["category"], "gold": row["answer"], "systems": {}}
    async with sem:
        counter = CallCounter()
        t0 = time.monotonic()
        o = await orchestrate(q, cfg.enabled_workers, cfg.leader, cfg.review_on_low_confidence, counter,
                              reviewer_pool=cfg.leaders)
        judge_time = time.monotonic() - t0
        singles = {}
        for r in o.results:
            singles[r.name] = extract_choice(r.output, n_opt) if r.ok else None
            rec["systems"][f"single:{r.name}"] = {"choice": singles[r.name], "ok": r.ok, "seconds": round(r.elapsed, 1),
                                                   "calls": 1}
        rec["systems"]["majority"] = {"choice": majority(list(singles.values())), "ok": True,
                                      "seconds": round(max((r.elapsed for r in o.results), default=0), 1),
                                      "calls": len(o.results)}
        v = o.verdict
        rec["systems"]["judge"] = {
            "choice": extract_choice((v.extra.get("final_answer") or v.output) if v and v.ok else "", n_opt),
            "ok": bool(v and v.ok), "seconds": round(judge_time, 1), "calls": counter.calls,
            "consensus": v.extra.get("consensus_score") if v else None,
            "confidence": v.extra.get("confidence") if v else None,
            "reviewed": bool(v and v.extra.get("reviewed")),
        }
        if do_cowork:
            counter = CallCounter()
            t0 = time.monotonic()
            c = await run_cowork_loop(q, cfg.enabled_workers, cfg.leader, counter, rounds=cfg.cowork_rounds,
                                      max_rounds=cfg.cowork_max_rounds, extend_on_low=cfg.cowork_extend)
            cv = c.verdict
            rec["systems"]["cowork"] = {
                "choice": extract_choice((cv.extra.get("final_answer") or cv.output) if cv and cv.ok else "", n_opt),
                "ok": bool(cv and cv.ok), "seconds": round(time.monotonic() - t0, 1), "calls": counter.calls,
                "rounds": len(c.rounds),
            }
    for s in rec["systems"].values():
        s["correct"] = s["choice"] == rec["gold"]
    return rec


async def run(args) -> Path:
    i18n.set_language("en")
    cfg = load_config(Path(args.config)) if args.config else load_config()
    rows = load_questions(args.n, args.seed)
    out_dir = HERE / "results" / time.strftime("%Y%m%d-%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "results.jsonl"
    meta = {"dataset": "TIGER-Lab/MMLU-Pro (test)", "n": args.n, "seed": args.seed, "cowork_n": args.cowork,
            "workers": [f"{w.name}:{w.selected_model or 'default'}" for w in cfg.enabled_workers],
            "leader": cfg.leader.name, "review_on_low_confidence": cfg.review_on_low_confidence,
            "cowork_rounds": cfg.cowork_rounds, "started": time.strftime("%Y-%m-%d %H:%M")}
    (out_dir / "meta.json").write_text(json.dumps(meta, indent=2))
    await POOL.sync(cfg.enabled_workers + [cfg.leader])
    sem = asyncio.Semaphore(args.concurrency)
    done = 0

    async def task(i: int, row: dict) -> None:
        nonlocal done
        rec = await run_one(row, cfg, i < args.cowork, sem)
        with out.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
        done += 1
        j = rec["systems"]["judge"]
        print(f"[{done}/{len(rows)}] {rec['question_id']} gold={rec['gold']} judge={j['choice']} "
              f"({'✓' if j['correct'] else '✗'}, {j['seconds']}s)", flush=True)

    try:
        await asyncio.gather(*(task(i, r) for i, r in enumerate(rows)))
    finally:
        await POOL.shutdown()
    return out


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson score interval for a proportion."""
    if n == 0:
        return 0.0, 0.0
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / d
    return c - h, c + h


def summarize(path: Path) -> str:
    recs = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    meta_f = path.parent / "meta.json"
    meta = json.loads(meta_f.read_text()) if meta_f.exists() else {}
    systems = sorted({s for r in recs for s in r["systems"]}, key=lambda s: (not s.startswith("single"), s))
    lines = [f"# Evaluation — {meta.get('dataset', 'MMLU-Pro')}", ""]
    if meta:
        lines += [f"- Questions: {len(recs)} (seed {meta.get('seed')}); Co-work on the first {meta.get('cowork_n')}",
                  f"- Workers: {', '.join(meta.get('workers', []))} · Leader: {meta.get('leader')}"
                  f" · second review: {meta.get('review_on_low_confidence')} · Co-work rounds: {meta.get('cowork_rounds')}",
                  f"- Run started: {meta.get('started')}", ""]
    lines += ["| System | Accuracy | 95% CI | n | Unparsed / failed | Median time / question | Model calls / question |",
              "|---|---|---|---|---|---|---|"]
    for s in systems:
        rows = [r["systems"][s] for r in recs if s in r["systems"]]
        n = len(rows)
        k = sum(x["correct"] for x in rows)
        lo, hi = wilson(k, n)
        bad = sum(1 for x in rows if not x["ok"] or x["choice"] is None)
        med = statistics.median(x["seconds"] for x in rows) if rows else 0
        calls = statistics.mean(x["calls"] for x in rows) if rows else 0
        lines.append(f"| {s} | **{k}/{n} = {k / n:.0%}** | {lo:.0%}–{hi:.0%} | {n} | {bad} | {med:.1f}s | {calls:.1f} |")
    # Where the Judge helped or hurt relative to the single models
    rescued = [r for r in recs if r["systems"]["judge"]["correct"] and r["systems"]["majority"]["choice"] != r["gold"]]
    broke = [r for r in recs if not r["systems"]["judge"]["correct"] and r["systems"]["majority"]["choice"] == r["gold"]]
    reviewed = [r for r in recs if r["systems"]["judge"].get("reviewed")]
    lines += ["", f"- Judge right where the majority vote was wrong or tied: **{len(rescued)}**",
              f"- Judge wrong where the majority vote was right: **{len(broke)}**",
              f"- Second reviews triggered: {len(reviewed)}"]
    if "cowork" in systems:
        sub = [r for r in recs if "cowork" in r["systems"]]
        jk = sum(r["systems"]["judge"]["correct"] for r in sub)
        ck = sum(r["systems"]["cowork"]["correct"] for r in sub)
        lines.append(f"- On the {len(sub)} questions run in both modes: Judge {jk}/{len(sub)}, Co-work {ck}/{len(sub)}")
    lines += ["", "*Small samples give wide confidence intervals; treat differences inside the CI as noise.*"]
    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--n", type=int, default=20, help="number of questions (default 20)")
    ap.add_argument("--cowork", type=int, default=0, help="also run Co-work on the first N questions")
    ap.add_argument("--seed", type=int, default=7, help="sampling seed (default 7)")
    ap.add_argument("--concurrency", type=int, default=2, help="questions in flight at once (default 2)")
    ap.add_argument("--config", help="config.json to use (default: the app's config)")
    ap.add_argument("--summarize", type=Path, help="only summarize an existing results.jsonl")
    args = ap.parse_args()
    path = args.summarize or asyncio.run(run(args))
    summary = summarize(path)
    (path.parent / "summary.md").write_text(summary, encoding="utf-8")
    print("\n" + summary)
    print(f"Saved: {path.parent}")


if __name__ == "__main__":
    main()
