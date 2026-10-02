"""Multimodal pre-processing.

多模态前置解析：由 Leader 先把附件（图片 / PDF / 语音）转成文字，再交给 Workers

各 Worker CLI 对文件的支持参差不齐，因此只让 Leader 读附件：
  · 文本类附件（.txt / .md / .csv …）直接在本地读取，不调用模型
  · 其余附件交给当前 Leader；它不支持的类型（如 claude 不能转录音频）自动改用池中能处理的 Leader
  · 同一个解析模型的所有附件合并为一次调用；不同解析模型并发执行
解析调用使用单次进程（附件目录授权、临时启用读取工具），不经过常驻进程池。

事件：
  preprocess_start  {index, spec, files}      files 为文件名列表
  preprocess_done   {index, spec, files, result}
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional

from . import i18n, prompts
from .agent import AgentResult
from .config import PROMPT_FLAGS, PROMPT_PLACEHOLDER, AgentSpec
from .orchestrate import EventHandler
from .runner import run_agent

FILE_KINDS = {
    "image": {".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic", ".bmp", ".tif", ".tiff"},
    "pdf": {".pdf"},
    "audio": {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".aiff", ".aif"},
    "text": {".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".log", ".yaml", ".yml", ".xml", ".html", ".py"},
}
TEXT_INLINE_LIMIT = 20000  # 本地内联的文本附件最大字符数

def file_kind(path: str) -> Optional[str]:
    ext = Path(path).suffix.lower()
    return next((k for k, exts in FILE_KINDS.items() if ext in exts), None)


def build_file_spec(leader: AgentSpec, files: list[str]) -> AgentSpec:
    """在 Leader 的命令中加入附件授权：目录 / 文件参数插在 prompt 之前；必要时临时启用读取工具。"""
    from dataclasses import replace as _replace
    cmd = _replace(leader, web_search=False).argv_template()  # 解析附件只需要读取工具
    if leader.file_tools and "--tools" in cmd:
        i = cmd.index("--tools")
        cmd[i + 1] = leader.file_tools
    targets = sorted({str(Path(f).resolve().parent) for f in files}) if leader.file_arg == "dir" \
        else [str(Path(f).resolve()) for f in files]
    file_args: list[str] = []
    for target in targets:
        file_args += [leader.file_flag + target] if leader.file_flag.endswith("=") else [leader.file_flag, target]
    idx = cmd.index(PROMPT_PLACEHOLDER) if PROMPT_PLACEHOLDER in cmd else len(cmd)
    if idx > 1 and cmd[idx - 1] in PROMPT_FLAGS:
        idx -= 1
    cmd = cmd[:idx] + file_args + cmd[idx:]
    return _replace(leader, command=cmd, selected_model="", is_persistent=False, web_search=False,
                    timeout=max(leader.timeout, 300))


def _choose_processor(kind: str, leader: AgentSpec, leaders: list[AgentSpec]) -> Optional[AgentSpec]:
    if leader.can_read(kind) and leader.installed:
        return leader
    return next((s for s in leaders if s.can_read(kind) and s.installed), None)


def combine_with_material(question: str, material: str) -> str:
    en = i18n.current() == "en"
    if not question.strip():
        question = "Please analyze the material above." if en else "请基于以上资料进行分析。"
    head, tail = ("[Multimodal material extracted from attachments]", "[User question]") if en \
        else ("[多模态资料提取]", "[用户问题]")
    return f"{head}\n{material}\n\n{tail}\n{question}"


async def preprocess_multimodal(question: str, attachments: list[str], leader: AgentSpec,
                                leaders: Optional[list[AgentSpec]] = None,
                                on_event: Optional[EventHandler] = None) -> tuple[str, list[dict]]:
    """返回 (组合后的 prompt, 解析记录列表)。没有附件时原样返回问题。"""
    if not attachments:
        return question, []
    emit: EventHandler = on_event or (lambda kind, payload: None)
    en = i18n.current() == "en"
    sections: list[tuple[str, str]] = []     # (标题, 内容)，保持附件顺序
    records: list[dict] = []
    groups: dict[str, tuple[AgentSpec, list[str]]] = {}

    for path in attachments:
        name, kind = Path(path).name, file_kind(path)
        if kind == "text":
            try:
                text = Path(path).read_text(encoding="utf-8", errors="replace")
            except OSError as e:
                text = f"({e})"
            if len(text) > TEXT_INLINE_LIMIT:
                text = text[:TEXT_INLINE_LIMIT] + "\n…(truncated)"
            sections.append((name, text))
            records.append({"processor": "local", "files": [name], "ok": True, "output": text, "elapsed": 0.0})
            continue
        proc = _choose_processor(kind, leader, leaders or []) if kind else None
        if proc is None:
            msg = (f"(unsupported attachment type: {name})" if en else f"（不支持的附件类型：{name}）") if kind is None \
                else (f"(no configured Leader can read {kind} files: {name})" if en else f"（没有可解析 {kind} 的 Leader：{name}）")
            sections.append((name, msg))
            records.append({"processor": None, "files": [name], "ok": False, "error": msg, "elapsed": 0.0})
            continue
        groups.setdefault(proc.name, (proc, []))[1].append(path)

    async def run_group(index: int, proc: AgentSpec, files: list[str]) -> tuple[AgentSpec, list[str], AgentResult]:
        names = [Path(f).name for f in files]
        emit("preprocess_start", {"index": index, "spec": proc, "files": names})
        listing = "\n".join(f"- {Path(f).resolve()}" for f in files)
        prompt = prompts.get("preprocess").format(files=listing)
        r = await run_agent(build_file_spec(proc, files), prompt)
        r.name = proc.name
        emit("preprocess_done", {"index": index, "spec": proc, "files": names, "result": r})
        return proc, files, r

    results = await asyncio.gather(*(run_group(i, p, fs) for i, (p, fs) in enumerate(groups.values())))
    for proc, files, r in results:
        names = [Path(f).name for f in files]
        title = ", ".join(names)
        sections.append((title, r.output if r.ok else (f"(extraction failed: {r.error_summary})" if en
                                                        else f"（解析失败：{r.error_summary}）")))
        records.append({"processor": proc.name, "files": names, "ok": r.ok, "output": r.output,
                        "error": r.error_brief, "elapsed": round(r.elapsed, 2)})

    # 解析模型按要求已用「### 文件名」分节时，不再重复加标题
    material = "\n\n".join(body.strip() if body.lstrip().startswith("### ") else f"### {title}\n{body.strip()}"
                             for title, body in sections)
    return combine_with_material(question, material), records
