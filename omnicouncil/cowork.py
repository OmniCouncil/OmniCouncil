"""Co-work mode: multi-round round-table discussion, then a Leader summary (extended when confidence is Low).

Co-work 模式：多轮圆桌讨论 + Leader 总结（确信度低时延长讨论）

流程：Round 1 各 Worker 独立作答 → Round 2..N 每个 Worker 看到其他人上一轮的答案后修正 / 补充 / 坚持
      → Leader 基于最后一轮答案总结；若确信度为「低」且未达 max_rounds，则提取「争议指导意见」作为下一轮
      的讨论重点，再次讨论后重新总结，直到不再是「低」或达到 max_rounds。

并发与超时：同一轮内 Workers 并发（asyncio.gather），轮与轮之间串行；每次调用都有独立超时（run_agent），
不存在共享锁，因此不会死锁。某个 Worker 在某轮失败后不再参与后续轮次（避免反复超时拖慢整轮）。

事件（在 Judge 模式事件之外新增 / 扩展）：
  round_start   {round, total, phase, status}       phase: independent / peer_review / guided
  worker_start  {index, spec, round, total, phase}
  worker_done   {index, spec, result, round}
  round_done    {round, results, active}
  leader_start  {spec, n_answers, round}
  leader_done   {spec, result, round, will_extend}
  extension     {round, guidance, max_rounds}       round 为即将开始的新一轮
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Optional

from . import i18n, prompts
from .agent import AgentResult
from .config import AgentSpec
from .i18n import t
from .orchestrate import EXIT_LEADER_FAILED, EXIT_NO_WORKERS, EXIT_OK, EventHandler, RunOutcome, format_answers
from .parsing import apply_judge_parse, extract_guidance
from .pool import POOL
from .runner import run_agent

PEER_ANSWER_LIMIT = 6000  # 传给同伴的单个回答最多字符数，避免 prompt 无限膨胀

def _clip(text: str, limit: int = PEER_ANSWER_LIMIT) -> str:
    return text if len(text) <= limit else text[:limit] + "\n…(truncated)"


def build_peer_prompt(question: str, own: AgentResult, peers: list[AgentResult], round_: int, total: int,
                      phase: str, guidance: Optional[str]) -> str:
    en = i18n.current() == "en"
    peer_fmt = "### {name}'s view\n{out}" if en else "### {name} 的看法\n{out}"
    peers_text = "\n\n".join(peer_fmt.format(name=p.name, out=_clip(p.output)) for p in peers) or "—"
    guidance_text = ""
    if guidance:
        guidance_text = (f"\n## The Leader's dispute guidance\n{guidance}\n" if en
                         else f"\n## Leader 的争议指导意见\n{guidance}\n")
    task_key = "guided" if phase == "guided" else ("final" if round_ == total else "peer_review")
    template = prompts.get("cowork_peer")
    return template.format(round=round_, total=total, question=question, own=_clip(own.output),
                           peers=peers_text, guidance=guidance_text, task=prompts.get(f"cowork_task_{task_key}"))


async def run_cowork_loop(question: str, workers: list[AgentSpec], leader: AgentSpec,
                          on_event: Optional[EventHandler] = None, rounds: int = 3, max_rounds: int = 4,
                          extend_on_low: bool = True) -> RunOutcome:
    session = uuid.uuid4().hex
    try:
        return await _cowork_loop(question, workers, leader, on_event, rounds, max_rounds, extend_on_low, session)
    finally:
        POOL.end_session(session)


async def _cowork_loop(question: str, workers: list[AgentSpec], leader: AgentSpec, on_event: Optional[EventHandler],
                       rounds: int, max_rounds: int, extend_on_low: bool, session: str) -> RunOutcome:
    emit: EventHandler = on_event or (lambda kind, payload: None)
    rounds = max(1, rounds)
    max_rounds = max(rounds, max_rounds)
    en = i18n.current() == "en"

    history: list[dict[int, AgentResult]] = []   # 每一轮：worker 下标 → 结果
    latest: dict[int, AgentResult] = {}         # 每个 worker 最后一次参与的结果
    leader_verdicts: list[AgentResult] = []
    guidance_list: list[str] = []
    active = list(range(len(workers)))
    planned = rounds
    guidance: Optional[str] = None
    verdict: Optional[AgentResult] = None

    def outcome(code: int) -> RunOutcome:
        return RunOutcome(
            results=[latest[i] for i in sorted(latest)], verdict=verdict, code=code,
            first_verdict=leader_verdicts[0] if leader_verdicts else None, mode="cowork",
            rounds=[[r[i] for i in sorted(r)] for r in history], leader_rounds=leader_verdicts,
            guidance=guidance_list)

    round_ = 0
    while True:
        round_ += 1
        phase = "independent" if round_ == 1 else ("guided" if guidance else "peer_review")
        emit("round_start", {"round": round_, "total": planned, "phase": phase,
                             "status": f"Round {round_}/{planned} generating..."})
        prev = history[-1] if history else {}

        async def one(i: int) -> tuple[int, AgentResult]:
            spec = workers[i]
            emit("worker_start", {"index": i, "spec": spec, "round": round_, "total": planned, "phase": phase})
            if round_ == 1:
                prompt = question
            else:
                peers = [prev[j] for j in sorted(prev) if j != i and prev[j].ok]
                prompt = build_peer_prompt(question, prev[i], peers, round_, planned, phase, guidance)
            try:
                r = await run_agent(spec, prompt, session)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # 兜底：任何未预料的异常都不影响其他 Worker
                r = AgentResult(spec.name, ok=False, error=t("eng.unexpected", e=repr(e)))
            r.extra["round"] = round_
            emit("worker_done", {"index": i, "spec": spec, "result": r, "round": round_})
            return i, r

        current = dict(await asyncio.gather(*(one(i) for i in active)))
        history.append(current)
        latest.update(current)
        active = [i for i in active if current[i].ok]
        emit("round_done", {"round": round_, "results": [current[i] for i in sorted(current)], "active": len(active)})

        if not active:
            emit("workers_finished", {"results": list(latest.values()), "ok": []})
            return outcome(EXIT_NO_WORKERS)
        # 至少两位参与者才有讨论的意义；否则直接进入总结
        if round_ < planned and len(active) >= 2:
            continue

        final_answers = [current[i] for i in active]
        if not leader_verdicts:
            emit("workers_finished", {"results": list(latest.values()), "ok": final_answers})
        guidance_note = ""
        if guidance:
            guidance_note = (f"\nIn the previous summary you issued this dispute guidance, which the assistants have since discussed:\n{guidance}\n"
                             if en else f"\n你在上一次总结中下发过以下争议指导意见，各助手已据此进行了讨论：\n{guidance}\n")
        template = prompts.get("cowork_leader")
        emit("leader_start", {"spec": leader, "n_answers": len(final_answers), "round": round_})
        v = await run_agent(leader, template.format(n=len(final_answers), rounds=round_, guidance=guidance_note,
                                                    question=question, answers=format_answers(final_answers)), session)
        will_extend = False
        v.extra.update(round=round_, mode="cowork")
        if v.ok:
            apply_judge_parse(v)  # Co-work 总结是 Markdown 格式：回退解析取「最终定论」一节作为正文
            will_extend = (extend_on_low and v.extra["confidence"] == "低" and round_ < max_rounds
                           and len(active) >= 2)
        emit("leader_done", {"spec": leader, "result": v, "round": round_, "will_extend": will_extend})
        leader_verdicts.append(v)
        if not v.ok:
            if verdict:  # 延长讨论后的总结失败：保留上一次成功的总结
                return outcome(EXIT_OK)
            verdict = v
            return outcome(EXIT_LEADER_FAILED)
        verdict = v
        if not will_extend:
            return outcome(EXIT_OK)

        guidance = extract_guidance(v.output)
        guidance_list.append(guidance)
        planned = round_ + 1
        emit("extension", {"round": planned, "guidance": guidance, "max_rounds": max_rounds})
