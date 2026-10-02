"""Result type shared by every model call, plus the registry of running CLI processes."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field

from .i18n import t

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
            return t("row.failed")
        for ln in reversed(lines):
            if _ERROR_LINE.search(ln):
                return ln
        return lines[-1]


# Every CLI subprocess that is currently running (one-shot or pooled), so the app can kill them on exit.
RUNNING_PROCS: set[asyncio.subprocess.Process] = set()
