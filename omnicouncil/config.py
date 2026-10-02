"""配置管理：从 config.json 读取 Workers / Leaders，文件不存在时自动生成默认模板。

命令模板是参数数组，"{prompt}" 会被替换为实际 prompt。
每个参数是独立的 argv 元素，不经过 shell，因此 prompt 中的引号/特殊字符是安全的。

结构：
    workers            Worker 列表，每项可用 "enabled": false 临时停用
    leaders            可选的 Leader 列表（GUI 下拉框 / CLI --leader 从中选择）
    default_leader     默认使用的 Leader 名称
    review_on_low_confidence   确信度为「低」时，是否从 leaders 中另选一个异构模型二次复审
    hotkey             GUI 全局唤醒快捷键，如 "cmd+shift+j"、"option+space"
    language           GUI 界面语言："en"（默认）或 "zh"
    mode               协作模式："judge"（一发多收 + 裁判，默认）或 "cowork"（多轮圆桌讨论 + Leader 总结）
    cowork_rounds      Co-work 模式的基础讨论轮数（默认 3）
    cowork_max_rounds  Leader 判定确信度低时，讨论最多延长到的轮数（默认 4）
    cowork_extend      Co-work 模式下确信度低时是否延长讨论

每个 Agent 可选字段：
    vendor   厂商（anthropic / google / openai ...），缺省时按命令名推断；复审优先选不同厂商
    tier     模型档位，"pro" 表示高阶模型；复审优先选 pro 档

Worker 的模型选择（缺失时自动迁移，见 migrate_config）：
    available_models   该 CLI 可选的模型列表（GUI 下拉框的选项）
    selected_model     当前选中的模型；空字符串表示使用 CLI 自己的默认模型（不注入参数）
    model_flag         指定模型的参数，如 claude / agy 用 "--model"，codex 用 "-m"

Leader 的多模态前置解析（见 engine.preprocess_multimodal）：
    file_flag    把附件交给 CLI 的参数。claude / agy 没有 --file，而是用 "--add-dir" 授权目录、在 prompt 中给出路径；
                 codex 用 "--image="（以 "=" 结尾表示与路径拼成一个参数）
    file_arg     "dir"（传附件所在目录，默认）或 "file"（每个文件传一次）
    file_tools   解析时临时启用的工具（覆盖 --tools ""），如 claude 需要 "Read" 才能读取图片 / PDF
    file_types   能解析的附件类型：image / pdf / audio / text

常驻进程（Warm Pool，见 engine.AgentPool）：
    is_persistent          是否以常驻进程运行（stdin 发问 / stdout 流式读取），省掉每次冷启动；
                           仅对支持流式协议的 CLI 生效（claude、agy），其余自动回退为单次调用
    max_turns_per_process  常驻进程处理多少次请求后重建（无法清空上下文的 CLI 用来限制上下文累积）

兼容旧格式：只有单个 "leader" 对象时视为 leaders = [leader]。
"""

from __future__ import annotations

import json
import os
import shutil
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .paths import CONFIG_PATH as DEFAULT_CONFIG_PATH, TEMPLATE_PATH  # noqa: E402  首次启动时把模板复制为 config.json
PROMPT_PLACEHOLDER = "{prompt}"

DEFAULT_CONFIG: dict = {
    "workers": [
        {
            "name": "Claude",
            # --tools ""：禁用全部内置工具（读写文件、记忆等），Worker 只做纯推理
            "command": ["claude", "--strict-mcp-config", "--tools", "", "-p", PROMPT_PLACEHOLDER],
            "timeout": 300,
            # 去掉 API Key，让 claude 走 claude.ai 订阅登录而不是按量计费
            "env_unset": ["ANTHROPIC_API_KEY"],
            "enabled": True,
            "model_flag": "--model",
            "available_models": ["haiku", "sonnet", "opus"],
            "selected_model": "",
            "is_persistent": True,
        },
        {
            # Gemini 走 Antigravity CLI（gemini CLI 个人免费档已停止支持）。
            # 用 --sandbox（限制终端操作）而不是 --mode plan：Pro 模型在 plan 模式下只会提交计划等待批准，不直接回答
            "name": "Gemini",
            "command": ["agy", "--sandbox", "-p", PROMPT_PLACEHOLDER],
            "timeout": 300,
            "enabled": True,
            "model_flag": "--model",
            "available_models": ["gemini-3.8-flash-low", "gemini-3.8-flash-medium", "gemini-3.1-pro-high"],
            "selected_model": "",
            "is_persistent": False,  # agy 常驻模式无法清空上下文；开启后每 max_turns_per_process 次请求重建进程
        },
        {
            "name": "Codex",
            "command": ["codex", "exec", "--sandbox", "read-only", "--skip-git-repo-check",
                        "--ephemeral", "--color", "never", PROMPT_PLACEHOLDER],
            "timeout": 300,
            "env_unset": ["OPENAI_API_KEY"],
            "enabled": True,
            "model_flag": "-m",
            "available_models": ["gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol", "gpt-6-astra"],
            "selected_model": "",
            "is_persistent": False,  # codex 暂无稳定的常驻协议
        },
    ],
    "leaders": [
        {
            "name": "Claude Opus",
            "tier": "pro",
            "command": ["claude", "--strict-mcp-config", "--tools", "", "--model", "opus", "-p", PROMPT_PLACEHOLDER],
            "timeout": 600,
            "env_unset": ["ANTHROPIC_API_KEY"],
            "is_persistent": True,
        },
        {
            "name": "Gemini 3.1 Pro",
            "tier": "pro",
            "command": ["agy", "--sandbox", "--model", "gemini-3.1-pro-high", "-p", PROMPT_PLACEHOLDER],
            "timeout": 600,
        },
        {
            "name": "Codex",
            "command": ["codex", "exec", "--sandbox", "read-only", "--skip-git-repo-check",
                        "--ephemeral", "--color", "never", PROMPT_PLACEHOLDER],
            "timeout": 600,
            "env_unset": ["OPENAI_API_KEY"],
        },
    ],
    "default_leader": "Claude Opus",
    "review_on_low_confidence": True,
    "hotkey": "cmd+shift+j",
    "language": "en",
    "mode": "judge",
    "cowork_rounds": 3,
    "cowork_max_rounds": 4,
    "cowork_extend": True,
}

MODES = ("judge", "cowork")

VENDOR_BY_EXECUTABLE = {"claude": "anthropic", "agy": "google", "gemini": "google", "codex": "openai"}

# 旧配置迁移时注入的默认模型选项（快速 → 均衡 → 强大）
DEFAULT_MODEL_OPTIONS = {
    "claude": ("--model", ["haiku", "sonnet", "opus"]),
    "agy": ("--model", ["gemini-3.8-flash-low", "gemini-3.8-flash-medium", "gemini-3.1-pro-high"]),
    "gemini": ("--model", ["gemini-2.5-flash-lite", "gemini-2.5-flash", "gemini-2.5-pro"]),
    "codex": ("-m", ["gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol", "gpt-6-astra"]),
}
PROMPT_FLAGS = ("-p", "--print", "--prompt")  # 紧挨着 prompt、以 prompt 为值的参数

# 旧配置迁移时 is_persistent 的默认值：
#   claude  支持 stream-json 常驻模式，且可用 /clear 在任务之间清空上下文 → 开启
#   agy     支持 stream-json 常驻模式，但 print 模式不支持 /clear（上下文会累积）→ 默认关闭，可手动开启
#   codex   只有实验性的 app-server，暂不支持 → 关闭（单次调用）
DEFAULT_PERSISTENT = {"claude": True}

# 旧配置迁移时注入的 Leader 附件解析能力（均经实测：claude 读不了音频，agy/Gemini 可转录音频，codex 只支持图片）
DEFAULT_FILE_ACCESS = {
    "claude": {"file_flag": "--add-dir", "file_arg": "dir", "file_tools": "Read", "file_types": ["image", "pdf", "text"]},
    "agy": {"file_flag": "--add-dir", "file_arg": "dir", "file_tools": "", "file_types": ["image", "pdf", "text", "audio"]},
    "codex": {"file_flag": "--image=", "file_arg": "file", "file_tools": "", "file_types": ["image"]},
}
FILE_TYPES = ("image", "pdf", "audio", "text")
DEFAULT_HOTKEY = "cmd+shift+j"


class ConfigError(Exception):
    pass


@dataclass
class AgentSpec:
    name: str
    command: list[str]
    timeout: float = 300.0
    env_unset: list[str] = field(default_factory=list)  # 子进程中要移除的环境变量
    enabled: bool = True
    vendor: str = ""
    tier: str = ""
    available_models: list[str] = field(default_factory=list)
    selected_model: str = ""
    model_flag: str = ""
    is_persistent: bool = False
    max_turns_per_process: int = 20
    file_flag: str = ""
    file_arg: str = "dir"
    file_tools: str = ""
    file_types: list[str] = field(default_factory=list)

    def can_read(self, kind: str) -> bool:
        return bool(self.file_flag) and kind in self.file_types

    def __post_init__(self) -> None:
        if not self.vendor:
            exe = Path(self.command[0]).name if self.command else ""
            self.vendor = VENDOR_BY_EXECUTABLE.get(exe, exe)

    @property
    def installed(self) -> bool:
        return shutil.which(self.command[0]) is not None

    def env(self) -> dict[str, str] | None:
        if not self.env_unset:
            return None  # 继承当前环境
        return {k: v for k, v in os.environ.items() if k not in self.env_unset}

    def argv_template(self) -> list[str]:
        """注入模型参数后的命令模板（仍含 {prompt} 占位符）。

        模型参数插在 prompt 之前；若 prompt 前面是 -p / --print 这类以 prompt 为值的参数，
        则插在该参数之前，避免把 prompt 和它的参数拆开。命令里已有同名参数时替换其值而不重复。"""
        cmd = list(self.command)
        if not (self.selected_model and self.model_flag):
            return cmd
        if self.model_flag in cmd:
            i = cmd.index(self.model_flag)
            if i + 1 < len(cmd) and PROMPT_PLACEHOLDER not in cmd[i + 1]:
                cmd[i + 1] = self.selected_model
                return cmd
        if PROMPT_PLACEHOLDER not in cmd:
            # prompt 嵌在脚本字符串里（如 sh -c "... {prompt}"）：无法确定安全的注入位置，不注入
            return cmd
        idx = cmd.index(PROMPT_PLACEHOLDER)
        if idx > 1 and cmd[idx - 1] in PROMPT_FLAGS:
            idx -= 1
        return cmd[:idx] + [self.model_flag, self.selected_model] + cmd[idx:]

    def build(self, prompt: str) -> list[str]:
        return [a.replace(PROMPT_PLACEHOLDER, prompt) for a in self.argv_template()]


@dataclass
class Config:
    workers: list[AgentSpec]
    leaders: list[AgentSpec]
    default_leader: str
    review_on_low_confidence: bool = True
    hotkey: str = DEFAULT_HOTKEY
    language: str = "en"
    mode: str = "judge"
    cowork_rounds: int = 3
    cowork_max_rounds: int = 4
    cowork_extend: bool = True
    path: Path = field(default=DEFAULT_CONFIG_PATH)

    @property
    def enabled_workers(self) -> list[AgentSpec]:
        return [w for w in self.workers if w.enabled]

    @property
    def leader(self) -> AgentSpec:
        return self.get_leader(self.default_leader)

    def get_leader(self, name: str) -> AgentSpec:
        for spec in self.leaders:
            if spec.name == name:
                return spec
        raise ConfigError(f"找不到名为「{name}」的 Leader，可选：{[s.name for s in self.leaders]}")


_write_lock = threading.Lock()  # 序列化「读 → 改 → 写」，避免并发保存互相覆盖


def write_raw_config(data: dict, path: Path = DEFAULT_CONFIG_PATH) -> None:
    """原子写入：先写同目录临时文件，再 os.replace，避免写到一半被读到或损坏。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def write_default_config(path: Path = DEFAULT_CONFIG_PATH) -> None:
    """写入默认配置：优先复制 config.template.json，模板缺失或损坏时使用内置的 DEFAULT_CONFIG。"""
    try:
        data = json.loads(TEMPLATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = DEFAULT_CONFIG
    write_raw_config(data, path)


def ensure_config(path: Path = DEFAULT_CONFIG_PATH) -> bool:
    """配置文件不存在时由模板生成。返回 True 表示本次新建了文件。"""
    if path.exists():
        return False
    write_default_config(path)
    return True


def read_raw_config(path: Path = DEFAULT_CONFIG_PATH) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ConfigError(f"{path} 不是合法的 JSON：第 {e.lineno} 行第 {e.colno} 列，{e.msg}") from e
    if not isinstance(data, dict):
        raise ConfigError(f"{path} 顶层必须是对象")
    return data


def _parse_agent(raw: object, where: str) -> AgentSpec:
    if not isinstance(raw, dict):
        raise ConfigError(f"{where} 必须是对象")
    name, command = raw.get("name"), raw.get("command")
    if not isinstance(name, str) or not name:
        raise ConfigError(f"{where}.name 必须是非空字符串")
    if not (isinstance(command, list) and command and all(isinstance(a, str) for a in command)):
        raise ConfigError(f"{where}.command 必须是非空的字符串数组")
    if not any(PROMPT_PLACEHOLDER in a for a in command):
        raise ConfigError(f"{where}.command 中缺少 {PROMPT_PLACEHOLDER} 占位符")
    timeout = raw.get("timeout", 300)
    if not isinstance(timeout, (int, float)) or timeout <= 0:
        raise ConfigError(f"{where}.timeout 必须是正数（秒）")
    env_unset = raw.get("env_unset", [])
    if not (isinstance(env_unset, list) and all(isinstance(k, str) for k in env_unset)):
        raise ConfigError(f"{where}.env_unset 必须是字符串数组")
    vendor, tier = raw.get("vendor", ""), raw.get("tier", "")
    if not isinstance(vendor, str) or not isinstance(tier, str):
        raise ConfigError(f"{where}.vendor / tier 必须是字符串")
    models = raw.get("available_models", [])
    if not (isinstance(models, list) and all(isinstance(m, str) and m for m in models)):
        raise ConfigError(f"{where}.available_models 必须是非空字符串数组")
    selected, flag = raw.get("selected_model", ""), raw.get("model_flag", "")
    if not isinstance(selected, str) or not isinstance(flag, str):
        raise ConfigError(f"{where}.selected_model / model_flag 必须是字符串")
    if selected and not flag:
        raise ConfigError(f"{where} 设置了 selected_model，但缺少 model_flag")
    max_turns = raw.get("max_turns_per_process", 20)
    if not isinstance(max_turns, int) or max_turns < 1:
        raise ConfigError(f"{where}.max_turns_per_process 必须是正整数")
    file_flag, file_arg, file_tools = raw.get("file_flag", ""), raw.get("file_arg", "dir"), raw.get("file_tools", "")
    file_types = raw.get("file_types", [])
    if not all(isinstance(x, str) for x in (file_flag, file_arg, file_tools)) or file_arg not in ("dir", "file"):
        raise ConfigError(f"{where}.file_flag / file_tools 必须是字符串，file_arg 只能是 dir 或 file")
    if not (isinstance(file_types, list) and all(x in FILE_TYPES for x in file_types)):
        raise ConfigError(f"{where}.file_types 只能包含 {', '.join(FILE_TYPES)}")
    return AgentSpec(name=name, command=command, timeout=float(timeout), env_unset=env_unset,
                     enabled=bool(raw.get("enabled", True)), vendor=vendor, tier=tier,
                     available_models=models, selected_model=selected, model_flag=flag,
                     is_persistent=bool(raw.get("is_persistent", False)), max_turns_per_process=max_turns,
                     file_flag=file_flag, file_arg=file_arg, file_tools=file_tools, file_types=file_types)


def _parse_agent_list(raw: object, where: str) -> list[AgentSpec]:
    if not isinstance(raw, list) or not raw:
        raise ConfigError(f"{where} 必须是非空数组")
    specs = [_parse_agent(a, f"{where}[{i}]") for i, a in enumerate(raw)]
    names = [s.name for s in specs]
    if len(set(names)) != len(names):
        raise ConfigError(f"{where} 中存在重名：{names}")
    return specs


def migrate_config(data: dict) -> bool:
    """为缺少模型选择字段的 Worker 注入默认值。返回 True 表示有改动（需要写回文件）。

    命令里已经写死的模型参数（如 agy 的 "--model gemini-3.8-flash-medium"）会被移出 command，
    成为 selected_model，以后统一由下拉框控制。"""
    changed = False
    for a in data.get("leaders", []):
        cmd = a.get("command") if isinstance(a, dict) else None
        if isinstance(cmd, list) and cmd and "file_flag" not in a:
            a.update(DEFAULT_FILE_ACCESS.get(Path(str(cmd[0])).name, {"file_flag": ""}))
            changed = True
    for a in data.get("workers", []) + data.get("leaders", []):
        # claude 默认带有读写文件 / 记忆等工具；作为 Worker / Leader 只需纯推理，统一禁用工具
        cmd = a.get("command") if isinstance(a, dict) else None
        if isinstance(cmd, list) and cmd and Path(str(cmd[0])).name == "claude" and "--tools" not in cmd:
            a["command"] = [cmd[0], "--tools", ""] + cmd[1:]
            changed = True
        if isinstance(a, dict) and isinstance(a.get("command"), list) and a["command"] and "is_persistent" not in a:
            a["is_persistent"] = DEFAULT_PERSISTENT.get(Path(str(a["command"][0])).name, False)
            changed = True
    for w in data.get("workers", []):
        if not isinstance(w, dict) or not isinstance(w.get("command"), list) or not w["command"]:
            continue
        exe = Path(str(w["command"][0])).name
        flag, models = DEFAULT_MODEL_OPTIONS.get(exe, ("", []))
        if "model_flag" not in w:
            w["model_flag"] = flag
            changed = True
        if "selected_model" not in w:
            selected = ""
            cmd, f = w["command"], w.get("model_flag")
            if f and f in cmd:
                i = cmd.index(f)
                if i + 1 < len(cmd) and PROMPT_PLACEHOLDER not in cmd[i + 1]:
                    selected = cmd[i + 1]
                    w["command"] = cmd[:i] + cmd[i + 2:]
            w["selected_model"] = selected
            changed = True
        if "available_models" not in w:
            opts = list(models)
            if w["selected_model"] and w["selected_model"] not in opts:
                opts.append(w["selected_model"])
            w["available_models"] = opts
            changed = True
    return changed


def save_worker_model(path: Path, worker: str, model: str) -> None:
    """只回写某个 Worker 的 selected_model，保留文件中其余内容不变。"""
    with _write_lock:
        data = read_raw_config(path)
        for w in data.get("workers", []):
            if w.get("name") == worker:
                w["selected_model"] = model
                break
        else:
            raise ConfigError(f"找不到名为「{worker}」的 Worker")
        write_raw_config(data, path)


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> Config:
    ensure_config(path)
    data = read_raw_config(path)
    if migrate_config(data):  # 旧配置：注入模型选择字段并写回
        write_raw_config(data, path)

    workers = _parse_agent_list(data.get("workers"), "workers")
    if "leaders" in data:
        leaders = _parse_agent_list(data["leaders"], "leaders")
    elif "leader" in data:  # 旧格式
        leaders = [_parse_agent(data["leader"], "leader")]
    else:
        raise ConfigError("缺少 leaders（或旧格式的 leader）配置")

    default_leader = data.get("default_leader", leaders[0].name)
    mode = data.get("mode", "judge")
    if mode not in MODES:
        raise ConfigError(f"mode 必须是 {' / '.join(MODES)} 之一，当前为 {mode!r}")
    rounds, max_rounds = data.get("cowork_rounds", 3), data.get("cowork_max_rounds", 4)
    if not (isinstance(rounds, int) and isinstance(max_rounds, int) and 1 <= rounds <= max_rounds <= 10):
        raise ConfigError("需满足 1 ≤ cowork_rounds ≤ cowork_max_rounds ≤ 10（整数）")
    cfg = Config(
        workers=workers,
        leaders=leaders,
        default_leader=default_leader,
        review_on_low_confidence=bool(data.get("review_on_low_confidence", True)),
        hotkey=str(data.get("hotkey", DEFAULT_HOTKEY)),
        language=str(data.get("language", "en")),
        mode=mode,
        cowork_rounds=rounds,
        cowork_max_rounds=max_rounds,
        cowork_extend=bool(data.get("cowork_extend", True)),
        path=path,
    )
    cfg.get_leader(default_leader)  # 校验 default_leader 存在
    return cfg


def save_selection(path: Path, enabled: dict[str, bool], default_leader: str, review_on_low: bool,
                   mode: Optional[str] = None, cowork_extend: Optional[bool] = None) -> None:
    """只回写 Worker 启用状态、默认 Leader、复审开关与协作模式，保留文件中其余内容不变。"""
    with _write_lock:
        data = read_raw_config(path)
        for w in data.get("workers", []):
            if w.get("name") in enabled:
                w["enabled"] = enabled[w["name"]]
        data["default_leader"] = default_leader
        data["review_on_low_confidence"] = review_on_low
        if mode is not None:
            data["mode"] = mode
        if cowork_extend is not None:
            data["cowork_extend"] = cowork_extend
        write_raw_config(data, path)


def save_language(path: Path, language: str) -> None:
    """只回写界面语言，保留文件中其余内容不变。"""
    with _write_lock:
        data = read_raw_config(path)
        data["language"] = language
        write_raw_config(data, path)


# 让内置默认值与迁移后的结构保持一致（新增字段只需在迁移逻辑中维护一处）
migrate_config(DEFAULT_CONFIG)
