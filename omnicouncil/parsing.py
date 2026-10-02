"""Parsing of model output: confidence, consensus score, JSON verdicts (with fallbacks), dispute guidance."""

from __future__ import annotations

import json
import re
from typing import Optional

from .agent import AgentResult

_CONFIDENCE_ALIASES = {"高": "高", "中": "中", "低": "低", "high": "高", "medium": "中", "low": "低"}


def parse_confidence(text: str) -> str | None:
    """从裁决文本中解析确信度，统一为 高/中/低（兼容 high/medium/low、置信度 等写法）。"""
    m = re.search(r"(?:确信度|置信度|confidence)\s*[:：]?\s*[*「【\[<]*\s*(高|中|低|high|medium|low)",
                  text, re.IGNORECASE)
    return _CONFIDENCE_ALIASES[m.group(1).lower()] if m else None


_CONF_FROM_SCORE = {1.0: "高", 0.5: "中", 0.0: "低"}


def normalize_score(value) -> Optional[float]:
    """把模型给出的分数规整到 {0, 0.5, 1.0}；无法解析时返回 None。"""
    try:
        v = float(str(value).strip().rstrip("%"))
    except (TypeError, ValueError):
        return None
    if 10 < v <= 100:  # 百分制，例如 "50" 或 "100"
        v = v / 100
    if not 0 <= v <= 1:
        return None
    return min((0.0, 0.5, 1.0), key=lambda s: abs(s - v))


def _json_candidates(text: str) -> list[str]:
    cands = [m.group(1) for m in re.finditer(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)]
    first, last = text.find("{"), text.rfind("}")
    if first != -1 and last > first:
        cands.append(text[first:last + 1])
        # 逐个起点做括号配对（容忍 JSON 前后夹杂说明文字）
        depth, in_str, esc, start = 0, False, False, None
        for i, ch in enumerate(text):
            if in_str:
                esc = (ch == "\\") and not esc
                if ch == '"' and not esc:
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                if depth == 0:
                    start = i
                depth += 1
            elif ch == "}" and depth:
                depth -= 1
                if depth == 0 and start is not None:
                    cands.append(text[start:i + 1])
    return cands


_SECTION_FINAL = re.compile(r"^#{1,4}\s*(?:最终定论|最终答案|Final verdict|Final answer)\b.*$", re.IGNORECASE | re.MULTILINE)


def _markdown_section(text: str, heading: re.Pattern) -> Optional[str]:
    m = heading.search(text)
    if not m:
        return None
    rest = text[m.end():]
    nxt = re.search(r"^#{1,4}\s", rest, re.MULTILINE)
    section = (rest[:nxt.start()] if nxt else rest).strip()
    return section or None


def parse_judge_output(text: str) -> dict:
    """解析 Leader / 复审的输出。

    优先解析 JSON（允许代码块包裹、前后夹杂文字、字符串里的原始换行）；
    失败时用正则回退：从 "consensus_score": x / 共识度：x 中取分数，从 "final_answer" 或
    「### 最终定论 / Final verdict」一节中取正文，实在没有就用全文。
    返回 {consensus_score, confidence, final_answer, analysis, structured}。
    """
    data = None
    for cand in _json_candidates(text):
        try:
            obj = json.loads(cand, strict=False)
        except ValueError:
            continue
        if isinstance(obj, dict) and ("final_answer" in obj or "consensus_score" in obj):
            data = obj
            break

    if data is not None:
        score = normalize_score(data.get("consensus_score"))
        conf_raw = str(data.get("confidence", "")).strip().lower()
        confidence = _CONFIDENCE_ALIASES.get(conf_raw)
        final = str(data.get("final_answer") or "").strip()
        analysis = str(data.get("analysis") or "").strip()
        structured = True
    else:
        m = re.search(r'consensus[_ ]?score"?\s*[:：=]\s*"?([0-9.]+%?)', text, re.IGNORECASE) or \
            re.search(r"(?:共识度|共识分)\s*[:：]?\s*\**\s*([0-9.]+%?)", text)
        score = normalize_score(m.group(1)) if m else None
        m = re.search(r'"?confidence"?\s*[:：]\s*"([^"]+)"', text, re.IGNORECASE)
        confidence = _CONFIDENCE_ALIASES.get(m.group(1).strip().lower()) if m else None
        # 完整的 "final_answer": "..."，或被截断（没有结尾引号）的情况
        m = (re.search(r'"final_answer"\s*:\s*"(.*?)"\s*(?:,\s*"\w+"\s*:|\})', text, re.DOTALL)
             or re.search(r'"final_answer"\s*:\s*"(.*)', text, re.DOTALL))
        if m:
            raw = m.group(1).rstrip().rstrip('}').rstrip().rstrip('"')
            try:
                final = json.loads(f'"{raw}"', strict=False)
            except ValueError:
                final = raw.replace("\\n", "\n").replace('\\"', '"')
        else:
            final = _markdown_section(text, _SECTION_FINAL) or text.strip()
        analysis = ""
        structured = False

    confidence = confidence or parse_confidence(text) or (_CONF_FROM_SCORE.get(score) if score is not None else None)
    return {"consensus_score": score, "confidence": confidence, "final_answer": final or text.strip(),
            "analysis": analysis, "structured": structured}


def apply_judge_parse(result: AgentResult) -> None:
    """把解析结果写入 result.extra（confidence / consensus_score / final_answer / analysis / structured）。"""
    result.extra.update(parse_judge_output(result.output))


_GUIDANCE_HEADING = re.compile(r"^#{1,4}\s*(?:争议指导意见|争议指导|Dispute guidance)\b.*$", re.IGNORECASE | re.MULTILINE)


def extract_guidance(verdict_text: str, limit: int = 3000) -> str:
    """取出 Leader 裁决中的「争议指导意见」一节；没有该节时退回整段裁决（截断）。"""
    m = _GUIDANCE_HEADING.search(verdict_text)
    if m:
        rest = verdict_text[m.end():]
        nxt = re.search(r"^#{1,4}\s", rest, re.MULTILINE)
        section = (rest[:nxt.start()] if nxt else rest).strip()
        if section:
            return section[:limit]
    return verdict_text.strip()[:limit]
