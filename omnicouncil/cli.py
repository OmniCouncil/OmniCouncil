#!/usr/bin/env python3
"""OmniCouncil CLI 入口。

    python main.py ask "你的问题"
    python main.py config            # 用编辑器打开 config.json
    python main.py config --show     # 在终端查看当前配置及 CLI 安装状态
"""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import List, Optional

import typer
from rich.logging import RichHandler
from rich.markdown import Markdown
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table

from .config import DEFAULT_CONFIG_PATH, ConfigError, ensure_config, load_config, write_default_config
from . import i18n
from .cli_render import console, main_flow
from .multimodal import preprocess_multimodal
from .pool import POOL

i18n.set_language("zh")  # 终端 CLI 固定使用中文

app = typer.Typer(
    help="OmniCouncil —— 多智能体协作与仲裁（Mixture of Agents），基于本地 AI CLI。",
    add_completion=False,
    no_args_is_help=True,
)

ConfigOption = typer.Option(DEFAULT_CONFIG_PATH, "--config", "-c", help="配置文件路径", dir_okay=False)


def _load(path: Path):
    if ensure_config(path):
        console.print(f"[yellow]未找到配置文件，已生成默认模板：[/]{path}")
    try:
        return load_config(path)
    except ConfigError as e:
        console.print(Panel(str(e), title=f"✗ 配置错误：{path.name}", title_align="left",
                            border_style="bold red", style="red"))
        raise typer.Exit(78)


@app.command()
def ask(
    question: List[str] = typer.Argument(..., help="要提问的问题（可不加引号，多个词会自动拼接）"),
    config_path: Path = ConfigOption,
    hide_workers: bool = typer.Option(False, "--hide-workers", "-q", help="不显示各 Worker 的原始回答"),
    no_review: bool = typer.Option(False, "--no-review", help="确信度低时不触发二次复审"),
    leader: Optional[str] = typer.Option(None, "--leader", "-l", help="本次使用的 Leader 名称（默认取 config.json 的 default_leader）"),
    mode: Optional[str] = typer.Option(None, "--mode", "-m", help="协作模式：judge（一发多收 + 裁判）或 cowork（多轮圆桌讨论）；默认取 config.json"),
    rounds: Optional[int] = typer.Option(None, "--rounds", "-r", help="Co-work 模式的讨论轮数（默认取 config.json 的 cowork_rounds）"),
    model: Optional[List[str]] = typer.Option(None, "--model", help="临时覆盖某个 Worker 的模型，如 --model Claude=haiku（可重复）"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="打印实际执行的 CLI 命令（含注入的模型参数）"),
    files: Optional[List[Path]] = typer.Option(None, "--file", "-f", exists=True, dir_okay=False,
                                               help="附件（图片 / PDF / 文本 / 音频，可重复）；由 Leader 先行解析为文字"),
) -> None:
    """把问题并发发给所有 Workers，再交由 Leader 综合仲裁。"""
    text = " ".join(question).strip()
    if not text and not files:
        console.print("[red]问题不能为空[/]")
        raise typer.Exit(64)
    cfg = _load(config_path)
    if no_review:
        cfg.review_on_low_confidence = False
    if mode:
        if mode not in ("judge", "cowork"):
            console.print("[bold red]✗ --mode 只能是 judge 或 cowork[/]")
            raise typer.Exit(64)
        cfg.mode = mode
    for item in model or []:
        name, sep, value = item.partition("=")
        spec = next((w for w in cfg.workers if w.name == name), None)
        if not sep or spec is None:
            console.print(f"[bold red]✗ --model 格式应为 Worker=模型，且 Worker 需存在于配置中：{item!r}[/]")
            raise typer.Exit(64)
        if value and not spec.model_flag:
            console.print(f"[bold red]✗ {name} 没有配置 model_flag，无法指定模型[/]")
            raise typer.Exit(64)
        spec.selected_model = value
    if verbose:
        logging.basicConfig(level=logging.INFO, format="[dim]%(message)s[/]", handlers=[RichHandler(
            console=console, show_time=False, show_level=False, show_path=False, markup=True)])
    if rounds is not None:
        if rounds < 1:
            console.print("[bold red]✗ --rounds 至少为 1[/]")
            raise typer.Exit(64)
        cfg.cowork_rounds = rounds
        cfg.cowork_max_rounds = max(cfg.cowork_max_rounds, rounds)
    try:
        leader_spec = cfg.get_leader(leader) if leader else cfg.leader
    except ConfigError as e:
        console.print(f"[bold red]✗ {e}[/]")
        raise typer.Exit(64)
    try:
        async def run() -> int:
            try:
                prompt = text
                if files:
                    with console.status(f"[bold cyan]{leader_spec.name} 正在解析 {len(files)} 个附件...", spinner="dots"):
                        prompt, recs = await preprocess_multimodal(text, [str(f) for f in files], leader_spec, cfg.leaders)
                    for rec in recs:
                        state = "[green]✓[/]" if rec["ok"] else "[red]✗[/]"
                        console.print(f"  📎 {', '.join(rec['files'])} → {rec['processor'] or '—'} {state} [dim]{rec['elapsed']}s[/]")
                    console.print(Panel(Markdown(prompt), title="[bold]多模态资料提取 + 问题", title_align="left",
                                        border_style="cyan"))
                return await main_flow(prompt, cfg, show_workers=not hide_workers, leader=leader_spec)
            finally:
                await POOL.shutdown()  # 常驻进程只在本次命令内复用（如 Co-work 多轮），退出前关闭

        code = asyncio.run(run())
    except KeyboardInterrupt:
        console.print("\n[red]已中断。[/]")
        raise typer.Exit(130)
    raise typer.Exit(code)


@app.command()
def config(
    config_path: Path = ConfigOption,
    show: bool = typer.Option(False, "--show", "-s", help="在终端显示配置内容及各 CLI 安装状态，而不是打开编辑器"),
    reset: bool = typer.Option(False, "--reset", help="用默认模板覆盖当前配置文件"),
) -> None:
    """打开（或查看 / 重置）config.json。"""
    if reset:
        if config_path.exists():
            typer.confirm(f"确定用默认模板覆盖 {config_path}？", abort=True)
        write_default_config(config_path)
        console.print(f"[green]✓ 已写入默认配置：[/]{config_path}")
        return

    if ensure_config(config_path):
        console.print(f"[yellow]未找到配置文件，已生成默认模板：[/]{config_path}")

    if show:
        console.print(Syntax(config_path.read_text(encoding="utf-8"), "json", theme="ansi_dark",
                             line_numbers=True))
        cfg = _load(config_path)
        table = Table(title="Agents", title_justify="left", header_style="bold")
        table.add_column("角色")
        table.add_column("状态")
        table.add_column("名称")
        table.add_column("模型")
        table.add_column("命令")
        table.add_column("超时", justify="right")
        table.add_column("已安装")
        rows = [("Worker", w, "[green]启用[/]" if w.enabled else "[dim]停用[/]") for w in cfg.workers]
        rows += [("Leader", ld, "[bold yellow]默认[/]" if ld.name == cfg.default_leader else "")
                 for ld in cfg.leaders]
        for role, spec, state in rows:
            installed = "[green]✓[/]" if shutil.which(spec.command[0]) else "[bold red]✗ 未找到[/]"
            cmd = shlex.join(spec.argv_template())
            model_txt = (spec.selected_model or "[dim]CLI 默认[/]") if spec.model_flag else "[dim]—[/]"
            if spec.env_unset:
                cmd += f"\n[dim]env_unset: {', '.join(spec.env_unset)}[/]"
            table.add_row(role, state, spec.name, model_txt, cmd, f"{spec.timeout:.0f}s", installed)
        console.print(table)
        return

    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    console.print(f"正在打开 [cyan]{config_path}[/] ...")
    if editor:
        subprocess.run([*shlex.split(editor), str(config_path)])
        _load(config_path)  # 编辑完成后校验一次
        console.print("[green]✓ 配置校验通过[/]")
    else:
        typer.launch(str(config_path))
        console.print("[dim]提示：设置 $EDITOR 可在终端内编辑，并在保存后自动校验。[/]")


if __name__ == "__main__":
    app()
