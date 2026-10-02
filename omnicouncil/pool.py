"""Warm pool (persistent CLI processes).

常驻进程池（Warm Pool）

冷启动（Node 运行时、登录校验、配置 / 插件加载）往往占一次调用的大半时间。对支持流式协议的 CLI，
我们让进程常驻：stdin 每行写入一条 JSON 消息，stdout 每行读取一个 JSON 事件。

「回答完毕」判定：不解析 TUI 文本（交互式 REPL 需要真实终端，且输出夹杂颜色码 / 光标控制符），
而是使用 CLI 官方的结构化流协议中明确的结束事件（策略 B）：
  claude  --input-format stream-json --output-format stream-json → {"type": "result", ...}
  agy     --print= --input-format stream-json --output-format stream-json → {"event": "result", ...}
因此无需正则剥离 ANSI，也不依赖「N 秒无输出」这类时间窗口猜测；超时只作为兜底。

上下文隔离（按「运行」划分会话）：每次 orchestrate / run_cowork_loop 生成一个 session id。
  · 同一次运行内（如 Co-work 的第 1–4 轮、Leader 的多次总结）沿用同一个会话：后续轮次命中已初始化的会话
    与提示词缓存，最快（实测约 1s，冷启动约 5s）。
  · 运行结束后（end_session）在后台用 /clear 重置会话（不调用模型），利用用户思考的间隙完成重新初始化；
    不同问题之间绝不共享上下文。重置进行中到来的请求会等待重置完成，而不是回退冷启动。
  · 预热时也先 /clear 一次，提前完成会话初始化。
agy 的 print 模式不支持 /clear，只能每 max_turns_per_process 次请求重建进程（因此 agy 默认不开启常驻）。

生命周期：GUI 启动时预热（启动进程并完成会话初始化，不调用模型、不消耗额度）；模型 / 勾选变化时同步；
写入前检查进程是否存活，崩溃自动拉起（Auto-healing）；超时 / 被取消时杀掉进程，下次调用自动重建；
应用退出时关闭 stdin 并 terminate，必要时 kill。
"""

from __future__ import annotations

import asyncio
import json
import logging
import shlex
import shutil
import time
from pathlib import Path
from typing import Optional

from .agent import RUNNING_PROCS, AgentResult
from .config import PROMPT_FLAGS, PROMPT_PLACEHOLDER, AgentSpec
from .i18n import t

log = logging.getLogger("omnicouncil.pool")


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
        from . import accounts
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
        RUNNING_PROCS.add(self.proc)
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
            RUNNING_PROCS.discard(self.proc)

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
        RUNNING_PROCS.discard(proc)
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
