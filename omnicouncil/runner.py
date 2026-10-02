"""Run one model call: through the warm pool when possible, otherwise as a one-shot subprocess."""

from __future__ import annotations

import asyncio
import logging
import shlex
import shutil
import time
from typing import Optional

from .agent import RUNNING_PROCS, AgentResult
from .config import AgentSpec
from .i18n import t
from .pool import POOL

log = logging.getLogger("omnicouncil.runner")


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
    for proc in list(RUNNING_PROCS):
        if proc.returncode is None:
            proc.kill()
            n += 1
    return n


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

    RUNNING_PROCS.add(proc)
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
        RUNNING_PROCS.discard(proc)

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
