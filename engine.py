"""AgentManager 核心引擎 —— 基于本地 AI CLI 的多智能体协作与仲裁 (MoA)。

流程：
    1. Workers 并发：把同一个问题同时发给多个本地 CLI（见 config.json）。
    2. Leader 仲裁：把原问题 + 所有 Worker 的回答拼成 Judge Prompt，
       交给高阶模型评估准确性、一致性、确信度，并给出最终定论。
    3. 若 Leader 判定确信度为「低」，触发一次二次复审（带上首轮裁决重新仲裁）。

分层：
    orchestrate()  纯逻辑，通过 on_event 回调报告进度，不做任何输出 —— CLI 与 GUI 共用。
    main_flow()    终端前端（rich），订阅 orchestrate 的事件进行渲染。入口见 main.py。
    gui.py         桌面前端（PySide6）。
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Optional

import logging
import shlex

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TaskID, TextColumn, TimeElapsedColumn
from rich.status import Status
from rich.table import Table
from rich.text import Text

import i18n
from pathlib import Path

from config import PROMPT_FLAGS, PROMPT_PLACEHOLDER, AgentSpec, Config
from i18n import t

# ---------------------------------------------------------------------------
# 核心：异步执行一个 CLI Agent（不负责输出，只返回结果）
# ---------------------------------------------------------------------------


_ERROR_LINE = re.compile(r"error|limit|fail|denied|not found|timed? ?out|exception|未找到|超时|失败|无法", re.IGNORECASE)


@dataclass
class AgentResult:
    name: str
    ok: bool
    output: str = ""
    error: str = ""
    elapsed: float = 0.0
    returncode: int | None = None
    extra: dict = field(default_factory=dict)

    @property
    def error_brief(self) -> str:
        """去掉 Node 堆栈行后的错误信息。"""
        lines = [ln for ln in self.error.splitlines() if not ln.lstrip().startswith("at ")]
        return "\n".join(lines).strip()

    @property
    def error_summary(self) -> str:
        """一行错误摘要：取最后一条像错误信息的行（CLI 常先输出启动日志，真正原因在末尾）。"""
        lines = [ln.strip() for ln in self.error_brief.splitlines() if ln.strip()]
        if not lines:
            return "失败"
        for ln in reversed(lines):
            if _ERROR_LINE.search(ln):
                return ln
        return lines[-1]


log = logging.getLogger("agentmanager.engine")

_running_procs: set[asyncio.subprocess.Process] = set()


def describe_argv(argv: list[str], prompt: str) -> str:
    """用于日志的命令行：prompt 只显示长度与开头，避免刷屏或泄露长上下文。"""
    shown = []
    for a in argv:
        if prompt and a == prompt:
            head = prompt[:40].replace("\n", " ")
            shown.append(f"<prompt {len(prompt)} chars: {head}…>")
        else:
            shown.append(a)
    return shlex.join(shown)


def kill_running_processes() -> int:
    """立即结束所有仍在运行的 CLI 子进程（含常驻进程池，GUI 关闭窗口时调用）。返回结束的数量。"""
    POOL.terminate_all()
    n = 0
    for proc in list(_running_procs):
        if proc.returncode is None:
            proc.kill()
            n += 1
    return n


# ---------------------------------------------------------------------------
# 常驻进程池（Warm Pool）
# ---------------------------------------------------------------------------
#
# 冷启动（Node 运行时、登录校验、配置 / 插件加载）往往占一次调用的大半时间。对支持流式协议的 CLI，
# 我们让进程常驻：stdin 每行写入一条 JSON 消息，stdout 每行读取一个 JSON 事件。
#
# 「回答完毕」判定：不解析 TUI 文本（交互式 REPL 需要真实终端，且输出夹杂颜色码 / 光标控制符），
# 而是使用 CLI 官方的结构化流协议中明确的结束事件（策略 B）：
#   claude  --input-format stream-json --output-format stream-json → {"type": "result", ...}
#   agy     --print= --input-format stream-json --output-format stream-json → {"event": "result", ...}
# 因此无需正则剥离 ANSI，也不依赖「N 秒无输出」这类时间窗口猜测；超时只作为兜底。
#
# 上下文隔离（按「运行」划分会话）：每次 orchestrate / run_cowork_loop 生成一个 session id。
#   · 同一次运行内（如 Co-work 的第 1–4 轮、Leader 的多次总结）沿用同一个会话：后续轮次命中已初始化的会话
#     与提示词缓存，最快（实测约 1s，冷启动约 5s）。
#   · 运行结束后（end_session）在后台用 /clear 重置会话（不调用模型），利用用户思考的间隙完成重新初始化；
#     不同问题之间绝不共享上下文。重置进行中到来的请求会等待重置完成，而不是回退冷启动。
#   · 预热时也先 /clear 一次，提前完成会话初始化。
# agy 的 print 模式不支持 /clear，只能每 max_turns_per_process 次请求重建进程（因此 agy 默认不开启常驻）。
#
# 生命周期：GUI 启动时预热（启动进程并完成会话初始化，不调用模型、不消耗额度）；模型 / 勾选变化时同步；
# 写入前检查进程是否存活，崩溃自动拉起（Auto-healing）；超时 / 被取消时杀掉进程，下次调用自动重建；
# 应用退出时关闭 stdin 并 terminate，必要时 kill。

POOL_STREAM_LIMIT = 32 * 1024 * 1024   # 单行 JSON 可能很大（如初始化事件中的工具列表）


class StreamProtocol:
    """某个 CLI 的常驻流式协议。"""

    name = ""
    extra_args: tuple[str, ...] = ()
    reset_command: Optional[str] = None   # 任务之间清空上下文的命令；None 表示不支持

    def argv(self, spec: AgentSpec) -> Optional[list[str]]:
        """把单次调用的命令模板改造成常驻命令；无法安全改造时返回 None。"""
        tpl = spec.argv_template()
        if PROMPT_PLACEHOLDER not in tpl:
            return None
        i = tpl.index(PROMPT_PLACEHOLDER)
        if i > 1 and tpl[i - 1] in PROMPT_FLAGS:
            del tpl[i - 1:i + 1]
        else:
            del tpl[i]
        if any(PROMPT_PLACEHOLDER in a for a in tpl):
            return None
        return tpl + list(self.extra_args)

    def encode(self, text: str) -> bytes:
        raise NotImplementedError

    def parse(self, event: dict) -> Optional[tuple[bool, str]]:
        """返回 (ok, 文本) 表示本轮结束；返回 None 表示继续读取。"""
        raise NotImplementedError


class ClaudeStream(StreamProtocol):
    name = "claude stream-json"
    extra_args = ("-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose")
    reset_command = "/clear"

    def encode(self, text: str) -> bytes:
        msg = {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": text}]}}
        return (json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8")

    def parse(self, event: dict) -> Optional[tuple[bool, str]]:
        if event.get("type") == "rate_limit_event" and event.get("rate_limit_info"):
            _note_claude_rate_limit(event["rate_limit_info"])
        if event.get("type") != "result":
            return None
        ok = not event.get("is_error") and event.get("subtype", "success") == "success"
        return ok, str(event.get("result") or event.get("error") or "")


class AgyStream(StreamProtocol):
    name = "agy stream-json"
    extra_args = ("--print=", "--input-format", "stream-json", "--output-format", "stream-json")

    def encode(self, text: str) -> bytes:
        msg = {"event": "user", "message": {"role": "user", "content": [{"type": "text", "text": text}]}}
        return (json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8")

    def parse(self, event: dict) -> Optional[tuple[bool, str]]:
        if event.get("event") != "result":
            return None
        r = event.get("result") or {}
        ok = r.get("status") == "SUCCESS"
        return ok, str(r.get("response") if ok else (r.get("error") or r.get("response") or ""))


PROTOCOLS: dict[str, StreamProtocol] = {"claude": ClaudeStream(), "agy": AgyStream()}


def _note_claude_rate_limit(info: dict) -> None:
    """常驻 claude 顺带返回的配额信息，免费更新「账户与用量」缓存（无需额外请求）。"""
    try:
        import accounts
        accounts.save_claude_rate_limit(info)
    except Exception:  # 配额缓存失败不影响回答
        log.debug("could not cache claude rate limit", exc_info=True)


class PersistentAgent:
    """一个常驻 CLI 进程；同一时间只处理一个请求（由 lock 保证）。"""

    def __init__(self, spec: AgentSpec, protocol: StreamProtocol, argv: list[str]):
        self.spec = spec
        self.protocol = protocol
        self.argv = argv
        self.proc: Optional[asyncio.subprocess.Process] = None
        self.lock = asyncio.Lock()
        self.turns = 0
        self.starts = 0
        self._killed = False
        self.dirty = False        # 会话中已有内容，下次用于其他运行前需要重置
        self.resetting = False    # 正在后台重置（此时到来的请求应等待，而不是回退冷启动）
        self.session: Optional[str] = None  # 当前会话属于哪一次运行
        self._stderr_tail = ""
        self._stderr_task: Optional[asyncio.Task] = None
        self._reset_task: Optional[asyncio.Task] = None

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.returncode is None and not self._killed

    @property
    def busy(self) -> bool:
        return self.lock.locked()

    async def start(self, eager_init: bool = False) -> None:
        """启动进程。eager_init=True 时在后台发送一次重置命令，提前完成会话初始化（预热用）。"""
        if self.alive:
            return
        self.starts += 1
        self.turns = 0
        self._killed = False
        self.dirty = False
        self._stderr_tail = ""
        log.info("[%s] pool %s process (%s): %s", self.spec.name, "start" if self.starts == 1 else "restart",
                 self.protocol.name, shlex.join(self.argv))
        self.proc = await asyncio.create_subprocess_exec(
            *self.argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env=self.spec.env(), limit=POOL_STREAM_LIMIT)
        _running_procs.add(self.proc)
        self._stderr_task = asyncio.ensure_future(self._drain_stderr(self.proc))
        if eager_init and self.protocol.reset_command:
            self.dirty = True
            self.schedule_reset()

    def schedule_reset(self) -> None:
        if self.protocol.reset_command and (self._reset_task is None or self._reset_task.done()):
            self._reset_task = asyncio.ensure_future(self._background_reset())

    async def _background_reset(self) -> None:
        async with self.lock:
            if not (self.alive and self.dirty):
                return
            self.resetting = True
            t0 = time.monotonic()
            try:
                await self._send(self.protocol.reset_command, time.monotonic() + 90)
                self.dirty = False
                log.info("[%s] pool session reset in background (%.1fs)", self.spec.name, time.monotonic() - t0)
            except (ConnectionError, BrokenPipeError, ConnectionResetError, asyncio.TimeoutError):
                self.kill()  # 下次请求时自动重建
            finally:
                self.resetting = False

    async def _drain_stderr(self, proc: asyncio.subprocess.Process) -> None:
        """持续读走 stderr，避免管道写满导致子进程阻塞；只保留末尾用于报错。"""
        try:
            while True:
                chunk = await proc.stderr.read(4096)
                if not chunk:
                    break
                self._stderr_tail = (self._stderr_tail + chunk.decode("utf-8", "replace"))[-4000:]
        except Exception:
            pass

    def kill(self) -> None:
        self._killed = True  # 立即视为失效（returncode 要等事件循环回收后才更新）
        if self.proc is not None and self.proc.returncode is None:
            try:
                self.proc.kill()
            except ProcessLookupError:
                pass
        if self.proc is not None:
            _running_procs.discard(self.proc)

    async def _send(self, text: str, deadline: float) -> tuple[bool, str]:
        """发送一条消息并读到本轮结束事件。"""
        assert self.proc is not None
        self.proc.stdin.write(self.protocol.encode(text))
        await self.proc.stdin.drain()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise asyncio.TimeoutError
            line = await asyncio.wait_for(self.proc.stdout.readline(), timeout=remaining)
            if not line:
                raise ConnectionError("process exited")
            try:
                event = json.loads(line)
            except ValueError:
                continue  # 非 JSON 行（极少见的调试输出）直接跳过
            if isinstance(event, dict):
                done = self.protocol.parse(event)
                if done is not None:
                    return done

    async def ask(self, prompt: str, session: Optional[str] = None) -> AgentResult:
        async with self.lock:
            start = time.monotonic()
            deadline = start + self.spec.timeout
            for attempt in (1, 2):
                try:
                    recycle = self.protocol.reset_command is None and self.turns >= self.spec.max_turns_per_process
                    if recycle:
                        log.info("[%s] pool recycle after %d turns", self.spec.name, self.turns)
                        await self.close()
                    if not self.alive:  # Auto-healing：首次启动或进程意外退出
                        await self.start()
                    same_run = session is not None and session == self.session
                    if self.protocol.reset_command and self.dirty and not same_run:  # 换了运行：先清空会话（兜底）
                        await self._send(self.protocol.reset_command, deadline)
                        self.dirty = False
                    ok, text = await self._send(prompt, deadline)
                    self.turns += 1
                    self.dirty = True
                    self.session = session
                    break
                except (ConnectionError, BrokenPipeError, ConnectionResetError):
                    # 进程在请求中途退出：重启后重试一次
                    tail = self._stderr_tail.strip()
                    self.kill()
                    if attempt == 2:
                        return AgentResult(self.spec.name, ok=False, error=tail or t("eng.empty"),
                                           elapsed=time.monotonic() - start, extra={"pool": True})
                    log.warning("[%s] pool process died, restarting: %s", self.spec.name, tail[-200:])
                except asyncio.TimeoutError:
                    self.kill()  # 状态未知，下次调用时重建
                    return AgentResult(self.spec.name, ok=False, error=t("eng.timeout", s=f"{self.spec.timeout:.0f}"),
                                       elapsed=time.monotonic() - start, extra={"pool": True})
                except asyncio.CancelledError:
                    self.kill()  # 回答进行到一半被取消：丢弃该进程，避免残留输出串到下一轮
                    raise
            elapsed = time.monotonic() - start
            if session is None:  # 不属于任何运行的单次调用：用完立即在后台清空
                self.schedule_reset()
            text = text.strip()
            if not ok:
                return AgentResult(self.spec.name, ok=False, error=text or t("eng.empty"), elapsed=elapsed,
                                   returncode=0, extra={"pool": True})
            if not text:
                return AgentResult(self.spec.name, ok=False, error=t("eng.empty"), elapsed=elapsed, returncode=0,
                                   extra={"pool": True})
            return AgentResult(self.spec.name, ok=True, output=text, elapsed=elapsed, returncode=0,
                               extra={"pool": True})

    async def close(self) -> None:
        """优雅退出：关闭 stdin（流式模式据此结束）→ 等待 → terminate → kill。"""
        proc = self.proc
        if proc is None:
            return
        if proc.returncode is None:
            try:
                proc.stdin.close()
            except Exception:
                pass
            try:
                await asyncio.wait_for(proc.wait(), 2)
            except asyncio.TimeoutError:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), 2)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
        _running_procs.discard(proc)
        if self._stderr_task:
            self._stderr_task.cancel()
        if self._reset_task and not self._reset_task.done():
            self._reset_task.cancel()
        self.proc = None


class AgentPool:
    """全局常驻进程池：按「注入模型后的命令 + 环境」区分进程。"""

    def __init__(self) -> None:
        self.enabled = True
        self.agents: dict[tuple, PersistentAgent] = {}

    @staticmethod
    def _key(spec: AgentSpec) -> tuple:
        # 含名称：即使 Worker 与 Leader 命令相同也各用一个进程，Leader 不会在会话里看到 Worker 的回答
        return spec.name, tuple(spec.argv_template()), tuple(sorted(spec.env_unset))

    def protocol_for(self, spec: AgentSpec) -> Optional[StreamProtocol]:
        if not (self.enabled and spec.is_persistent):
            return None
        return PROTOCOLS.get(Path(spec.command[0]).name)

    def supports(self, spec: AgentSpec) -> bool:
        proto = self.protocol_for(spec)
        return proto is not None and proto.argv(spec) is not None and shutil.which(spec.command[0]) is not None

    def get(self, spec: AgentSpec) -> Optional[PersistentAgent]:
        if not self.supports(spec):
            return None
        key = self._key(spec)
        agent = self.agents.get(key)
        if agent is None:
            proto = self.protocol_for(spec)
            agent = self.agents[key] = PersistentAgent(spec, proto, proto.argv(spec))
        return agent

    async def ask(self, spec: AgentSpec, prompt: str, session: Optional[str] = None) -> Optional[AgentResult]:
        """由常驻进程回答；不支持或进程正忙（例如另一个会话正在使用）时返回 None，由调用方回退为单次调用。"""
        agent = self.get(spec)
        if agent is None or (agent.busy and not agent.resetting):
            return None
        return await agent.ask(prompt, session)

    def end_session(self, session: Optional[str]) -> None:
        """一次运行结束：在后台重置为它服务过的进程，下一个问题从干净的会话开始。"""
        if session is None:
            return
        for agent in self.agents.values():
            if agent.session == session and agent.dirty:
                agent.schedule_reset()

    async def sync(self, specs: list[AgentSpec]) -> None:
        """让池与当前配置一致：关闭不再需要的进程，预热需要的进程（只启动，不发请求）。"""
        wanted = {self._key(s): s for s in specs if self.supports(s)}
        for key in [k for k in self.agents if k not in wanted]:
            agent = self.agents.pop(key)
            if not agent.busy:
                log.info("[%s] pool retire: %s", agent.spec.name, shlex.join(agent.argv))
                await agent.close()
        await asyncio.gather(*(self.get(s).start(eager_init=True) for s in wanted.values()), return_exceptions=True)

    def status(self) -> list[tuple[str, bool, bool]]:
        """[(名称, 存活, 忙碌)]，供界面显示。"""
        return [(a.spec.name, a.alive, a.busy) for a in self.agents.values()]

    async def shutdown(self) -> None:
        agents, self.agents = list(self.agents.values()), {}
        await asyncio.gather(*(a.close() for a in agents), return_exceptions=True)

    def terminate_all(self) -> None:
        """同步终止所有常驻进程（事件循环即将停止时使用）。"""
        for a in list(self.agents.values()):
            if a.proc is not None and a.proc.returncode is None:
                try:
                    a.proc.stdin.close()
                except Exception:
                    pass
                try:
                    a.proc.terminate()
                except ProcessLookupError:
                    pass
        self.agents = {}


POOL = AgentPool()


async def run_agent(spec: AgentSpec, prompt: str, session: Optional[str] = None) -> AgentResult:
    """调用一个本地 CLI，捕获 stdout；处理未安装、超时、非零退出码等异常。
    被取消（CancelledError）时会结束子进程。session 为所属运行的 id（常驻进程据此决定是否沿用会话）。"""
    exe = spec.command[0]
    if shutil.which(exe) is None:
        return AgentResult(spec.name, ok=False, error=t("eng.not_found", exe=exe))

    # 常驻进程优先；不支持 / 正忙时回退为单次调用
    pooled = await POOL.ask(spec, prompt, session)
    if pooled is not None:
        log.info("[%s] answered by warm process in %.1fs", spec.name, pooled.elapsed)
        return pooled

    argv = spec.build(prompt)
    log.info("[%s] exec: %s", spec.name, describe_argv(argv, prompt))
    start = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,  # 防止 CLI 等待交互输入
            env=spec.env(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (FileNotFoundError, PermissionError) as e:
        return AgentResult(spec.name, ok=False, error=t("eng.cant_start", e=e))

    _running_procs.add(proc)
    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=spec.timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return AgentResult(spec.name, ok=False, error=t("eng.timeout", s=f"{spec.timeout:.0f}"),
                           elapsed=time.monotonic() - start)
    except asyncio.CancelledError:
        if proc.returncode is None:
            proc.kill()
        raise
    finally:
        _running_procs.discard(proc)

    elapsed = time.monotonic() - start
    stdout = stdout_b.decode("utf-8", errors="replace").strip()
    stderr = stderr_b.decode("utf-8", errors="replace").strip()

    if proc.returncode != 0:
        return AgentResult(spec.name, ok=False, output=stdout, error=stderr or "non-zero exit",
                           elapsed=elapsed, returncode=proc.returncode)

    # 部分 CLI（如 agy）出错时仍返回 0，把错误打印成普通输出
    if stdout.lower().startswith("error:"):
        return AgentResult(spec.name, ok=False, error=stdout, elapsed=elapsed, returncode=0)

    if not stdout:
        return AgentResult(spec.name, ok=False, error=t("eng.empty"), elapsed=elapsed, returncode=0)

    return AgentResult(spec.name, ok=True, output=stdout, elapsed=elapsed, returncode=0)


# ---------------------------------------------------------------------------
# Prompt 模板
# ---------------------------------------------------------------------------

_SCORE_RULES_ZH = """## 共识评分规则：consensus_score（只能是 1.0、0.5、0 三者之一）
- 1.0：所有助手的核心答案完全一致。
- 0.5：多数助手（如三个中的两个）答案相近或一致，但至少有一个给出了明显不同的答案。
- 0：各助手的答案各不相同，无法形成多数共识。
（只有两个助手时：一致为 1.0，不一致为 0；只有一个助手时为 1.0。）"""

_SCORE_RULES_EN = """## Consensus scoring: consensus_score (exactly one of 1.0, 0.5 or 0)
- 1.0: every assistant's core answer is the same.
- 0.5: most assistants (e.g. two of three) give similar or matching answers, but at least one gives a clearly different answer.
- 0: the assistants' answers all differ; there is no majority.
(With only two assistants: 1.0 if they agree, 0 if not. With only one assistant: 1.0.)"""

_JSON_SPEC_ZH = """## 输出要求
只输出一个 JSON 对象，不要输出任何其他文字，也不要使用代码块标记：
{{"consensus_score": 1.0, "confidence": "高", "analysis": "…", "final_answer": "…"}}
- consensus_score：按上述规则给出 1.0 / 0.5 / 0
- confidence：你对最终答案的确信度，只能是 "高"、"中"、"低" 之一
- analysis：逐一评估各回答的准确性与一致性，指出错误与分歧（Markdown）
- final_answer：直接回答用户的最终答案（Markdown），综合各方优点，就像你亲自回答用户一样"""

_JSON_SPEC_EN = """## Output
Output only one JSON object — no other text and no code fences:
{{"consensus_score": 1.0, "confidence": "high", "analysis": "…", "final_answer": "…"}}
- consensus_score: 1.0 / 0.5 / 0 according to the rules above
- confidence: your confidence in the final answer — exactly one of "high", "medium" or "low"
- analysis: assess each answer's accuracy and consistency, pointing out errors and disagreements (Markdown)
- final_answer: the final answer addressed directly to the user (Markdown), combining the strengths of the answers, as if you were answering the user yourself"""

JUDGE_TEMPLATE = """你是一名严谨的首席评审（Leader / Judge）。{n} 个独立的 AI 助手回答了同一个问题，请你对它们进行综合评审。

## 原始问题
{question}

## 各助手的回答
{answers}

""" + _SCORE_RULES_ZH + "\n\n" + _JSON_SPEC_ZH + "\n"

JUDGE_TEMPLATE_EN = """You are a rigorous chief reviewer (Leader / Judge). {n} independent AI assistants answered the same question. Review them together.

## Original question
{question}

## The assistants' answers
{answers}

""" + _SCORE_RULES_EN + "\n\n" + _JSON_SPEC_EN + "\n"

REVIEW_TEMPLATE = """你是一名独立的二次复审专家（Secondary Reviewer）。首轮评审（{first_leader}）对下面的问题给出了「低」确信度，说明各回答之间存在争议或难以判定。请你作为来自不同模型的第二意见，重新独立思考并给出更可靠的结论。重点核查首轮评审中的争议点，不要盲从首轮结论，必要时推翻它。

## 原始问题
{question}

## 各助手的原始回答
{answers}

## 首轮评审意见（含争议点）
{first_verdict}

""" + _SCORE_RULES_ZH + "\n\n" + _JSON_SPEC_ZH + "\n"

REVIEW_TEMPLATE_EN = """You are an independent Secondary Reviewer. The first-round reviewer ({first_leader}) gave the question below "low" confidence, meaning the answers disagree or are hard to judge. As a second opinion from a different model, think it through independently and reach a more reliable conclusion. Focus on the disputed points; don't simply follow the first-round conclusion, and overturn it if needed.

## Original question
{question}

## The assistants' original answers
{answers}

## First-round review (including disputed points)
{first_verdict}

""" + _SCORE_RULES_EN + "\n\n" + _JSON_SPEC_EN + "\n"


def _templates() -> tuple[str, str]:
    """按当前界面语言选择 Judge / Review 模板（模型会用同一语言回答）。"""
    if i18n.current() == "en":
        return JUDGE_TEMPLATE_EN, REVIEW_TEMPLATE_EN
    return JUDGE_TEMPLATE, REVIEW_TEMPLATE


def format_answers(results: list[AgentResult]) -> str:
    fmt = "### Assistant {i} ({name})\n{out}" if i18n.current() == "en" else "### 助手 {i}（{name}）\n{out}"
    return "\n\n".join(fmt.format(i=i, name=r.name, out=r.output) for i, r in enumerate(results, 1))


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


# ---------------------------------------------------------------------------
# 多轮对话：把近期对话历史拼成纯文本前缀（CLI 无法直接传历史 JSON）
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


def select_reviewer(pool: list[AgentSpec], leader: AgentSpec) -> Optional[AgentSpec]:
    """从 Leader 池中挑选复审模型：排除当前 Leader 与未安装的，
    优先不同厂商，其次 pro 档，最后按配置顺序。没有可选时返回 None。"""
    candidates = [s for s in pool if s.name != leader.name and s.installed]
    if not candidates:
        return None
    order = {s.name: i for i, s in enumerate(candidates)}
    return min(candidates, key=lambda s: (s.vendor == leader.vendor, s.tier != "pro", order[s.name]))


# ---------------------------------------------------------------------------
# 编排：纯逻辑 + 事件回调
# ---------------------------------------------------------------------------
#
# 事件（kind, payload）：
#   worker_start      {index, spec}
#   worker_done       {index, spec, result}
#   workers_finished  {results, ok}
#   leader_start      {spec, n_answers}
#   leader_done       {spec, result}               result.extra["confidence"] 为 高/中/低/None
#   review_start      {spec, leader, fallback}     首轮确信度低 → 异构模型复审；fallback=True 表示池中无其他可用模型，只能由原 Leader 复审
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

    answers = format_answers(ok)
    judge_template, review_template = _templates()
    emit("leader_start", {"spec": leader, "n_answers": len(ok)})
    first = await run_agent(leader, judge_template.format(n=len(ok), question=question, answers=answers), session)
    if first.ok:
        apply_judge_parse(first)
    emit("leader_done", {"spec": leader, "result": first})
    if not first.ok:
        return RunOutcome(results, first, EXIT_LEADER_FAILED, first_verdict=first)

    if not (review_on_low and first.extra["confidence"] == "低"):
        return RunOutcome(results, first, EXIT_OK, first_verdict=first)

    # —— 动态复审：换一个异构模型 ——
    reviewer = select_reviewer(reviewer_pool or [], leader)
    fallback = reviewer is None
    reviewer = reviewer or leader
    emit("review_start", {"spec": reviewer, "leader": leader, "fallback": fallback})
    review = await run_agent(reviewer, review_template.format(
        question=question, answers=answers, first_leader=leader.name, first_verdict=first.output), session)
    if review.ok:
        apply_judge_parse(review)
        review.extra.update(reviewed=True, first_confidence=first.extra["confidence"], first_leader=leader.name)
    emit("review_done", {"spec": reviewer, "result": review})
    # 复审失败时保留首轮裁决
    return RunOutcome(results, review if review.ok else first, EXIT_OK, first_verdict=first, review=review)


# ---------------------------------------------------------------------------
# 多模态前置解析：由 Leader 先把附件（图片 / PDF / 语音）转成文字，再交给 Workers
# ---------------------------------------------------------------------------
#
# 各 Worker CLI 对文件的支持参差不齐，因此只让 Leader 读附件：
#   · 文本类附件（.txt / .md / .csv …）直接在本地读取，不调用模型
#   · 其余附件交给当前 Leader；它不支持的类型（如 claude 不能转录音频）自动改用池中能处理的 Leader
#   · 同一个解析模型的所有附件合并为一次调用；不同解析模型并发执行
# 解析调用使用单次进程（附件目录授权、临时启用读取工具），不经过常驻进程池。
#
# 事件：
#   preprocess_start  {index, spec, files}      files 为文件名列表
#   preprocess_done   {index, spec, files, result}

FILE_KINDS = {
    "image": {".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic", ".bmp", ".tif", ".tiff"},
    "pdf": {".pdf"},
    "audio": {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".aiff", ".aif"},
    "text": {".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".log", ".yaml", ".yml", ".xml", ".html", ".py"},
}
TEXT_INLINE_LIMIT = 20000  # 本地内联的文本附件最大字符数

PREPROCESS_TEMPLATE = """你是一名资料预处理助手。请处理下列附件：
{files}

要求：
- 图片：精确提取其中的全部文字；若有图表或画面，详细描述其内容与关键数据。
- PDF：精确提取正文文字，保留标题、数字、日期等关键事实。
- 语音：逐字转录说话内容。
请只输出提取到的核心事实与内容，不要寒暄、不要评论、不要回答用户的问题——这些内容将作为背景资料发给其他 AI 助手。
多个附件时，用「### 文件名」分节输出。"""

PREPROCESS_TEMPLATE_EN = """You are a material pre-processing assistant. Process the following attachments:
{files}

Requirements:
- Images: extract all text exactly; if there are charts or scenes, describe their content and key data in detail.
- PDFs: extract the body text exactly, keeping headings, numbers, dates and other key facts.
- Audio: transcribe the speech verbatim.
Output only the extracted facts and content — no pleasantries, no commentary, and don't answer any question. This will be passed to other AI assistants as background material.
With several attachments, use one "### filename" section per file."""


def file_kind(path: str) -> Optional[str]:
    ext = Path(path).suffix.lower()
    return next((k for k, exts in FILE_KINDS.items() if ext in exts), None)


def build_file_spec(leader: AgentSpec, files: list[str]) -> AgentSpec:
    """在 Leader 的命令中加入附件授权：目录 / 文件参数插在 prompt 之前；必要时临时启用读取工具。"""
    from dataclasses import replace as _replace
    cmd = leader.argv_template()
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
    return _replace(leader, command=cmd, selected_model="", is_persistent=False,
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
        prompt = (PREPROCESS_TEMPLATE_EN if en else PREPROCESS_TEMPLATE).format(files=listing)
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


# ---------------------------------------------------------------------------
# Co-work 模式：多轮圆桌讨论 + Leader 总结（确信度低时延长讨论）
# ---------------------------------------------------------------------------
#
# 流程：Round 1 各 Worker 独立作答 → Round 2..N 每个 Worker 看到其他人上一轮的答案后修正 / 补充 / 坚持
#       → Leader 基于最后一轮答案总结；若确信度为「低」且未达 max_rounds，则提取「争议指导意见」作为下一轮
#       的讨论重点，再次讨论后重新总结，直到不再是「低」或达到 max_rounds。
#
# 并发与超时：同一轮内 Workers 并发（asyncio.gather），轮与轮之间串行；每次调用都有独立超时（run_agent），
# 不存在共享锁，因此不会死锁。某个 Worker 在某轮失败后不再参与后续轮次（避免反复超时拖慢整轮）。
#
# 事件（在 Judge 模式事件之外新增 / 扩展）：
#   round_start   {round, total, phase, status}       phase: independent / peer_review / guided
#   worker_start  {index, spec, round, total, phase}
#   worker_done   {index, spec, result, round}
#   round_done    {round, results, active}
#   leader_start  {spec, n_answers, round}
#   leader_done   {spec, result, round, will_extend}
#   extension     {round, guidance, max_rounds}       round 为即将开始的新一轮

PEER_ANSWER_LIMIT = 6000  # 传给同伴的单个回答最多字符数，避免 prompt 无限膨胀

COWORK_PEER_TEMPLATE = """你正在参与一场多位 AI 助手的圆桌讨论（第 {round}/{total} 轮）。

## 用户的原始问题
{question}

## 你在上一轮的回答
{own}

## 其他助手在上一轮的回答
{peers}
{guidance}
## 你的任务
{task}
请直接给出你本轮完整的最新答案（不要只评论他人），并简要说明你修正或坚持的理由。"""

COWORK_PEER_TEMPLATE_EN = """You are taking part in a round-table discussion among several AI assistants (round {round} of {total}).

## The user's original question
{question}

## Your answer from the previous round
{own}

## The other assistants' answers from the previous round
{peers}
{guidance}
## Your task
{task}
Give your complete, updated answer for this round (don't just comment on the others), and briefly explain what you revised or why you stand by it."""

COWORK_TASKS = {
    "zh": {
        "peer_review": "结合其他助手的观点，修正、补充或坚持你自己的答案。",
        "final": "这是最后一轮。请深度反思各方观点，给出你最终的、尽可能与他人达成共识的答案。",
        "guided": "Leader 认为各方仍存在分歧。请重点针对上面的「争议指导意见」逐条讨论，并给出你的答案。",
    },
    "en": {
        "peer_review": "Taking the other assistants' views into account, revise, extend or stand by your own answer.",
        "final": "This is the final round. Reflect deeply on everyone's views and give your final answer, converging with the others where you can.",
        "guided": "The Leader thinks the assistants still disagree. Address each point in the Leader's dispute guidance above, then give your answer.",
    },
}

COWORK_LEADER_TEMPLATE = """你是一场圆桌讨论的主持人兼首席评审（Leader）。{n} 位 AI 助手就同一问题进行了 {rounds} 轮讨论（每轮都能看到彼此上一轮的答案），下面是他们最后一轮的答案。
{guidance}
## 原始问题
{question}

## 各助手最后一轮的答案
{answers}

## 你的任务
1. **准确性**：评估各答案的事实与逻辑，指出明确的错误。
2. **共识分析**：各方是否已达成共识？列出仍然存在的分歧。
3. **共识度**：只能是 1.0、0.5、0 之一（1.0 = 所有助手核心答案一致；0.5 = 多数一致但至少一个明显不同；0 = 无法形成多数共识）。
4. **确信度**：只能是「高」「中」「低」之一；若仍存在严重分歧，请给「低」。
5. **最终定论**：综合讨论成果给出完整、准确的最终答案。
6. **争议指导意见**（仅当确信度为「低」时）：列出下一轮需要各助手重点解决的具体分歧点与核查方向。

请严格按以下格式输出（Markdown）：

### 准确性评估
...
### 共识分析
...
### 共识度：<1.0/0.5/0>
### 确信度：<高/中/低>
### 最终定论
...
### 争议指导意见
...（仅当确信度为低时）
"""

COWORK_LEADER_TEMPLATE_EN = """You are the moderator and chief reviewer (Leader) of a round-table discussion. {n} AI assistants discussed the same question over {rounds} round(s), each round seeing the others' previous answers. Below are their final-round answers.
{guidance}
## Original question
{question}

## The assistants' final-round answers
{answers}

## Your task
1. **Accuracy**: assess each answer's facts and reasoning, and point out clear errors.
2. **Consensus**: have the assistants reached consensus? List any remaining disagreements.
3. **Consensus score**: exactly one of 1.0, 0.5 or 0 (1.0 = every assistant's core answer is the same; 0.5 = a majority agrees but at least one clearly differs; 0 = no majority).
4. **Confidence**: exactly one of High, Medium or Low; if serious disagreement remains, give Low.
5. **Final verdict**: combine the discussion into one complete, accurate final answer.
6. **Dispute guidance** (only when confidence is Low): list the specific disagreements and what to check that the assistants should resolve in the next round.

Use exactly this format (Markdown):

### Accuracy
...
### Consensus
...
### Consensus score: <1.0/0.5/0>
### Confidence: <High/Medium/Low>
### Final verdict
...
### Dispute guidance
...(only when confidence is Low)
"""

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


def _clip(text: str, limit: int = PEER_ANSWER_LIMIT) -> str:
    return text if len(text) <= limit else text[:limit] + "\n…(truncated)"


def build_peer_prompt(question: str, own: AgentResult, peers: list[AgentResult], round_: int, total: int,
                      phase: str, guidance: Optional[str]) -> str:
    en = i18n.current() == "en"
    lang = "en" if en else "zh"
    peer_fmt = "### {name}'s view\n{out}" if en else "### {name} 的看法\n{out}"
    peers_text = "\n\n".join(peer_fmt.format(name=p.name, out=_clip(p.output)) for p in peers) or "—"
    guidance_text = ""
    if guidance:
        guidance_text = (f"\n## The Leader's dispute guidance\n{guidance}\n" if en
                         else f"\n## Leader 的争议指导意见\n{guidance}\n")
    task_key = "guided" if phase == "guided" else ("final" if round_ == total else "peer_review")
    template = COWORK_PEER_TEMPLATE_EN if en else COWORK_PEER_TEMPLATE
    return template.format(round=round_, total=total, question=question, own=_clip(own.output),
                           peers=peers_text, guidance=guidance_text, task=COWORK_TASKS[lang][task_key])


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
        template = COWORK_LEADER_TEMPLATE_EN if en else COWORK_LEADER_TEMPLATE
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


# ---------------------------------------------------------------------------
# 终端前端（rich）
# ---------------------------------------------------------------------------

console = Console()

AGENT_STYLES = ["magenta", "blue", "green", "cyan", "yellow"]
CONFIDENCE_STYLES = {"高": "green", "中": "yellow", "低": "red"}


def agent_style(index: int) -> str:
    return AGENT_STYLES[index % len(AGENT_STYLES)]


def print_failure(result: AgentResult, con: Console = console) -> None:
    """以红色高亮面板展示失败原因（含 returncode 与 stderr 末尾）。"""
    title = f"✗ {result.name} 执行失败"
    if result.returncode not in (None, 0):
        title += f"（returncode={result.returncode}）"
    con.print(Panel(Text(result.error_brief[-800:], style="red"), title=title, title_align="left",
                    border_style="bold red"))


def render_worker_answers(results: list[AgentResult], workers: list[AgentSpec]) -> None:
    styles = {w.name: agent_style(i) for i, w in enumerate(workers)}
    for r in results:
        if r.ok:
            console.print(Panel(Markdown(r.output), title=f"{r.name} 的回答", title_align="left",
                                border_style=styles.get(r.name, "white"), subtitle=f"{r.elapsed:.1f}s",
                                subtitle_align="right"))


SCORE_STYLES = {1.0: ("green", "🟢"), 0.5: ("yellow", "🟡"), 0.0: ("red", "🔴")}


def render_verdict(verdict: AgentResult) -> None:
    conf = verdict.extra.get("confidence")
    score = verdict.extra.get("consensus_score")
    border = CONFIDENCE_STYLES.get(conf, "yellow")
    title = "⚖  最终裁决" + (f"（经 {verdict.name} 二次复审）" if verdict.extra.get("reviewed") else "")
    if verdict.extra.get("mode") == "cowork":
        title = f"⚖  最终裁决（Co-work · {verdict.extra.get('round')} 轮讨论）"
    subtitle = f"确信度：{conf or '未知'}"
    if score is not None:
        style, dot = SCORE_STYLES[score]
        subtitle = f"[{style}]共识度：{ {1.0: '1.0', 0.5: '0.5', 0.0: '0'}[score]} {dot}[/]  ·  " + subtitle
    console.print()
    body = verdict.extra.get("final_answer") or verdict.output
    console.print(Panel(Markdown(body), title=f"[bold]{title}", subtitle=subtitle, border_style=border, padding=(1, 2)))


def render_summary(results: list[AgentResult], verdict: AgentResult | None) -> None:
    table = Table(title="运行摘要", title_justify="left", show_edge=False, header_style="bold")
    table.add_column("Agent")
    table.add_column("状态")
    table.add_column("耗时", justify="right")
    for r in results:
        brief = r.error_summary[:60]
        status = Text("✓ 成功", style="green") if r.ok else Text(f"✗ {brief}", style="red")
        table.add_row(r.name, status, f"{r.elapsed:.1f}s")
    if verdict is not None:
        conf = verdict.extra.get("confidence")
        status = (Text(f"确信度 {conf or '未知'}", style=CONFIDENCE_STYLES.get(conf, "yellow")) if verdict.ok
                  else Text("✗ 仲裁失败", style="red"))
        role = "复审" if verdict.extra.get("reviewed") else "Leader"
        table.add_row(f"{verdict.name}（{role}）", status, f"{verdict.elapsed:.1f}s")
    console.print(table)


PHASE_NAMES = {"independent": "独立作答", "peer_review": "互评修正", "guided": "按 Leader 指导讨论"}


async def cowork_flow(question: str, config: Config, show_workers: bool, leader: AgentSpec) -> int:
    """终端前端（Co-work 模式）：逐轮显示讨论进度、Leader 总结与延长讨论。"""
    workers = config.enabled_workers
    styles = {w.name: agent_style(i) for i, w in enumerate(workers)}
    extend = (f"，确信度低时最多延长至 {config.cowork_max_rounds} 轮" if config.cowork_extend
              and config.cowork_max_rounds > config.cowork_rounds else "")
    console.print(f"[bold]Co-work 模式[/] · {len(workers)} 位 Workers · {config.cowork_rounds} 轮圆桌讨论{extend} → {leader.name}")
    status: Optional[Status] = None

    def stop_status() -> None:
        nonlocal status
        if status:
            status.stop()
            status = None

    def on_event(kind: str, p: dict) -> None:
        nonlocal status
        if kind == "round_start":
            console.rule(f"[bold]Round {p['round']}/{p['total']} · {PHASE_NAMES[p['phase']]}")
            status = console.status(f"[bold cyan]Round {p['round']}/{p['total']} · Workers 正在作答...", spinner="dots")
            status.start()
        elif kind == "worker_done":
            r: AgentResult = p["result"]
            name = f"[bold {styles.get(r.name, 'white')}]{r.name:<8}[/]"
            if r.ok:
                console.print(f"  {name} [green]✓[/] R{p['round']} [dim]{r.elapsed:.1f}s · {len(r.output)} 字符[/]")
            else:
                print_failure(r)
                console.print(f"  {name} [red]✗ 退出后续讨论[/]")
        elif kind == "round_done":
            stop_status()
        elif kind == "leader_start":
            console.rule(f"[bold]Leader（{p['spec'].name}）总结第 {p['round']} 轮后的 {p['n_answers']} 份答案")
            status = console.status(f"[bold yellow]{p['spec'].name} 正在总结...", spinner="dots")
            status.start()
        elif kind == "leader_done":
            stop_status()
            r = p["result"]
            if r.ok:
                conf = r.extra.get("confidence")
                conf_txt = f"[{CONFIDENCE_STYLES[conf]}]{conf}[/]" if conf else "[dim]未能解析[/]"
                console.print(f"[bold yellow]{r.name}[/] [green]✓ 完成[/] [dim]({r.elapsed:.1f}s)[/]  确信度：{conf_txt}")
            else:
                print_failure(r)
        elif kind == "extension":
            console.print(Panel(Markdown(p["guidance"]), title=f"确信度低 → 争议指导意见（进入 Round {p['round']}）",
                                title_align="left", border_style="red"))

    try:
        outcome = await run_cowork_loop(question, workers, leader, on_event, rounds=config.cowork_rounds,
                                        max_rounds=config.cowork_max_rounds, extend_on_low=config.cowork_extend)
    finally:
        stop_status()

    if show_workers and outcome.ok_results:
        console.rule("[bold]Workers 最后一轮的答案")
        render_worker_answers(outcome.ok_results, workers)
    if outcome.code == EXIT_NO_WORKERS:
        console.print(Panel("所有 Worker 均失败，讨论无法进行。", border_style="bold red", style="red"))
    elif outcome.code == EXIT_LEADER_FAILED:
        console.print(Panel("Leader 总结失败，以上为 Workers 最后一轮的答案（未经总结）。", border_style="bold red", style="red"))
    else:
        render_verdict(outcome.verdict)

    table = Table(title=f"运行摘要（{len(outcome.rounds)} 轮讨论）", title_justify="left", show_edge=False, header_style="bold")
    table.add_column("Agent")
    for n in range(1, len(outcome.rounds) + 1):
        table.add_column(f"R{n}", justify="center")
    table.add_column("状态")
    for w in workers:
        cells = []
        for rnd in outcome.rounds:
            r = next((x for x in rnd if x.name == w.name), None)
            cells.append("" if r is None else ("[green]✓[/]" if r.ok else "[red]✗[/]"))
        last = next((x for x in reversed(outcome.results) if x.name == w.name), None)
        state = Text("✓ 坚持到最后", style="green") if last and last.ok and len(cells) and cells[-1] else \
            Text(f"✗ {last.error_summary[:40]}" if last and not last.ok else "—", style="red")
        table.add_row(w.name, *cells, state)
    console.print(table)
    return outcome.code


async def main_flow(question: str, config: Config, show_workers: bool = True,
                    leader: Optional[AgentSpec] = None) -> int:
    """终端前端。返回进程退出码：0 成功；1 所有 Worker 失败；2 Leader 仲裁失败。"""
    workers = config.enabled_workers
    leader = leader or config.leader
    if config.mode == "cowork" and workers:
        console.print(Panel(Text(question), title="[bold]问题", title_align="left", border_style="cyan"))
        return await cowork_flow(question, config, show_workers, leader)
    console.print(Panel(Text(question), title="[bold]问题", title_align="left", border_style="cyan"))
    if not workers:
        console.print(Panel("config.json 中没有启用的 Worker（enabled 全为 false）。",
                            border_style="bold red", style="red"))
        return EXIT_NO_WORKERS

    console.rule(f"[bold]阶段 1 · Workers 并发作答（{len(workers)} 个）")
    progress = Progress(
        SpinnerColumn(finished_text=" "),
        TextColumn("{task.fields[label]}"),
        TextColumn("{task.description}"),
        TimeElapsedColumn(),
        console=console,
    )
    tasks: dict[int, TaskID] = {}
    status: Optional[Status] = None

    def on_event(kind: str, p: dict) -> None:
        nonlocal status
        if kind == "worker_start":
            tasks[p["index"]] = progress.add_task(
                "正在思考...", total=1, label=f"[bold {agent_style(p['index'])}]{p['spec'].name:<8}[/]")
        elif kind == "worker_done":
            r: AgentResult = p["result"]
            desc = f"[green]✓ 完成[/]  [dim]{len(r.output)} 字符[/]" if r.ok else "[bold red]✗ 失败[/]"
            progress.update(tasks[p["index"]], description=desc, completed=1)
            if not r.ok:
                print_failure(r, progress.console)
        elif kind == "workers_finished":
            progress.stop()
            ok = p["ok"]
            if show_workers and ok:
                console.rule("[bold]Workers 原始回答")
                render_worker_answers(ok, workers)
            if len(ok) == 1:
                console.print(f"[yellow]⚠ 仅 {ok[0].name} 成功作答，无法做交叉一致性比较，仍交由 Leader 评审其准确性[/]")
        elif kind in ("leader_start", "review_start"):
            if kind == "leader_start":
                console.rule(f"[bold]阶段 2 · Leader（{p['spec'].name}）综合仲裁")
                msg = f"{p['spec'].name} 正在仲裁 {p['n_answers']} 份回答..."
            else:
                who = (f"无其他可用模型，仍由 {p['spec'].name} 复审" if p["fallback"]
                       else f"改由异构模型 {p['spec'].name}（{p['spec'].vendor}）复审")
                console.rule(f"[bold red]阶段 3 · 确信度低 → Secondary Review：{who}")
                msg = f"{p['spec'].name} 正在二次复审..."
            status = console.status(f"[bold yellow]{msg}", spinner="dots")
            status.start()
        elif kind in ("leader_done", "review_done"):
            if status:
                status.stop()
            r = p["result"]
            if r.ok:
                conf = r.extra["confidence"]
                conf_txt = f"[{CONFIDENCE_STYLES[conf]}]{conf}[/]" if conf else "[dim]未能解析[/]"
                console.print(f"[bold yellow]{r.name}[/] [green]✓ 完成[/] [dim]({r.elapsed:.1f}s)[/]  确信度：{conf_txt}")
            else:
                print_failure(r)
                if kind == "review_done":
                    console.print("[red]复审失败，保留首轮裁决[/]")

    console.print("[dim]正在等待各个 Agent 响应...[/]")
    progress.start()
    try:
        outcome = await orchestrate(question, workers, leader, config.review_on_low_confidence, on_event,
                                    reviewer_pool=config.leaders)
    finally:
        progress.stop()
        if status:
            status.stop()

    if outcome.code == EXIT_NO_WORKERS:
        console.print(Panel("所有 Worker 均失败，无法进行仲裁。", border_style="bold red", style="red"))
        render_summary(outcome.results, None)
    elif outcome.code == EXIT_LEADER_FAILED:
        console.print(Panel("Leader 仲裁失败，以下为 Workers 的原始回答（未经仲裁）。",
                            border_style="bold red", style="red"))
        if not show_workers:
            render_worker_answers(outcome.ok_results, workers)
        render_summary(outcome.results, outcome.verdict)
    else:
        render_verdict(outcome.verdict)
        render_summary(outcome.results, outcome.verdict)
    return outcome.code
