"""账户与配额（Account & Quota）—— 非侵入式地获取各 CLI 的登录状态与用量。

    Anthropic / Claude   账户：`claude auth status`（不消耗额度）
                         用量：仅在用户点击刷新时发送一条极小请求（haiku），从 stream-json 的
                               rate_limit_event 中读取 5 小时 / 7 天窗口；结果缓存到 data/quota_cache.json
    OpenAI / Codex       账户：`codex login status`
                         用量：读取 ~/.codex/sessions 下最新会话日志中的 rate_limits（不发请求）
    Google / agy         状态不透明：仅验证 CLI 可用（`agy models` 能列出模型即视为已登录）

不读取任何凭据文件（如 ~/.codex/auth.json）。
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import re
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .i18n import t
from .paths import DATA_DIR

CACHE_PATH = DATA_DIR / "quota_cache.json"
CODEX_SESSIONS = Path.home() / ".codex" / "sessions"

CODEX_WINDOW_KEYS = {300: "five_hour", 10080: "seven_day"}  # window_minutes → 统一的窗口 key


@dataclass
class QuotaWindow:
    key: str                           # five_hour / seven_day / seven_day_opus ...
    used_percent: Optional[float]      # 0–100
    resets_at: Optional[float] = None  # epoch 秒

    @property
    def label(self) -> str:
        text = t(f"win.{self.key}")
        return self.key if text == f"win.{self.key}" else text

    @property
    def expired(self) -> bool:
        """窗口已过重置时间：日志里的百分比已经过时。"""
        return self.resets_at is not None and self.resets_at <= time.time()

    @property
    def limited(self) -> bool:
        return not self.expired and self.used_percent is not None and self.used_percent >= 100


@dataclass
class ProviderStatus:
    provider: str                       # anthropic / openai / google
    title: str
    cli: str
    installed: bool = False
    logged_in: Optional[bool] = None    # None = 未知
    account: str = ""
    plan: str = ""
    status_text: str = ""
    windows: list[QuotaWindow] = field(default_factory=list)
    limited_until: Optional[float] = None
    source: str = ""                    # 用量数据来源说明
    error: str = ""
    fetched_at: float = field(default_factory=time.time)

    @property
    def limited(self) -> bool:
        if self.limited_until and self.limited_until > time.time():
            return True
        return any(w.limited for w in self.windows)

    @property
    def limit_reset(self) -> Optional[float]:
        """限额解除时间：显式的 limited_until 或已满窗口的重置时间。"""
        if self.limited_until and self.limited_until > time.time():
            return self.limited_until
        times = [w.resets_at for w in self.windows if w.limited and w.resets_at]
        return max(times) if times else None


PROVIDERS = {
    "anthropic": ("Anthropic · Claude", "claude"),
    "openai": ("OpenAI · Codex", "codex"),
    "google": ("Google · Gemini (agy)", "agy"),
}

LOGIN_COMMANDS = {
    # claude 必须去掉 API Key 才会走 claude.ai 订阅登录
    "anthropic": "unset ANTHROPIC_API_KEY; claude auth login",
    "openai": "codex login",
    # agy 没有独立的 login 子命令，进入交互模式后按提示登录
    "google": "agy",
}


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


async def _run(argv: list[str], env_unset: tuple[str, ...] = (), timeout: float = 30) -> tuple[int, str, str]:
    env = {k: v for k, v in os.environ.items() if k not in env_unset}
    proc = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.DEVNULL, env=env,
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return -1, "", t("acct.timeout", s=f"{timeout:.0f}")
    except asyncio.CancelledError:
        if proc.returncode is None:
            proc.kill()
        raise
    return proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


_RETRY_AT = re.compile(r"try again at\s+(\d{1,2}):(\d{2})\s*([AP]M)?", re.IGNORECASE)


def parse_limit_message(text: str, now: Optional[dt.datetime] = None) -> Optional[float]:
    """从 "...hit your usage limit ... try again at 8:51 PM." 中解析出解除时间（epoch）。"""
    if "limit" not in text.lower():
        return None
    m = _RETRY_AT.search(text)
    if not m:
        return None
    hour, minute, ampm = int(m.group(1)), int(m.group(2)), (m.group(3) or "").upper()
    if ampm == "PM" and hour != 12:
        hour += 12
    elif ampm == "AM" and hour == 12:
        hour = 0
    now = now or dt.datetime.now()
    t = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if t <= now:  # 已过今天这个时间 → 指的是明天
        t += dt.timedelta(days=1)
    return t.timestamp()


def fmt_reset(ts: Optional[float]) -> str:
    """重置时间的简短表示：今天显示 20:51，否则显示 10-08 20:51。"""
    if not ts:
        return ""
    t = dt.datetime.fromtimestamp(ts)
    return t.strftime("%H:%M") if t.date() == dt.date.today() else t.strftime("%m-%d %H:%M")


# ---------------------------------------------------------------------------
# Anthropic / Claude
# ---------------------------------------------------------------------------


def _load_cache() -> dict:
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_cache(key: str, value: dict) -> None:
    data = _load_cache()
    data[key] = value
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def save_claude_rate_limit(info: dict) -> None:
    """记录 claude 返回的配额信息（常驻进程每次回答都会附带，见 engine.ClaudeStream）。"""
    _save_cache("anthropic", {"info": info, "fetched_at": time.time()})


def parse_claude_rate_limit(stream_json: str) -> Optional[dict]:
    """从 `claude -p --output-format stream-json --verbose` 的输出中取最后一条 rate_limit_event。"""
    info = None
    for line in stream_json.splitlines():
        if '"rate_limit_event"' not in line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if d.get("type") == "rate_limit_event":
            info = d.get("rate_limit_info") or info
    return info


def claude_windows(info: dict) -> list[QuotaWindow]:
    windows = []
    for key, w in (info.get("unifiedWindows") or {}).items():
        util = w.get("utilization")
        windows.append(QuotaWindow(key, None if util is None else round(util * 100, 1), w.get("resetsAt")))
    return windows


async def fetch_claude(with_usage: bool = False) -> ProviderStatus:
    title, cli = PROVIDERS["anthropic"]
    st = ProviderStatus("anthropic", title, cli, installed=shutil.which(cli) is not None)
    if not st.installed:
        st.status_text = t("acct.not_installed")
        return st

    rc, out, err = await _run([cli, "auth", "status"], env_unset=("ANTHROPIC_API_KEY",))
    try:
        d = json.loads(out)
        st.logged_in = bool(d.get("loggedIn"))
        st.account = d.get("email") or ""
        st.plan = (d.get("subscriptionType") or "").capitalize()
        if d.get("authMethod") and d.get("authMethod") != "claude.ai":
            st.plan = f"{st.plan} · {d['authMethod']}".strip(" ·")
    except ValueError:
        st.logged_in = False if rc != 0 else None
        st.error = (err or out).strip()[:300]
    st.status_text = t("acct.logged_in") if st.logged_in else t("acct.logged_out")

    if with_usage and st.logged_in:
        rc, out, err = await _run([cli, "--strict-mcp-config", "--model", "haiku", "-p", "Reply with: ok",
                                   "--output-format", "stream-json", "--verbose"],
                                  env_unset=("ANTHROPIC_API_KEY",), timeout=90)
        info = parse_claude_rate_limit(out)
        if info:
            _save_cache("anthropic", {"info": info, "fetched_at": time.time()})
        else:
            st.error = t("acct.usage_failed", e=(err or out).strip()[:200] or "no rate_limit_event")

    cached = _load_cache().get("anthropic")
    if cached:
        info = cached["info"]
        st.windows = claude_windows(info)
        if info.get("status") == "rejected" and info.get("resetsAt"):
            st.limited_until = info["resetsAt"]
        st.source = t("acct.src_live", t=time.strftime('%m-%d %H:%M', time.localtime(cached['fetched_at'])))
    else:
        st.source = t("acct.src_click")
    return st


# ---------------------------------------------------------------------------
# OpenAI / Codex
# ---------------------------------------------------------------------------


def _find_rate_limits(obj) -> Optional[dict]:
    if isinstance(obj, dict):
        rl = obj.get("rate_limits")
        if isinstance(rl, dict):
            return rl
        for v in obj.values():
            found = _find_rate_limits(v)
            if found:
                return found
    return None


def latest_codex_rate_limits(sessions_dir: Path = CODEX_SESSIONS, max_files: int = 30) -> Optional[tuple[dict, float]]:
    """返回 (rate_limits, 记录时间 epoch)。只解析含 rate_limits 的行，不读取对话内容。"""
    try:
        files = sorted(sessions_dir.rglob("rollout-*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return None
    for f in files[:max_files]:
        found = None
        try:
            with f.open(encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if '"rate_limits"' not in line:
                        continue
                    try:
                        d = json.loads(line)
                    except ValueError:
                        continue
                    rl = _find_rate_limits(d)
                    if rl and (rl.get("primary") or rl.get("secondary")):
                        found = (rl, d.get("timestamp"))
        except OSError:
            continue
        if found:
            rl, ts = found
            try:
                when = dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
            except ValueError:
                when = f.stat().st_mtime
            return rl, when
    return None


def codex_windows(rl: dict) -> list[QuotaWindow]:
    windows = []
    for key in ("primary", "secondary"):
        w = rl.get(key)
        if not w:
            continue
        wkey = CODEX_WINDOW_KEYS.get(w.get("window_minutes"), key)
        windows.append(QuotaWindow(wkey, w.get("used_percent"), w.get("resets_at")))
    return windows


async def fetch_codex(sessions_dir: Path = CODEX_SESSIONS) -> ProviderStatus:
    title, cli = PROVIDERS["openai"]
    st = ProviderStatus("openai", title, cli, installed=shutil.which(cli) is not None)
    if not st.installed:
        st.status_text = t("acct.not_installed")
        return st

    rc, out, err = await _run([cli, "login", "status"])
    text = (out + err).strip()
    st.logged_in = rc == 0 and "logged in" in text.lower()
    st.account = text.splitlines()[0] if text else ""
    if st.account.lower().startswith("logged in using "):
        st.account = t("acct.codex_logged", how=st.account[len("logged in using "):])
    st.status_text = t("acct.logged_in") if st.logged_in else t("acct.logged_out")

    found = await asyncio.get_running_loop().run_in_executor(None, latest_codex_rate_limits, sessions_dir)
    if found:
        rl, when = found
        st.windows = codex_windows(rl)
        st.plan = (rl.get("plan_type") or "").capitalize()
        if rl.get("rate_limit_reached_type"):
            st.limited_until = max((w.resets_at or 0) for w in st.windows) or None
        st.source = t("acct.src_logs", t=time.strftime('%m-%d %H:%M', time.localtime(when)))
    else:
        st.source = t("acct.src_no_logs")
    return st


# ---------------------------------------------------------------------------
# Google / agy
# ---------------------------------------------------------------------------


async def fetch_agy() -> ProviderStatus:
    title, cli = PROVIDERS["google"]
    st = ProviderStatus("google", title, cli, installed=shutil.which(cli) is not None)
    if not st.installed:
        st.status_text = t("acct.not_installed")
        return st
    rc, out, err = await _run([cli, "models"], timeout=45)
    models = [ln for ln in out.splitlines() if "\t" in ln]
    if rc == 0 and models and not out.lower().startswith("error"):
        st.logged_in = True
        st.status_text = t("acct.active")
        st.account = t("acct.agy_ok", n=len(models))
    else:
        st.logged_in = False
        st.status_text = t("acct.unavailable")
        st.error = (err or out).strip()[:300]
    st.source = t("acct.src_agy")
    return st


FETCHERS = {"anthropic": fetch_claude, "openai": fetch_codex, "google": fetch_agy}


# —— 运行中观察到的限额（例如 Worker 报 "hit your usage limit ... try again at 8:51 PM"）——
# 本地日志可能滞后（Codex 只在交互式会话中写日志），因此把运行时看到的限额也记下来，重启后依然有效。


def record_limit(provider: str, until: float, message: str = "") -> None:
    _save_cache(f"{provider}_limit", {"until": until, "message": message[:300], "noted_at": time.time()})


def cached_limit(provider: str) -> Optional[dict]:
    v = _load_cache().get(f"{provider}_limit")
    return v if v and v.get("until", 0) > time.time() else None


async def fetch(provider: str, **kwargs) -> ProviderStatus:
    """获取某个厂商的状态，并合并运行中观察到的限额。"""
    st = await FETCHERS[provider](**kwargs)
    lim = cached_limit(provider)
    if lim:
        st.limited_until = max(st.limited_until or 0, lim["until"])
    return st


# ---------------------------------------------------------------------------
# 登录 / 切换账户：在系统终端中执行登录命令
# ---------------------------------------------------------------------------


def write_login_script(provider: str, directory: Path = DATA_DIR / "login") -> Path:
    """生成 .command 脚本。用 `open -a Terminal x.command` 打开，无需 AppleScript 自动化权限。"""
    title, _ = PROVIDERS[provider]
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"login_{provider}.command"
    path.write_text(
        "#!/bin/zsh -l\n"
        f"echo {shlex.quote('▶ ' + t('acct.script_title', title=title))}\n"
        f"echo {shlex.quote('  ' + t('acct.script_run', cmd=LOGIN_COMMANDS[provider]))}\n"
        "echo\n"
        f"{LOGIN_COMMANDS[provider]}\n"
        "echo\n"
        f"echo {shlex.quote(t('acct.script_done'))}\n"
        "exec $SHELL -l\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def preferred_terminal() -> str:
    """优先使用 iTerm（若已安装），否则使用系统自带的 Terminal。"""
    for app in ("/Applications/iTerm.app", str(Path.home() / "Applications/iTerm.app")):
        if Path(app).exists():
            return "iTerm"
    return "Terminal"


def open_login_terminal(provider: str, terminal: Optional[str] = None) -> tuple[list[str], Path]:
    script = write_login_script(provider)
    argv = ["open", "-a", terminal or preferred_terminal(), str(script)]
    launch(argv)
    return argv, script


def launch(argv: list[str]) -> None:
    subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

