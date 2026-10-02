"""Terminal front end: renders orchestration events with rich."""

from __future__ import annotations

from typing import Optional

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TaskID, TextColumn, TimeElapsedColumn
from rich.status import Status
from rich.table import Table
from rich.text import Text

from .agent import AgentResult
from .config import AgentSpec, Config
from .cowork import run_cowork_loop
from .orchestrate import EXIT_LEADER_FAILED, EXIT_NO_WORKERS, orchestrate

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
