"""Judge mode orchestration — pure logic that reports progress through event callbacks (no output).

Shared by the terminal front end (cli_render.py) and the desktop app (gui.py).
"""

from __future__ import annotations

import asyncio
import random
import uuid
from dataclasses import dataclass, field
from typing import Callable, Optional

from . import i18n, prompts
from .agent import AgentResult
from .config import AgentSpec
from .i18n import t
from .parsing import apply_judge_parse
from .pool import POOL
from .runner import run_agent

# ---------------------------------------------------------------------------
# Multi-turn chat: recent history injected as a plain-text prefix (CLIs can't take a history object)
# ---------------------------------------------------------------------------

CONTEXT_TURNS = 5             # 最多带入的历史轮数
CONTEXT_ANSWER_LIMIT = 1500   # 每条历史回答的最大字符数
CONTEXT_TOTAL_LIMIT = 8000    # 历史前缀总字符数上限（超出时丢弃最早的轮次）


def build_context_prompt(history: list[tuple[str, str]], question: str, max_turns: int = CONTEXT_TURNS) -> str:
    """history 为 [(用户问题, AI 最终答案), ...]（按时间顺序）。没有历史时原样返回问题。"""
    if not history:
        return question
    en = i18n.current() == "en"
    user, ai = ("User", "AI") if en else ("用户", "AI")
    turns = []
    for q, a in history[-max_turns:]:
        a = a if len(a) <= CONTEXT_ANSWER_LIMIT else a[:CONTEXT_ANSWER_LIMIT] + " …"
        turns.append(f"{user}: {q}\n{ai}: {a}")
    while len(turns) > 1 and sum(len(x) for x in turns) > CONTEXT_TOTAL_LIMIT:
        turns.pop(0)
    head, tail = (("Previous conversation:", "Current question:") if en else ("此前的对话：", "当前问题："))
    return f"{head}\n" + "\n\n".join(turns) + f"\n\n{tail} {question}"


# ---------------------------------------------------------------------------
# Blind, bounded answer formatting for the Judge
# ---------------------------------------------------------------------------
# Model names are never shown to the Judge (self-preference / brand bias), the order is shuffled
# (position bias), every answer is wrapped in <answer> tags and treated as untrusted data by the prompt
# (prompt injection), and each answer is length-capped.

ANSWER_LIMIT = 8000  # max characters of one answer passed to a Judge / reviewer


def answer_letter(i: int) -> str:
    """0 → A, 1 → B, … 25 → Z, 26 → AA."""
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def sanitize_untrusted(text: str, limit: int = ANSWER_LIMIT) -> str:
    """Clip, and defuse anything that looks like our own data-boundary tags."""
    if len(text) > limit:
        text = text[:limit] + "\n…(truncated)"
    for tag in ("answer", "first_review"):
        text = text.replace(f"</{tag}>", f"</{tag}_>").replace(f"<{tag}", f"<{tag}_")
    return text


def answer_block(label: str, text: str) -> str:
    return f'<answer id="{label}">\n{sanitize_untrusted(text)}\n</answer>'


def format_answers_blind(results: list[AgentResult], rng: Optional[random.Random] = None) -> tuple[str, dict[str, str]]:
    """Anonymize and shuffle answers. Returns (prompt text, {letter: agent name}) — the mapping stays local."""
    order = list(results)
    (rng or random).shuffle(order)
    labels, parts = {}, []
    for i, r in enumerate(order):
        letter = answer_letter(i)
        labels[letter] = r.name
        parts.append(answer_block(letter, r.output))
    return "\n\n".join(parts), labels


def apply_verdict_meta(result: AgentResult, n_answers: int, labels: dict[str, str]) -> None:
    """Parse the verdict, then enforce quorum: with fewer than two answers there is nothing to agree on."""
    apply_judge_parse(result)
    quorum = n_answers >= 2
    result.extra["quorum"] = quorum
    result.extra["labels"] = labels
    if not quorum:
        result.extra["consensus_score"] = None


def needs_second_opinion(result: AgentResult) -> bool:
    """Low self-reported confidence, or measured disagreement (score 0) — self-reported confidence is poorly
    calibrated, so a total lack of consensus triggers a second opinion even when the Judge sounds sure."""
    return result.extra.get("confidence") == "低" or (
        result.extra.get("quorum") and result.extra.get("consensus_score") == 0.0)


def select_reviewer(pool: list[AgentSpec], leader: AgentSpec) -> Optional[AgentSpec]:
    """从 Leader 池中挑选复审模型：排除当前 Leader 与未安装的，
    优先不同厂商，其次 pro 档，最后按配置顺序。没有可选时返回 None。"""
    candidates = [s for s in pool if s.name != leader.name and s.installed]
    if not candidates:
        return None
    order = {s.name: i for i, s in enumerate(candidates)}
    return min(candidates, key=lambda s: (s.vendor == leader.vendor, s.tier != "pro", order[s.name]))


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------
# 编排：纯逻辑 + 事件回调
#
# 事件（kind, payload）：
#   worker_start      {index, spec}
#   worker_done       {index, spec, result}
#   workers_finished  {results, ok}
#   leader_start      {spec, n_answers}
#   leader_done       {spec, result}               result.extra["confidence"] 为 高/中/低/None
#   review_start      {spec, leader, fallback}     首轮确信度低或共识度为 0 → 异构模型复审；fallback=True 表示池中无其他可用模型
#   leader_done / review_done 的 result.extra 还包含：consensus_score（不足两个回答时为 None）、quorum、labels（字母 → 模型名）
#   review_done       {spec, result}

EventHandler = Callable[[str, dict], None]

EXIT_OK, EXIT_NO_WORKERS, EXIT_LEADER_FAILED = 0, 1, 2


@dataclass
class RunOutcome:
    results: list[AgentResult]               # Judge：各 Worker 的回答；Co-work：各 Worker 最后一次参与的回答
    verdict: Optional[AgentResult]           # 最终裁决（复审成功时为复审结果）
    code: int
    first_verdict: Optional[AgentResult] = None
    review: Optional[AgentResult] = None     # 复审结果（可能失败）
    mode: str = "judge"
    rounds: list[list[AgentResult]] = field(default_factory=list)   # Co-work：每一轮的全部回答
    leader_rounds: list[AgentResult] = field(default_factory=list)  # Co-work：每次 Leader 总结（含延长讨论）
    guidance: list[str] = field(default_factory=list)               # Co-work：Leader 下发的争议指导意见

    @property
    def ok_results(self) -> list[AgentResult]:
        return [r for r in self.results if r.ok]


async def orchestrate(question: str, workers: list[AgentSpec], leader: AgentSpec,
                      review_on_low: bool = True, on_event: Optional[EventHandler] = None,
                      reviewer_pool: Optional[list[AgentSpec]] = None) -> RunOutcome:
    """reviewer_pool：二次复审的候选模型池（通常是 config.leaders），由 select_reviewer 从中挑选。"""
    session = uuid.uuid4().hex
    try:
        return await _orchestrate(question, workers, leader, review_on_low, on_event, reviewer_pool, session)
    finally:
        POOL.end_session(session)


async def _orchestrate(question: str, workers: list[AgentSpec], leader: AgentSpec, review_on_low: bool,
                       on_event: Optional[EventHandler], reviewer_pool: Optional[list[AgentSpec]],
                       session: str) -> RunOutcome:
    emit: EventHandler = on_event or (lambda kind, payload: None)

    async def tracked(i: int, spec: AgentSpec) -> AgentResult:
        emit("worker_start", {"index": i, "spec": spec})
        try:
            r = await run_agent(spec, question, session)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # 兜底：任何未预料的异常都不影响其他 Worker
            r = AgentResult(spec.name, ok=False, error=t("eng.unexpected", e=repr(e)))
        emit("worker_done", {"index": i, "spec": spec, "result": r})
        return r

    results = list(await asyncio.gather(*(tracked(i, w) for i, w in enumerate(workers))))
    ok = [r for r in results if r.ok]
    emit("workers_finished", {"results": results, "ok": ok})
    if not ok:
        return RunOutcome(results, None, EXIT_NO_WORKERS)

    answers, labels = format_answers_blind(ok)
    judge_template, review_template = prompts.get("judge"), prompts.get("review")
    emit("leader_start", {"spec": leader, "n_answers": len(ok)})
    first = await run_agent(leader, judge_template.format(n=len(ok), question=question, answers=answers), session)
    if first.ok:
        apply_verdict_meta(first, len(ok), labels)
    emit("leader_done", {"spec": leader, "result": first})
    if not first.ok:
        return RunOutcome(results, first, EXIT_LEADER_FAILED, first_verdict=first)

    if not (review_on_low and needs_second_opinion(first)):
        return RunOutcome(results, first, EXIT_OK, first_verdict=first)

    # —— 动态复审：换一个异构模型 ——
    reviewer = select_reviewer(reviewer_pool or [], leader)
    fallback = reviewer is None
    reviewer = reviewer or leader
    emit("review_start", {"spec": reviewer, "leader": leader, "fallback": fallback})
    review = await run_agent(reviewer, review_template.format(
        question=question, answers=answers, first_verdict=sanitize_untrusted(first.output)), session)
    if review.ok:
        apply_verdict_meta(review, len(ok), labels)
        review.extra.update(reviewed=True, first_confidence=first.extra["confidence"], first_leader=leader.name)
    emit("review_done", {"spec": reviewer, "result": review})
    # 复审失败时保留首轮裁决
    return RunOutcome(results, review if review.ok else first, EXIT_OK, first_verdict=first, review=review)
