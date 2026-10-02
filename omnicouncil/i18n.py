"""界面文案（English / 中文）。

    from omnicouncil.i18n import t
    t("card.judging", n=3)      # → "Judging 3 answers..." / "正在仲裁 3 份回答..."

GUI 默认英文，可在界面右上角切换，选择保存在 config.json 的 "language"。
终端 CLI（main.py）固定使用中文。
"""

from __future__ import annotations

LANGUAGES = ("en", "zh")
DEFAULT_LANGUAGE = "en"
_lang = DEFAULT_LANGUAGE


def set_language(lang: str) -> None:
    global _lang
    _lang = lang if lang in LANGUAGES else DEFAULT_LANGUAGE


def current() -> str:
    return _lang


def t(key: str, /, **kwargs) -> str:
    """key 为仅限位置参数，因此文案占位符也可以叫 {key}。"""
    entry = STRINGS.get(key)
    if entry is None:
        return key
    text = entry[0] if _lang == "en" else entry[1]
    return text.format(**kwargs) if kwargs else text


def conf(level: str | None) -> str:
    """把内部统一的确信度（高/中/低）翻译成当前语言。"""
    return t(f"conf.{level}") if level in ("高", "中", "低") else t("card.unknown")


# key: (English, 中文)
STRINGS: dict[str, tuple[str, str]] = {
    # —— 侧边栏 / 会话 ——
    "sidebar.new": ("＋  New chat", "＋  新建会话"),
    "sidebar.history": ("  History", "  历史会话"),
    "sidebar.hotkey_ok": ("  {key} to summon", "  {key} 随时唤醒"),
    "sidebar.hotkey_fail": ("  Hotkey {key} unavailable", "  快捷键 {key} 不可用"),
    "sidebar.hotkey_fail_tip": ("Registration failed (see terminal log). Change \"hotkey\" in config.json.",
                                "注册失败，详见终端日志；可在 config.json 的 hotkey 中更换"),
    "session.new": ("New chat", "新会话"),
    "session.delete": ("Delete chat", "删除会话"),
    "session.delete_confirm": ("Delete “{title}” and all of its history?", "确定删除「{title}」及其全部历史记录？"),
    "session.tooltip": ("{title}\n{when} · {n} question(s)", "{title}\n{when} · {n} 次提问"),
    "log.history": ("Loaded {n} saved chat(s) ({path})", "已加载 {n} 个历史会话（{path}）"),
    "log.history_failed": ("Failed to read history: {e}", "读取历史失败：{e}"),
    # —— 顶栏 / 输入 ——
    "header.config": ("⚙ Settings", "⚙ 配置"),
    "lang.switch": ("中文", "EN"),
    "lang.switch_tip": ("切换到中文界面", "Switch to English"),
    "lang.busy": ("Stop running tasks before switching language.", "请先停止正在运行的任务再切换语言。"),
    "input.placeholder": ("Ask a question…  (Enter to send, Shift+Enter for a new line)",
                          "输入问题…（Enter 发送，Shift+Enter 换行）"),
    "hint.ready": ("Enter to send · Shift+Enter for a new line", "Enter 发送 · Shift+Enter 换行"),
    "hint.busy": ("Agents are working…  click ■ to stop", "Agents 正在工作…  点击 ■ 可停止"),
    "hint.bad_config": ("Config error — check the panel on the right, fix it, then click “Reload”.",
                        "配置文件有误，请在右侧查看并修复后点击「重新加载」"),
    "btn.send": ("Send", "发送"),
    "btn.stop": ("Stop", "停止"),
    "empty.title": ("Ask several agents at once", "向多个 Agent 同时提问"),
    "empty.sub": ("Workers answer in parallel; the Leader checks consistency and confidence, then gives a final verdict",
                  "Workers 并发作答，Leader 评估一致性与确信度后给出最终定论"),
    # —— 状态行 ——
    "row.waiting": ("Waiting", "等待中"),
    "row.thinking": ("Thinking...", "正在思考..."),
    "row.expand": ("Expand ▾", "展开 ▾"),
    "row.collapse": ("Collapse ▴", "收起 ▴"),
    "row.failed": ("Failed", "失败"),
    "row.stopped": ("Stopped", "已停止"),
    # —— 运行卡片 ——
    "card.header": ("Mixture of Agents · {n} Workers → {leader}", "Mixture of Agents · {n} Workers → {leader}"),
    "card.stage1": ("Stage 1 · Workers answering in parallel", "阶段 1 · Workers 并发作答"),
    "card.stage2": ("Stage 2 · Leader arbitration", "阶段 2 · Leader 综合仲裁"),
    "card.stage3": ("Stage 3 · Secondary Review", "阶段 3 · Secondary Review"),
    "card.wait_workers": ("Waiting for workers...", "等待 Workers 完成..."),
    "card.done_chars": ("Done · {n} chars", "完成 · {n} 字符"),
    "card.no_answers": ("No usable answers — arbitration skipped", "没有可用的回答，跳过仲裁"),
    "card.judging": ("Judging {n} answer(s)...", "正在仲裁 {n} 份回答..."),
    "card.done_conf": ("Done · confidence: {conf}", "完成 · 确信度：{conf}"),
    "card.unparsed": ("unparsed", "未能解析"),
    "card.review_fallback": ("First-round confidence was Low and no other model is available, so {name} reviews it again.",
                             "首轮确信度为「低」，无其他可用模型，仍由 {name} 复审。"),
    "card.review_hetero": ("First-round confidence was Low — a different model, {name} ({vendor}), is reviewing it independently.",
                           "首轮确信度为「低」，改由异构模型 {name}（{vendor}）独立复审。"),
    "card.reviewing": ("Reviewing the disputed points...", "正在复审首轮争议点..."),
    "card.dim_note": ("Low confidence — {name} is reviewing; this verdict will be replaced…",
                      "确信度低，正在由 {name} 复审，结果将替换此裁决…"),
    "card.review_failed": ("Review failed — keeping the first-round verdict.", "复审失败，保留首轮裁决。"),
    "card.verdict": ("⚖  Final verdict", "⚖  最终裁决"),
    "card.verdict_reviewed": ("⚖  Final verdict (reviewed by {name})", "⚖  最终裁决（经 {name} 二次复审）"),
    "card.badge": ("Confidence · {conf}", "确信度 · {conf}"),
    "card.first_note": ("First-round {leader} rated confidence “{conf}”; below is the reviewer's conclusion.",
                        "首轮 {leader} 判定确信度为「{conf}」，以下为复审结论。"),
    "card.unknown": ("Unknown", "未知"),
    "card.failed_all": ("All workers failed, so there is nothing to arbitrate. Expand the rows above for details.",
                        "所有 Worker 均失败，无法进行仲裁。展开上方各行可查看错误详情。"),
    "card.failed_leader": ("Leader arbitration failed. Each worker's raw answer can still be expanded above.",
                           "Leader 仲裁失败。各 Worker 的原始回答仍可在上方展开查看。"),
    "card.stage_failed": ("Failed · {s}s", "失败 · {s}s"),
    "card.stage_leader_failed": ("Arbitration failed · {s}s", "仲裁失败 · {s}s"),
    "card.stage_done": ("Done · total {s}s", "完成 · 总耗时 {s}s"),
    "card.stopped": ("Stopped", "已停止"),
    "card.stopped_banner": ("Stopped — all subprocesses were terminated.", "已停止，所有子进程已结束。"),
    "card.error": ("Error", "出错"),
    "card.no_workers": ("No workers selected — enable at least one in the settings panel.",
                        "没有勾选任何 Worker，请在右侧配置面板中至少启用一个。"),
    "card.unexpected": ("Unexpected error: {e}", "未预期异常：{e}"),
    "card.save_failed": ("Failed to save history: {e}", "保存历史失败：{e}"),
    "conf.高": ("High", "高"),
    "conf.中": ("Medium", "中"),
    "conf.低": ("Low", "低"),
    # —— 配置面板 ——
    "panel.title": ("Settings", "配置"),
    "panel.error": ("Config error: {msg}", "配置有误：{msg}"),
    "panel.second": ("SECOND REVIEW", "SECOND REVIEW"),
    "panel.review_cb": ("Auto-review when confidence is Low", "确信度低时自动二次复审"),
    "panel.review_off": ("Off — low-confidence verdicts are used as-is.", "已关闭：低确信度时直接采用首轮裁决。"),
    "panel.reviewer": ("Reviewer: {name} ({vendor})", "复审模型：{name}（{vendor}）"),
    "panel.reviewer_self": ("No other model available; {name} will review itself.", "无其他可用模型，将由 {name} 自行复审。"),
    "panel.not_installed": ("{name} (not installed: {cli})", "{name}（未安装 {cli}）"),
    "panel.leader_not_installed": ("{name} (not installed)", "{name}（未安装）"),
    "panel.save": ("Save as default", "保存为默认"),
    "panel.reload": ("Reload", "重新加载"),
    "panel.open": ("Open config.json", "打开 config.json"),
    "panel.saved": ("✓ Saved {time}", "✓ 已保存 {time}"),
    "panel.save_failed": ("Save failed: {e}", "保存失败：{e}"),
    # —— 协作模式 ——
    "mode.section": ("MODE", "模式"),
    "mode.judge": ("Judge Mode", "Judge 模式"),
    "mode.cowork": ("Co-work Mode", "Co-work 模式"),
    "mode.judge_hint": ("Workers answer independently; the Leader judges once (with an optional second review).",
                        "Workers 独立作答，Leader 一次性仲裁（可选二次复审）。"),
    "mode.cowork_hint": ("Workers discuss over {rounds} rounds, seeing each other's answers each round; the Leader then summarizes.",
                         "Workers 进行 {rounds} 轮圆桌讨论，每轮都能看到彼此上一轮的答案，最后由 Leader 总结。"),
    "panel.discussion": ("DISCUSSION", "讨论"),
    "panel.extend_cb": ("Extend when confidence is Low", "确信度低时延长讨论"),
    "panel.extend_on": ("If the Leader rates confidence Low, its dispute guidance becomes the focus of another round (up to {max} rounds).",
                        "若 Leader 判定确信度低，其争议指导意见将作为下一轮的讨论重点（最多 {max} 轮）。"),
    "panel.extend_off": ("Off — the Leader's summary after round {rounds} is final.",
                         "已关闭：第 {rounds} 轮后的 Leader 总结即为最终结论。"),
    "card.cowork_header": ("Co-work · {n} Workers · {rounds} rounds → {leader}", "Co-work · {n} Workers · {rounds} 轮 → {leader}"),
    "card.round_stage": ("Round {r}/{total} · {phase}", "第 {r}/{total} 轮 · {phase}"),
    "phase.independent": ("Independent answers", "独立作答"),
    "phase.peer_review": ("Peer review", "互评修正"),
    "phase.guided": ("Guided by the Leader", "按 Leader 指导讨论"),
    "card.r_thinking": ("Round {r}/{total}: Thinking...", "第 {r}/{total} 轮：正在思考..."),
    "card.r_reviewing": ("Round {r}/{total}: Reviewing peers...", "第 {r}/{total} 轮：正在参考同伴的观点..."),
    "card.r_guided": ("Round {r}/{total}: Addressing the Leader's guidance...", "第 {r}/{total} 轮：正在讨论 Leader 指出的争议..."),
    "card.r_done": ("Round {r} done · {n} chars", "第 {r} 轮完成 · {n} 字符"),
    "card.round_detail": ("Round {r}", "第 {r} 轮"),
    "card.wait_discussion": ("Waiting for the discussion...", "等待讨论结束..."),
    "card.summarizing": ("Summarizing {n} final answer(s) after round {r}...", "正在总结第 {r} 轮后的 {n} 份答案..."),
    "card.leader_round": ("{name} · after round {r}", "{name} · 第 {r} 轮后"),
    "card.guidance_row": ("Dispute guidance", "争议指导意见"),
    "card.guidance_status": ("Low confidence — discussion extended to round {r}", "确信度低，讨论延长至第 {r} 轮"),
    "card.dim_extend": ("Low confidence — the discussion continues in round {r}; this verdict will be replaced…",
                        "确信度低，讨论将在第 {r} 轮继续，结果将替换此裁决…"),
    "card.verdict_cowork": ("⚖  Final verdict (Co-work · {r} rounds)", "⚖  最终裁决（Co-work · {r} 轮）"),
    "card.stage_cowork_done": ("Done · {r} rounds · total {s}s", "完成 · {r} 轮 · 总耗时 {s}s"),
    # —— 降噪后的回答气泡 / 共识评分 / 多轮对话 ——
    "card.process": ("Process", "过程"),
    "card.waiting_answer": ("Waiting for the final answer…", "正在等待最终答案…"),
    "card.consensus": ("Consensus {score} {dot}", "共识度 {score} {dot}"),
    "card.consensus_tip": ("1.0 = all agents agree · 0.5 = a majority agrees, one differs · 0 = no consensus",
                           "1.0 = 所有 Agent 一致 · 0.5 = 多数一致、有一个不同 · 0 = 无法达成共识"),
    "card.judge_by": ("⚖  {name}", "⚖  {name}"),
    "card.reviewed_by": ("⚖  {name} · second review", "⚖  {name} · 二次复审"),
    "card.cowork_by": ("⚖  {name} · Co-work · {r} rounds", "⚖  {name} · Co-work · {r} 轮"),
    "card.context": (" · {n} previous turn(s) as context", " · 带入 {n} 轮上下文"),
    "card.analysis": ("Analysis", "评审分析"),
    "card.no_quorum": ("Insufficient quorum · not cross-validated", "样本不足 · 未经交叉验证"),
    "card.no_quorum_tip": ("Only one agent answered, so there was nothing to compare against.",
                           "只有一个 Agent 作答，没有可比较的其他回答。"),
    "card.labels": ("Answers were shown to the Judge anonymized and shuffled: {pairs}",
                    "回答以匿名、打乱顺序的方式提交给评审：{pairs}"),
    "card.first_answer": ("First-round answer (replaced)", "首轮答案（已被替换）"),
    "card.raw_output": ("Raw output (not valid JSON — parsed with fallback)", "原始输出（非合法 JSON，已回退解析）"),
    # —— 模型选择 ——
    "panel.model_default": ("CLI default", "CLI 默认"),
    "panel.model_tip": ("Model for {name} (passed to the CLI as {flag} <model>)", "{name} 使用的模型（以 {flag} <模型> 传给 CLI）"),
    "panel.model_saved": ("✓ {name} → {model}", "✓ {name} → {model}"),
    "panel.web": ("🌐 Web search", "🌐 联网搜索"),
    "panel.web_tip": ("Let {name} search the web (Claude: read-only WebSearch/WebFetch tools; Codex: --search). "
                      "Slower and uses more quota; search queries go to the provider's search service.",
                      "允许 {name} 联网搜索（Claude：只读的 WebSearch / WebFetch 工具；Codex：--search）。"
                      "会更慢、消耗更多额度；搜索内容会发送到该厂商的搜索服务。"),
    "panel.web_saved": ("✓ {name}: web search {state}", "✓ {name}：联网搜索{state}"),
    "panel.on": ("on", "已开启"),
    "panel.off": ("off", "已关闭"),
    # —— 常驻进程池 ——
    "pool.status": ("⚡ Warm pool: {items}", "⚡ 常驻进程：{items}"),
    "pool.none": ("⚡ Warm pool: none (all agents run one-shot)", "⚡ 常驻进程：无（全部按单次调用运行）"),
    "pool.tip": ("Persistent CLI processes skip cold starts. ● ready · ◐ busy · ○ restarting. Enable per agent with \"is_persistent\" in config.json.",
                 "常驻的 CLI 进程可跳过冷启动。● 就绪 · ◐ 忙碌 · ○ 重启中。可在 config.json 中用 \"is_persistent\" 按 Agent 开关。"),
    # —— 附件 / 录音 / 前置解析 ——
    "att.attach_tip": ("Attach files (images, PDF, text, audio)", "添加附件（图片、PDF、文本、音频）"),
    "att.pick": ("Choose attachments", "选择附件"),
    "att.filter": ("Supported files", "支持的文件"),
    "att.all": ("All files", "所有文件"),
    "att.remove": ("Remove", "移除"),
    "att.only": ("(attachments only)", "（仅附件）"),
    "rec.tip": ("Record a voice note (click to start, click again to stop)", "录制语音（点击开始，再次点击停止）"),
    "rec.recording": ("Recording… click 🎤 again to stop", "正在录音…再次点击 🎤 停止"),
    "rec.no_device": ("No microphone found.", "未找到麦克风。"),
    "rec.failed": ("Recording failed: {e}", "录音失败：{e}"),
    "rec.silent": ("The recording is silent — the app may not have microphone permission (System Settings → Privacy & Security → Microphone).",
                   "录音没有声音——可能尚未获得麦克风权限（系统设置 → 隐私与安全性 → 麦克风）。"),
    "card.pre_section": ("PRE-PROCESSING", "前置解析"),
    "card.stage_pre": ("Pre-processing attachments", "正在解析附件"),
    "card.pre_reading": ("Reading {files}...", "正在读取 {files}..."),
    "card.pre_done": ("Extracted · {n} chars", "已提取 · {n} 字符"),
    "card.pre_local": ("📝 {files} — read locally", "📝 {files} — 本地读取"),
    "card.pre_wait": ("The Leader is reading the attachments…", "Leader 正在解析附件…"),
    "card.pre_unsupported": ("⚠ {msg}", "⚠ {msg}"),
    # —— 账户与用量 ——
    "acct.section": ("ACCOUNT & USAGE", "账户与用量"),
    "acct.refresh_tip": ("Refresh status", "刷新状态"),
    "acct.refresh_tip_claude": ("Refresh status and live usage (sends one tiny request)",
                                "刷新状态与实时用量（会发送一条极小的请求）"),
    "acct.login": ("Switch / Log in", "切换 / 登录"),
    "acct.login_tip": ("Runs in your terminal: {cmd}", "在系统终端中执行：{cmd}"),
    "acct.refreshing": ("Refreshing", "刷新中"),
    "acct.not_installed": ("Not installed", "未安装"),
    "acct.limited": ("Rate-limited", "限额中"),
    "acct.logged_in": ("Signed in", "已登录"),
    "acct.logged_out": ("Signed out", "未登录"),
    "acct.unknown": ("Unknown", "未知"),
    "acct.active": ("Active", "Active"),
    "acct.unavailable": ("Unavailable", "不可用"),
    "acct.limited_until": ("⛔ Rate-limited until {t}", "⛔ 限额中（至 {t}）"),
    "acct.limited_plain": ("⛔ Rate-limited", "⛔ 限额中"),
    "acct.reset_done": ("reset", "已重置"),
    "acct.resets": ("resets {t}", "{t} 重置"),
    "acct.window_tip": ("{label} window: {pct}% used", "{label} 窗口：已用 {pct}%"),
    "acct.terminal_fail": ("Could not open a terminal: {e}", "无法打开终端：{e}"),
    "acct.terminal_opened": ("Opened the login flow in {app} — click ↻ when you're done.",
                             "已在 {app} 中打开登录流程，完成后点击 ↻ 刷新。"),
    "acct.fetch_failed": ("Fetch failed: {e}", "获取失败：{e}"),
    "acct.src_live": ("Live query · {t}", "实时查询 · {t}"),
    "acct.src_click": ("Click ↻ for live usage (sends one tiny request)", "点击 ↻ 获取实时用量（会发送一条极小的请求）"),
    "acct.usage_failed": ("Could not fetch usage: {e}", "未能获取用量：{e}"),
    "acct.codex_logged": ("Signed in ({how})", "已登录（{how}）"),
    "acct.src_logs": ("Local session logs · {t}", "本地会话日志 · {t}"),
    "acct.src_no_logs": ("No local session logs yet (created after using codex interactively)",
                         "未找到本地会话日志（交互式使用 codex 后才会生成）"),
    "acct.agy_ok": ("CLI available · {n} models", "CLI 可用 · {n} 个模型"),
    "acct.src_agy": ("Antigravity doesn't expose account or usage info", "Antigravity 不公开账户与用量信息"),
    "acct.timeout": ("timed out (>{s}s)", "超时（>{s}s）"),
    "acct.script_title": ("OmniCouncil · {title} — log in / switch account", "OmniCouncil · {title} 登录 / 切换账户"),
    "acct.script_run": ("Running: {cmd}", "将执行：{cmd}"),
    "acct.script_done": ("✓ Done — you can close this window and click ↻ in OmniCouncil.",
                         "✓ 完成后可关闭此窗口，回到 OmniCouncil 点击 ↻ 刷新状态。"),
    "win.five_hour": ("5-hour", "5 小时"),
    "win.seven_day": ("7-day", "7 天"),
    "win.seven_day_opus": ("7-day · Opus", "7 天 · Opus"),
    "win.seven_day_sonnet": ("7-day · Sonnet", "7 天 · Sonnet"),
    # —— 引擎 ——
    "eng.not_found": ("Command `{exe}` not found (make sure it's installed and on PATH)",
                      "未找到命令 `{exe}`（请确认已安装并在 PATH 中）"),
    "eng.cant_start": ("Could not start: {e}", "无法启动：{e}"),
    "eng.timeout": ("Timed out (>{s}s), terminated", "超时（>{s}s），已终止"),
    "eng.empty": ("Empty output", "输出为空"),
    "eng.unexpected": ("Unexpected error: {e}", "未预期异常：{e}"),
    # —— 全局快捷键 ——
    "hk.hint": ("If the hotkey doesn't work: make sure it doesn't clash with a system or app shortcut "
                "(Option+Space is often taken by input methods/Spotlight) and change \"hotkey\" in config.json; "
                "if needed, grant Terminal/Python access in System Settings → Privacy & Security → Accessibility, then restart.",
                "若快捷键无效：先确认没有与系统或其他应用的快捷键冲突（如 Option+Space 常被输入法/Spotlight 占用），"
                "可在 config.json 的 \"hotkey\" 中更换；必要时在「系统设置 → 隐私与安全性 → 辅助功能」"
                "中为终端 / Python 授予权限后重启应用。"),
    "hk.registered": ("Registered global hotkey {key}. {hint}", "已注册全局快捷键 {key}。{hint}"),
    "hk.taken": ("that combination is already taken by another app", "该组合已被其他应用占用"),
    "hk.failed": ("Failed to register global hotkey {key}: {reason}. {hint}", "注册全局快捷键 {key} 失败：{reason}。{hint}"),
    "hk.handler_failed": ("Failed to install the event handler (OSStatus {err}). {hint}",
                          "安装事件处理器失败（OSStatus {err}）。{hint}"),
    "hk.invalid": ("Invalid hotkey ({spec}): {e}", "快捷键配置无效（{spec}）：{e}"),
    "hk.mac_only": ("Global hotkeys are only supported on macOS", "全局快捷键仅支持 macOS"),
    "hk.empty": ("hotkey is empty", "快捷键为空"),
    "hk.bad_key": ("unsupported key: {key!r}", "不支持的按键：{key!r}"),
    "hk.bad_mod": ("unsupported modifier: {mod!r} (use cmd / shift / option / ctrl)",
                   "不支持的修饰键：{mod!r}（可用 cmd / shift / option / ctrl）"),
    "hk.need_mod": ("a global hotkey needs at least one modifier", "全局快捷键至少需要一个修饰键"),
}
