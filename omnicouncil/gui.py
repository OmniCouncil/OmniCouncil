#!/usr/bin/env python3
"""OmniCouncil 桌面端（PySide6 + qasync）。

布局：
    左侧   会话列表（持久化到 data/history.sqlite，启动时自动加载，右键可删除）
    中间   对话区：Worker 实时进度 + Leader 裁决（Markdown 渲染）+ 异构模型二次复审，底部多行输入框
    右侧   可折叠配置面板：Workers / Leader / 二次复审开关 / 账户与用量

界面默认英文，右上角可切换中文（保存在 config.json 的 "language"）。
qasync 把 asyncio 事件循环跑在 Qt 主线程上，engine.orchestrate() 的事件回调可直接更新界面。
全局快捷键（默认 ⌘⇧J，config.json 的 "hotkey"）可随时把窗口唤回最前并聚焦输入框。

    python gui.py [--config path/to/config.json] [--db path/to/history.sqlite]
"""

from __future__ import annotations

import argparse
import array
import asyncio
import logging
import math
import shlex
import shutil as _shutil
import sys
import time
import uuid
import wave
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

import qasync
from PySide6.QtCore import QEasingCurve, QObject, QPoint, QPropertyAnimation, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QIcon, QKeyEvent, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QGraphicsDropShadowEffect,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QTextBrowser,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from PySide6.QtMultimedia import QAudioFormat, QAudioSource, QMediaDevices

from . import accounts, i18n
from .accounts import PROVIDERS, ProviderStatus, fmt_reset, parse_limit_message
from .config import (
    DEFAULT_CONFIG_PATH,
    DEFAULT_HOTKEY,
    AgentSpec,
    Config,
    ConfigError,
    load_config,
    save_language,
    save_selection,
    save_worker_model,
)
from .agent import AgentResult
from .cowork import run_cowork_loop
from .hotkey import GlobalHotkey, activate_app, pretty_hotkey
from .i18n import conf, t
from .multimodal import FILE_KINDS as _FILE_EXTS
from .multimodal import file_kind, preprocess_multimodal
from .orchestrate import (
    CONTEXT_TURNS,
    EXIT_LEADER_FAILED,
    EXIT_NO_WORKERS,
    RunOutcome,
    build_context_prompt,
    orchestrate,
    select_reviewer,
)
from .paths import ASSETS_DIR, DATA_DIR, ensure_login_path
from .pool import POOL
from .runner import kill_running_processes
from .storage import DEFAULT_DB_PATH, HistoryStore, outcome_to_record, record_to_outcome

log = logging.getLogger("omnicouncil")

# ---------------------------------------------------------------------------
# 视觉：macOS 暗色风格
# ---------------------------------------------------------------------------

C = {
    "bg": "#1c1c1e",
    "sidebar": "#232325",
    "card": "#2a2a2d",
    "card_hi": "#323236",
    "border": "#3a3a3e",
    "text": "#f2f2f7",
    "muted": "#98989f",
    "faint": "#636366",
    "accent": "#0a84ff",
    "accent_hi": "#409cff",
    "green": "#30d158",
    "red": "#ff453a",
    "orange": "#ff9f0a",
    "yellow": "#ffd60a",
}
AGENT_COLORS = ["#bf5af2", "#64d2ff", "#30d158", "#ff9f0a", "#ff375f"]
CONF_COLORS = {"高": C["green"], "中": C["yellow"], "低": C["red"]}
SCORE_COLORS = {1.0: C["green"], 0.5: C["yellow"], 0.0: C["red"]}
SCORE_DOTS = {1.0: "🟢", 0.5: "🟡", 0.0: "🔴"}
SCORE_TEXT = {1.0: "1.0", 0.5: "0.5", 0.0: "0"}
PROVIDER_COLORS = {"anthropic": "#d97757", "openai": "#10a37f", "google": "#4285f4"}
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
KIND_ICONS = {"image": "🖼", "pdf": "📄", "audio": "🎙", "text": "📝"}
ATTACHMENTS_DIR = DATA_DIR / "attachments"
RECORDINGS_DIR = DATA_DIR / "recordings"
CHEVRON = (ASSETS_DIR / "chevron-down.svg").as_posix()

QSS = f"""
* {{ color: {C['text']}; font-size: 13px; }}
QMainWindow, #Main {{ background: {C['bg']}; }}
QToolTip {{ background: {C['card_hi']}; color: {C['text']}; border: 1px solid {C['border']}; padding: 4px 6px; }}

#Sidebar, #ConfigPanel, #PanelBody {{ background: {C['sidebar']}; }}
#Sidebar {{ border-right: 1px solid {C['border']}; }}
#ConfigPanel {{ border-left: 1px solid {C['border']}; }}
#AppTitle {{ font-size: 15px; font-weight: 600; }}
#Section {{ color: {C['muted']}; font-size: 11px; font-weight: 600; letter-spacing: 0.5px; }}
#Muted {{ color: {C['muted']}; font-size: 12px; }}
#Faint {{ color: {C['faint']}; font-size: 11px; }}

QListWidget {{ background: transparent; border: none; outline: none; }}
QListWidget::item {{ padding: 8px 10px; border-radius: 7px; margin: 1px 0; color: {C['text']}; }}
QListWidget::item:hover {{ background: {C['card']}; }}
QListWidget::item:selected {{ background: {C['card_hi']}; }}

QPushButton {{ background: {C['card_hi']}; border: 1px solid {C['border']}; border-radius: 7px; padding: 6px 12px; }}
QPushButton:hover {{ background: #3a3a3f; }}
QPushButton:pressed {{ background: {C['card']}; }}
QPushButton:disabled {{ color: {C['faint']}; }}
#Primary {{ background: {C['accent']}; border: none; color: white; font-weight: 600; }}
#Primary:hover {{ background: {C['accent_hi']}; }}
#SmallBtn {{ padding: 2px 8px; font-size: 11px; border-radius: 6px; }}

QToolButton {{ background: transparent; border: none; border-radius: 6px; padding: 4px 8px; color: {C['muted']}; }}
QToolButton:hover {{ background: {C['card_hi']}; color: {C['text']}; }}
QToolButton:checked {{ color: {C['text']}; }}
#LangBtn {{ border: 1px solid {C['border']}; padding: 3px 9px; font-size: 12px; }}

#Header {{ border-bottom: 1px solid {C['border']}; }}
#HeaderTitle {{ font-size: 14px; font-weight: 600; }}

QScrollArea {{ background: transparent; border: none; }}
#ChatContainer {{ background: {C['bg']}; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: #48484c; border-radius: 3px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: #5a5a5f; }}
QScrollBar::add-line, QScrollBar::sub-line, QScrollBar::add-page, QScrollBar::sub-page {{ height: 0; background: none; }}

#UserBubble {{ background: {C['accent']}; border-radius: 14px; padding: 9px 13px; color: white; }}
#RunCard {{ background: {C['card']}; border: 1px solid {C['border']}; border-radius: 14px; }}
#Row {{ background: transparent; border-radius: 8px; }}
#Row:hover {{ background: {C['card_hi']}; }}
#Detail {{ background: {C['bg']}; border: 1px solid {C['border']}; border-radius: 8px; }}
#Verdict {{ background: {C['bg']}; border: 1px solid {C['border']}; border-radius: 10px; }}
#Accordion {{ background: {C['bg']}; border: 1px solid {C['border']}; border-radius: 9px; }}
#AccordionHeader {{ background: transparent; border-radius: 9px; }}
#AccordionHeader:hover {{ background: {C['card_hi']}; }}
#Provider {{ background: {C['card']}; border: 1px solid {C['border']}; border-radius: 10px; }}
#Pill {{ border-radius: 8px; padding: 1px 8px; font-size: 11px; font-weight: 600; }}
#Chip {{ background: {C['card_hi']}; border: 1px solid {C['border']}; border-radius: 12px; }}
#ToolBtn {{ background: transparent; border: none; border-radius: 15px; font-size: 16px; padding: 0; }}
#ToolBtn:hover {{ background: {C['card_hi']}; }}
#ToolBtn:checked {{ background: {C['red']}; }}

#Composer {{ background: {C['card']}; border: 1px solid {C['border']}; border-radius: 16px; }}
#Composer QTextEdit {{ background: transparent; border: none; font-size: 14px; }}
#SendBtn {{ background: {C['accent']}; border: none; border-radius: 15px; color: white; font-size: 15px; font-weight: 700; padding: 0; }}
#SendBtn:hover {{ background: {C['accent_hi']}; }}
#SendBtn:disabled {{ background: #3a3a3e; color: {C['faint']}; }}
#SendBtn[busy="true"] {{ background: {C['red']}; font-size: 11px; }}

QCheckBox {{ spacing: 8px; padding: 3px 0; }}
QCheckBox:disabled {{ color: {C['faint']}; }}
QComboBox {{ background: {C['card_hi']}; border: 1px solid {C['border']}; border-radius: 7px; padding: 5px 28px 5px 10px; }}
QComboBox:hover {{ border-color: #4a4a4f; }}
QComboBox::drop-down {{ subcontrol-origin: padding; subcontrol-position: center right; width: 24px; border: none; }}
QComboBox::down-arrow {{ image: url("{CHEVRON}"); width: 12px; height: 12px; }}
QComboBox::down-arrow:on {{ top: 1px; }}
QComboBox QAbstractItemView {{ background: {C['card_hi']}; border: 1px solid {C['border']};
    selection-background-color: {C['accent']}; outline: none; padding: 4px; }}
QProgressBar {{ background: #3a3a3e; border: none; border-radius: 3px; }}
QMenu {{ background: {C['card_hi']}; border: 1px solid {C['border']}; padding: 4px; }}
QMenu::item {{ padding: 5px 16px; border-radius: 4px; }}
QMenu::item:selected {{ background: {C['accent']}; }}
"""

MARKDOWN_CSS = f"""
h1, h2, h3, h4 {{ margin-top: 12px; margin-bottom: 4px; }}
h3 {{ font-size: 15px; }}
p, li {{ line-height: 150%; }}
code {{ background-color: #2c2c30; color: #ffb86c; font-family: Menlo, monospace; }}
pre {{ background-color: #141416; }}
a {{ color: {C['accent_hi']}; }}
"""


def label(text: str = "", obj: str = "", wrap: bool = False) -> QLabel:
    lb = QLabel(text)
    if obj:
        lb.setObjectName(obj)
    lb.setWordWrap(wrap)
    return lb


def shrinkable(lb: QLabel) -> QLabel:
    """允许单行标签在空间不足时被压缩（不再撑宽父布局）。"""
    lb.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
    lb.setMinimumWidth(0)
    return lb


def color_dot(color: str, size: int = 10) -> QIcon:
    pm = QPixmap(size * 2, size * 2)  # 2x 供 Retina 使用
    pm.setDevicePixelRatio(2)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor(color))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawEllipse(1, 1, size - 2, size - 2)
    p.end()
    return QIcon(pm)


def shadow(widget: QWidget, blur: int = 28, alpha: int = 110, dy: int = 6) -> None:
    eff = QGraphicsDropShadowEffect(widget)
    eff.setBlurRadius(blur)
    eff.setOffset(0, dy)
    eff.setColor(QColor(0, 0, 0, alpha))
    widget.setGraphicsEffect(eff)


def banner(text: str, color: str) -> QLabel:
    b = label(text, wrap=True)
    b.setStyleSheet(f"color: {color}; border: 1px solid {color}; border-radius: 8px; padding: 8px 10px;")
    return b


def clear_layout(layout) -> None:
    while layout.count():
        item = layout.takeAt(0)
        if item.widget():
            item.widget().deleteLater()


# ---------------------------------------------------------------------------
# 基础组件
# ---------------------------------------------------------------------------


class MarkdownView(QTextBrowser):
    """高度随内容自适应的 Markdown 视图（嵌入在滚动区中，自身不滚动）。"""

    def __init__(self, text: str = "", parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setOpenExternalLinks(True)
        self.setStyleSheet("background: transparent;")
        self.document().setDefaultStyleSheet(MARKDOWN_CSS)
        self.document().setDocumentMargin(4)
        self.document().documentLayout().documentSizeChanged.connect(self._fit)
        self.set_markdown(text)

    def set_markdown(self, text: str) -> None:
        self.setMarkdown(text)
        self._fit()

    def _fit(self, *_) -> None:
        h = math.ceil(self.document().size().height()) + 2
        if self.height() != h:
            self.setFixedHeight(h)

    def resizeEvent(self, e) -> None:  # noqa: N802 (Qt API)
        super().resizeEvent(e)
        self._fit()

    def wheelEvent(self, e) -> None:  # noqa: N802 —— 交给外层滚动区
        e.ignore()


class StatusRow(QFrame):
    """一个 Agent 的状态行：动画 / 名称 / 状态 / 计时，可展开查看详情。"""

    def __init__(self, name: str, color: str, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("Row")
        self._t0 = 0.0
        self._frame = 0
        self._timer = QTimer(self)
        self._timer.setInterval(90)
        self._timer.timeout.connect(self._tick)

        self.icon = label("◦")
        self.icon.setFixedWidth(16)
        self.icon.setStyleSheet(f"color: {C['faint']}; font-size: 14px;")
        self._icon_color = C["faint"]
        self.started = False
        dot = label("●")
        dot.setStyleSheet(f"color: {color}; font-size: 9px;")
        self.name = label(name)
        self.name.setStyleSheet("font-weight: 600;")
        self.name.setMinimumWidth(90)
        self.status = shrinkable(label(t("row.waiting"), "Muted"))
        self.elapsed = label("", "Faint")
        self.toggle = QToolButton()
        self.toggle.setText(t("row.expand"))
        self.toggle.setCheckable(True)
        self.toggle.setVisible(False)
        self.toggle.toggled.connect(self._on_toggle)

        top = QHBoxLayout()
        top.setContentsMargins(8, 5, 6, 5)
        top.setSpacing(8)
        for w in (self.icon, dot, self.name):
            top.addWidget(w)
        top.addWidget(self.status, 1)
        top.addWidget(self.elapsed)
        top.addWidget(self.toggle)

        self.detail = QFrame()
        self.detail.setObjectName("Detail")
        self.detail.setVisible(False)
        self.detail_layout = QVBoxLayout(self.detail)
        self.detail_layout.setContentsMargins(10, 8, 10, 8)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(4)
        outer.addLayout(top)
        outer.addWidget(self.detail)

    @property
    def running(self) -> bool:
        return self._timer.isActive()

    def start(self, text: Optional[str] = None) -> None:
        self._t0 = time.monotonic()
        self.status.setText(text or t("row.thinking"))
        self.status.setStyleSheet("")
        self.icon.setStyleSheet(f"color: {C['accent_hi']}; font-size: 14px;")
        self._icon_color = C["accent_hi"]
        self.started = True
        self._timer.start()
        self._tick()

    def _tick(self) -> None:
        self._frame = (self._frame + 1) % len(SPINNER)
        self.icon.setText(SPINNER[self._frame])
        self.elapsed.setText(f"{time.monotonic() - self._t0:.1f}s")

    def _finish(self, icon: str, color: str, text: str, elapsed: Optional[float]) -> None:
        self._timer.stop()
        self.started = True
        self.icon.setText(icon)
        self.icon.setStyleSheet(f"color: {color}; font-size: 14px; font-weight: 700;")
        self._icon_color = color
        self.status.setText(text)
        if elapsed is not None:
            self.elapsed.setText(f"{elapsed:.1f}s")

    def set_model(self, model: str) -> None:
        """在名称后面以淡色显示模型名。"""
        self.model = model
        self.model_label = label(model, "Faint")
        self.layout().itemAt(0).layout().insertWidget(3, self.model_label)

    def chip_html(self, prefix: str = "") -> str:
        """折叠标题中的状态小标签，如「✓ Claude 3.8s」。"""
        import html
        el = self.elapsed.text()
        return (f"<span style='color:{self._icon_color}; font-weight:700'>{html.escape(self.icon.text())}</span> "
                f"{html.escape(prefix + self.name.text())}"
                + (f" <span style='color:{C['faint']}'>{html.escape(el)}</span>" if el else ""))

    def add_detail(self, markdown: str) -> None:
        self.detail_layout.addWidget(MarkdownView(markdown))
        self.toggle.setVisible(True)

    def succeed(self, text: str, elapsed: float, detail_markdown: Optional[str] = None) -> None:
        self._finish("✓", C["green"], text, elapsed)
        if detail_markdown:
            self.add_detail(detail_markdown)

    def fail(self, result: AgentResult) -> None:
        brief = result.error_brief or t("row.failed")
        rc = f"returncode={result.returncode} · " if result.returncode not in (None, 0) else ""
        self._finish("✗", C["red"], f"{rc}{result.error_summary}", result.elapsed or None)
        self.status.setStyleSheet(f"color: {C['red']};")
        self.status.setToolTip(brief[-1500:])
        err = label(brief[-1500:], wrap=True)
        err.setStyleSheet(f"color: {C['red']}; font-family: Menlo, monospace; font-size: 12px;")
        err.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.detail_layout.addWidget(err)
        self.toggle.setVisible(True)

    def skip(self, text: str) -> None:
        self._finish("–", C["faint"], text, None)

    def cancel(self) -> None:
        if self.running:
            self._finish("■", C["faint"], t("row.stopped"), time.monotonic() - self._t0)

    def _on_toggle(self, on: bool) -> None:
        self.detail.setVisible(on)
        self.toggle.setText(t("row.collapse") if on else t("row.expand"))


# ---------------------------------------------------------------------------
# 对话区
# ---------------------------------------------------------------------------


class Accordion(QFrame):
    """可折叠面板：标题行显示摘要（富文本），点击展开 / 收起内容。默认收起。"""

    toggled = Signal(bool)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("Accordion")
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        self.header = QFrame()
        self.header.setObjectName("AccordionHeader")
        self.header.setCursor(Qt.CursorShape.PointingHandCursor)
        self.header.mouseReleaseEvent = lambda _e: self.set_expanded(not self.expanded)
        h = QHBoxLayout(self.header)
        h.setContentsMargins(10, 6, 10, 6)
        h.setSpacing(8)
        self.arrow = label("▸", "Faint")
        self.arrow.setFixedWidth(10)
        h.addWidget(self.arrow)
        self.summary = shrinkable(label(""))
        self.summary.setTextFormat(Qt.TextFormat.RichText)
        self.summary.setStyleSheet("font-size: 12px;")
        h.addWidget(self.summary, 1)
        v.addWidget(self.header)
        self.body = QWidget()
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(6, 4, 6, 8)
        self.body_layout.setSpacing(6)
        self.body.hide()
        v.addWidget(self.body)

    @property
    def expanded(self) -> bool:
        return self.body.isVisibleTo(self)

    def set_expanded(self, on: bool) -> None:
        self.body.setVisible(on)
        self.arrow.setText("▾" if on else "▸")
        self.toggled.emit(on)

    def set_summary(self, html: str) -> None:
        self.summary.setText(html)


class RunCard(QFrame):
    """一次提问的回答气泡：以最终答案为主体；Workers / Leader / 复审 / 讨论过程收在默认折叠的「过程」面板里。

    实时运行时由 orchestrate() / run_cowork_loop() 的事件驱动；从历史恢复时用 replay() 重放同样的事件。"""

    def __init__(self, worker_names: list[str], leader_name: str, mode: str = "judge", rounds: int = 3,
                 context_turns: int = 0, worker_models: Optional[list[str]] = None,
                 parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("RunCard")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self._t0 = time.monotonic()
        self._first_verdict: Optional[AgentResult] = None
        self._last_verdict: Optional[AgentResult] = None
        self.reviewer: Optional[SimpleNamespace] = None  # 供保存历史：name / vendor / fallback
        self.mode = mode
        self._leader_name = leader_name
        self._worker_index = {n: i for i, n in enumerate(worker_names)}
        self._round_text = ""
        self._frame = 0

        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 10, 14, 14)
        lay.setSpacing(8)

        head = QHBoxLayout()
        header = (t("card.cowork_header", n=len(worker_names), rounds=rounds, leader=leader_name) if mode == "cowork"
                  else t("card.header", n=len(worker_names), leader=leader_name))
        if context_turns:
            header += t("card.context", n=context_turns)
        head.addWidget(shrinkable(label(header, "Faint")), 1)
        self.stage = label(t("card.round_stage", r=1, total=rounds, phase=t("phase.independent"))
                           if mode == "cowork" else t("card.stage1"), "Faint")
        head.addWidget(self.stage)
        lay.addLayout(head)

        # —— 过程（默认折叠）——
        self.details = Accordion()
        body = self.details.body_layout
        # 前置解析（有附件时才出现）
        self.pre_box = QWidget()
        pv = QVBoxLayout(self.pre_box)
        pv.setContentsMargins(0, 0, 0, 4)
        pv.setSpacing(4)
        pv.addWidget(label(t("card.pre_section"), "Section"))
        self._pre_layout = pv
        self.pre_rows: dict[int, StatusRow] = {}
        self.pre_box.hide()
        body.addWidget(self.pre_box)
        self._waiting_text = t("card.waiting_answer")
        body.addWidget(label("WORKERS", "Section"))
        self.worker_rows = [StatusRow(n, AGENT_COLORS[i % len(AGENT_COLORS)]) for i, n in enumerate(worker_names)]
        for row, model in zip(self.worker_rows, worker_models or []):
            if model:
                row.set_model(model)
        for row in self.worker_rows:
            body.addWidget(row)
        body.addSpacing(4)
        body.addWidget(label("LEADER", "Section"))
        self.leader_row = StatusRow(leader_name, C["yellow"])
        self.leader_row.status.setText(t("card.wait_discussion") if mode == "cowork" else t("card.wait_workers"))
        self.leader_rows: list[StatusRow] = [self.leader_row]   # Co-work 延长讨论时会追加
        self._leader_box = QVBoxLayout()
        self._leader_box.setSpacing(4)
        self._leader_box.addWidget(self.leader_row)
        body.addLayout(self._leader_box)
        # Secondary Review（Judge 模式首轮确信度低时才出现）
        self.review_box = QWidget()
        rv = QVBoxLayout(self.review_box)
        rv.setContentsMargins(0, 4, 0, 0)
        rv.setSpacing(4)
        rv.addWidget(label("SECONDARY REVIEW", "Section"))
        self.review_note = label("", "Faint", wrap=True)
        rv.addWidget(self.review_note)
        self.review_row: Optional[StatusRow] = None
        self._review_layout = rv
        self.review_box.hide()
        body.addWidget(self.review_box)
        lay.addWidget(self.details)

        # —— 回答主体 ——
        self.answer = QWidget()
        self._answer_layout = QVBoxLayout(self.answer)
        self._answer_layout.setContentsMargins(2, 2, 2, 0)
        self._answer_layout.setSpacing(6)
        self.placeholder = label(f"◦  {self._waiting_text}", "Muted")
        self._answer_layout.addWidget(self.placeholder)
        self._answer_body: Optional[QWidget] = None
        lay.addWidget(self.answer)

        self._tail = QVBoxLayout()
        self._tail.setSpacing(8)
        lay.addLayout(self._tail)

        self._timer = QTimer(self)  # 刷新折叠标题里的实时状态与等待动画
        self._timer.setInterval(120)
        self._timer.timeout.connect(self._refresh)
        self._timer.start()
        self._refresh()

    @property
    def all_rows(self) -> list[StatusRow]:
        return [*self.worker_rows, *self.leader_rows] + ([self.review_row] if self.review_row else [])

    # —— 折叠标题：各 Agent 的状态摘要，如「✓ Claude 3.8s · ⠋ Codex 2.1s」——
    def _refresh(self) -> None:
        chips = [row.chip_html(prefix="📎 ") for row in self.pre_rows.values()]
        chips += [row.chip_html() for row in self.worker_rows if row.started or not self.pre_rows]
        judge_rows = [r for r in [*self.leader_rows, self.review_row] if r is not None and r.started]
        if judge_rows:
            chips.append(judge_rows[-1].chip_html(prefix="⚖ "))
        prefix = f"<span style='color:{C['muted']}'>{t('card.process')}</span>"
        if self._round_text:
            prefix += f" <span style='color:{C['faint']}'>· {self._round_text}</span>"
        self.details.set_summary(prefix + "&nbsp;&nbsp;&nbsp;" + "&nbsp;&nbsp;·&nbsp;&nbsp;".join(chips))
        if self.placeholder.isVisible():
            self._frame = (self._frame + 1) % len(SPINNER)
            self.placeholder.setText(f"{SPINNER[self._frame]}  {self._waiting_text}")

    def _stop(self) -> None:
        self._timer.stop()
        self._refresh()
        self.placeholder.setText(f"◦  {self._waiting_text}")

    def add_local_files(self, records: list[dict]) -> None:
        """本地读取的文本附件 / 无法解析的附件：在前置解析区显示一行说明。"""
        for rec in records:
            if rec.get("processor") == "local":
                text = t("card.pre_local", files=", ".join(rec["files"]))
            elif rec.get("processor") is None:
                text = t("card.pre_unsupported", msg=rec.get("error", ""))
            else:
                continue
            self.pre_box.show()
            self._pre_layout.addWidget(label(text, "Faint", wrap=True))

    # —— engine 事件 ——
    def on_event(self, kind: str, p: dict) -> None:
        if kind == "preprocess_start":
            self.pre_box.show()
            row = StatusRow(p["spec"].name, C["accent_hi"])
            self.pre_rows[p["index"]] = row
            self._pre_layout.addWidget(row)
            row.start(t("card.pre_reading", files=", ".join(p["files"])))
            self.stage.setText(t("card.stage_pre"))
            self._waiting_text = t("card.pre_wait")
        elif kind == "preprocess_done":
            r = p["result"]
            row = self.pre_rows[p["index"]]
            if r.ok:
                row.succeed(t("card.pre_done", n=len(r.output)), r.elapsed, r.output)
            else:
                row.fail(r)
            if all(not rw.running for rw in self.pre_rows.values()):
                self._waiting_text = t("card.waiting_answer")
                self.stage.setText(t("card.stage1"))
        elif kind == "round_start":  # Co-work
            self._round_text = f"R{p['round']}/{p['total']}"
            self.stage.setText(t("card.round_stage", r=p["round"], total=p["total"], phase=t(f"phase.{p['phase']}")))
        elif kind == "worker_start":
            if "round" in p:
                key = {"independent": "card.r_thinking", "peer_review": "card.r_reviewing",
                       "guided": "card.r_guided"}[p["phase"]]
                self.worker_rows[p["index"]].start(t(key, r=p["round"], total=p["total"]))
            else:
                self.worker_rows[p["index"]].start()
        elif kind == "worker_done":
            r: AgentResult = p["result"]
            row = self.worker_rows[p["index"]]
            if not r.ok:
                row.fail(r)
            elif "round" in p:  # Co-work：每一轮的答案都追加到详情中
                row.succeed(t("card.r_done", r=p["round"], n=len(r.output)), r.elapsed,
                            f"#### {t('card.round_detail', r=p['round'])}\n\n{r.output}")
            else:
                row.succeed(t("card.done_chars", n=len(r.output)), r.elapsed, r.output)
        elif kind == "workers_finished":
            if not p["ok"]:
                self.leader_row.skip(t("card.no_answers"))
        elif kind == "leader_start":
            row = self.leader_rows[-1]
            self.stage.setText(t("card.stage2"))
            if "round" in p:
                row.name.setText(t("card.leader_round", name=self._leader_name, r=p["round"]))
                row.start(t("card.summarizing", n=p["n_answers"], r=p["round"]))
            else:
                row.start(t("card.judging", n=p["n_answers"]))
        elif kind == "leader_done":
            r = p["result"]
            row = self.leader_rows[-1]
            if "round" in p:
                row.name.setText(t("card.leader_round", name=self._leader_name, r=p["round"]))
            if r.ok:
                if self._first_verdict is None:
                    self._first_verdict = r
                self._last_verdict = r
                row.succeed(self._verdict_status(r), r.elapsed, self._verdict_detail(r))
                self._show_verdict(r)
                if p.get("will_extend"):
                    self._dim_verdict(t("card.dim_extend", r=p["round"] + 1))
            else:
                row.fail(r)
        elif kind == "extension":  # Co-work：确信度低 → 下发争议指导，追加一轮讨论
            if self._last_verdict:  # 被替换的答案仍可在原 Leader 行展开查看
                self.leader_rows[-1].add_detail(f"#### {t('card.first_answer')}\n\n{self._final_text(self._last_verdict)}")
            guide = StatusRow(t("card.guidance_row"), C["orange"])
            guide._finish("↻", C["orange"], t("card.guidance_status", r=p["round"]), None)
            guide.add_detail(p["guidance"])
            self._leader_box.addWidget(guide)
            nxt = StatusRow(self._leader_name, C["yellow"])
            nxt.status.setText(t("card.wait_discussion"))
            self.leader_rows.append(nxt)
            self._leader_box.addWidget(nxt)
        elif kind == "review_start":
            spec = p["spec"]
            self.reviewer = SimpleNamespace(name=spec.name, vendor=spec.vendor, fallback=p["fallback"])
            self.stage.setText(t("card.stage3"))
            self.review_note.setText(t("card.review_fallback", name=spec.name) if p["fallback"]
                                     else t("card.review_hetero", name=spec.name, vendor=spec.vendor))
            self.review_row = StatusRow(spec.name, C["orange"])
            self._review_layout.addWidget(self.review_row)
            self.review_box.show()
            self.review_row.start(t("card.reviewing"))
            self._dim_verdict(t("card.dim_note", name=spec.name))
        elif kind == "review_done":
            r = p["result"]
            if r.ok:
                self.review_row.succeed(self._verdict_status(r), r.elapsed, self._verdict_detail(r))
                if self._first_verdict:  # 首轮答案被替换后仍可在 Leader 行展开查看
                    self.leader_row.add_detail(f"#### {t('card.first_answer')}\n\n{self._final_text(self._first_verdict)}")
                self._show_verdict(r)
            else:
                self.review_row.fail(r)
                if self._first_verdict:
                    self._show_verdict(self._first_verdict)
                self._tail.addWidget(banner(t("card.review_failed"), C["orange"]))
        self._refresh()

    # —— 裁决 ——
    @staticmethod
    def _conf_text(r: AgentResult) -> str:
        c = r.extra.get("confidence")
        return conf(c) if c else t("card.unparsed")

    @staticmethod
    def _final_text(r: AgentResult) -> str:
        return r.extra.get("final_answer") or r.output

    def _verdict_status(self, r: AgentResult) -> str:
        text = t("card.done_conf", conf=self._conf_text(r))
        score = r.extra.get("consensus_score")
        if r.extra.get("quorum") is False:
            text += " · " + t("card.no_quorum")
        elif score is not None:
            text += " · " + t("card.consensus", score=SCORE_TEXT[score], dot=SCORE_DOTS[score])
        return text

    @staticmethod
    def _verdict_detail(r: AgentResult) -> str:
        """Leader / 复审行的详情：匿名标签对照 + 评审分析（结构化输出时）或原始输出。"""
        legend = ""
        if r.extra.get("labels"):
            pairs = " · ".join(f"**{k}** = {v}" for k, v in sorted(r.extra["labels"].items()))
            legend = f"*{t('card.labels', pairs=pairs)}*\n\n"
        if r.extra.get("analysis"):
            return f"{legend}#### {t('card.analysis')}\n\n{r.extra['analysis']}"
        if r.extra.get("structured") is False and r.extra.get("mode") != "cowork":
            return f"{legend}#### {t('card.raw_output')}\n\n{r.output}"
        return legend + r.output

    def _show_verdict(self, verdict: AgentResult) -> None:
        """渲染（或替换）回答主体：评分徽章 + 最终答案。"""
        self.placeholder.hide()
        if self._answer_body is not None:
            self._answer_body.deleteLater()
        level = verdict.extra.get("confidence")
        score = verdict.extra.get("consensus_score")

        body = QWidget()
        v = QVBoxLayout(body)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(6)
        head = QHBoxLayout()
        head.setSpacing(6)
        if verdict.extra.get("mode") == "cowork":
            who = t("card.cowork_by", name=verdict.name, r=verdict.extra.get("round", "?"))
        elif verdict.extra.get("reviewed"):
            who = t("card.reviewed_by", name=verdict.name)
        else:
            who = t("card.judge_by", name=verdict.name)
        head.addWidget(shrinkable(label(who, "Faint")), 1)
        if verdict.extra.get("quorum") is False:
            qb = label(t("card.no_quorum"))
            qb.setStyleSheet(f"color: {C['muted']}; border: 1px solid {C['muted']}; border-radius: 9px;"
                             f" padding: 2px 9px; font-size: 12px; font-weight: 600;")
            qb.setToolTip(t("card.no_quorum_tip"))
            head.addWidget(qb)
        elif score is not None:
            color = SCORE_COLORS[score]
            sb = label(t("card.consensus", score=SCORE_TEXT[score], dot=SCORE_DOTS[score]))
            sb.setStyleSheet(f"color: {color}; border: 1px solid {color}; border-radius: 9px;"
                             f" padding: 2px 9px; font-size: 12px; font-weight: 700;")
            sb.setToolTip(t("card.consensus_tip"))
            head.addWidget(sb)
        color = CONF_COLORS.get(level, C["muted"])
        badge = label(t("card.badge", conf=conf(level)))
        badge.setStyleSheet(f"color: {color}; border: 1px solid {color}; border-radius: 9px;"
                            f" padding: 2px 9px; font-size: 12px; font-weight: 600;")
        head.addWidget(badge)
        v.addLayout(head)
        if verdict.extra.get("reviewed") and verdict.extra.get("first_confidence"):
            v.addWidget(label(t("card.first_note", leader=verdict.extra.get("first_leader", "Leader"),
                                conf=conf(verdict.extra["first_confidence"])), "Faint", wrap=True))
        self.verdict_note = label("", "Muted", wrap=True)
        self.verdict_note.hide()
        v.addWidget(self.verdict_note)
        md = MarkdownView(self._final_text(verdict))
        md.document().setDefaultFont(md.font())
        v.addWidget(md)
        self._answer_body = body
        self._answer_layout.addWidget(body)

    def _dim_verdict(self, note: str) -> None:
        if self._answer_body is None:
            return
        eff = QGraphicsOpacityEffect(self._answer_body)
        eff.setOpacity(0.45)
        self._answer_body.setGraphicsEffect(eff)
        self.verdict_note.setText(f"⟳ {note}")
        self.verdict_note.show()

    # —— 结束状态 ——
    def finish(self, outcome: RunOutcome, total: Optional[float] = None) -> None:
        self._stop()
        total = time.monotonic() - self._t0 if total is None else total
        s = f"{total:.1f}"
        if outcome.code == EXIT_NO_WORKERS:
            self.placeholder.hide()
            self.stage.setText(t("card.stage_failed", s=s))
            self._tail.addWidget(banner(t("card.failed_all"), C["red"]))
            self.details.set_expanded(True)  # 出错时自动展开过程，方便查看原因
        elif outcome.code == EXIT_LEADER_FAILED:
            self.placeholder.hide()
            self.stage.setText(t("card.stage_leader_failed", s=s))
            self._tail.addWidget(banner(t("card.failed_leader"), C["red"]))
            self.details.set_expanded(True)
        elif outcome.mode == "cowork":
            self.stage.setText(t("card.stage_cowork_done", r=len(outcome.rounds), s=s))
        else:
            self.stage.setText(t("card.stage_done", s=s))

    def _replay_cowork(self, outcome: RunOutcome) -> None:
        """按轮次重放 Co-work 讨论：各轮答案 → 每次 Leader 总结 → 争议指导 → ……"""
        verdict_at = {v.extra.get("round"): k for k, v in enumerate(outcome.leader_rounds)}
        last = len(outcome.leader_rounds) - 1
        for n, rnd in enumerate(outcome.rounds, 1):
            self._round_text = f"R{n}/{len(outcome.rounds)}"
            for r in rnd:
                self.on_event("worker_done", {"index": self._worker_index.get(r.name, 0), "result": r, "round": n})
            if n in verdict_at:
                k = verdict_at[n]
                self.on_event("leader_done", {"result": outcome.leader_rounds[k], "round": n, "will_extend": k < last})
                if k < len(outcome.guidance):
                    self.on_event("extension", {"round": n + 1, "guidance": outcome.guidance[k]})
        if outcome.code == EXIT_NO_WORKERS:
            self.on_event("workers_finished", {"ok": []})

    def replay_preprocess(self, records: list[dict]) -> None:
        self.add_local_files(records)
        for i, rec in enumerate(r for r in records if r.get("processor") not in (None, "local")):
            spec = SimpleNamespace(name=rec["processor"])
            self.on_event("preprocess_start", {"index": i, "spec": spec, "files": rec["files"]})
            res = AgentResult(rec["processor"], ok=rec.get("ok", False), output=rec.get("output", ""),
                              error=rec.get("error", ""), elapsed=rec.get("elapsed", 0.0))
            self.on_event("preprocess_done", {"index": i, "spec": spec, "files": rec["files"], "result": res})

    def replay(self, record: dict) -> None:
        """从历史记录重建卡片的最终状态。"""
        if record.get("preprocess"):
            self.replay_preprocess(record["preprocess"])
        outcome = record_to_outcome(record)
        if outcome.mode == "cowork":
            self._replay_cowork(outcome)
        else:
            for i, r in enumerate(outcome.results):
                self.on_event("worker_done", {"index": i, "result": r})
            self.on_event("workers_finished", {"ok": [r for r in outcome.results if r.ok]})
            if outcome.first_verdict:
                self.on_event("leader_done", {"result": outcome.first_verdict})
            rv = record.get("reviewer")
            if outcome.review and rv:
                spec = SimpleNamespace(name=rv["name"], vendor=rv.get("vendor", ""))
                self.on_event("review_start", {"spec": spec, "fallback": rv.get("fallback", False)})
                self.on_event("review_done", {"result": outcome.review})
        self.finish(outcome, record.get("total_elapsed", 0.0))
        if record.get("created_at"):
            self.stage.setText(self.stage.text() + time.strftime(" · %m-%d %H:%M", time.localtime(record["created_at"])))

    def cancelled(self) -> None:
        for row in self.all_rows:
            row.cancel()
        self._stop()
        self.placeholder.hide()
        self.stage.setText(t("card.stopped"))
        self._tail.addWidget(banner(t("card.stopped_banner"), C["muted"]))

    def error(self, message: str) -> None:
        for row in self.all_rows:
            row.cancel()
        self._stop()
        self.placeholder.hide()
        self.stage.setText(t("card.error"))
        self._tail.addWidget(banner(message, C["red"]))


class ChatView(QScrollArea):
    """一个会话的消息流；新内容出现时，若用户停留在底部则自动跟随滚动。"""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        container = QWidget()
        container.setObjectName("ChatContainer")
        self._lay = QVBoxLayout(container)
        self._lay.setContentsMargins(28, 20, 28, 20)
        self._lay.setSpacing(14)

        self.placeholder = QWidget()
        ph = QVBoxLayout(self.placeholder)
        ph.addStretch(1)
        for text, obj, size in (("⚖", "", 40), (t("empty.title"), "HeaderTitle", 0), (t("empty.sub"), "Muted", 0)):
            lb = label(text, obj, wrap=True)
            lb.setAlignment(Qt.AlignmentFlag.AlignCenter)
            if size:
                lb.setStyleSheet(f"font-size: {size}px; color: {C['faint']};")
            ph.addWidget(lb)
        ph.addStretch(1)
        self._lay.addWidget(self.placeholder, 1)
        self._lay.addStretch(0)
        self.setWidget(container)

        self._follow = True
        bar = self.verticalScrollBar()
        bar.valueChanged.connect(lambda v: setattr(self, "_follow", v >= bar.maximum() - 40))
        bar.rangeChanged.connect(lambda _lo, hi: self._follow and bar.setValue(hi))

    @property
    def is_empty(self) -> bool:
        return self._lay.count() <= 2

    def _append(self, widget: QWidget) -> None:
        if self.is_empty:
            self.placeholder.hide()
            self._lay.setStretch(1, 1)  # 占位隐藏后，用底部弹簧把消息顶到上方
        self._follow = True
        self._lay.insertWidget(self._lay.count() - 1, widget)

    def add_user(self, text: str, attachments: Optional[list[str]] = None) -> None:
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(80, 0, 0, 0)
        h.addStretch(1)
        if attachments:
            files = "  ".join(f"{KIND_ICONS.get(file_kind(a), '📎')} {Path(a).name}" for a in attachments)
            text = f"{text}\n\n{files}" if text else files
        bubble = label(text, "UserBubble", wrap=True)
        bubble.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        # 自动换行的 QLabel 默认会缩得很窄，按文本宽度给出最小宽度
        longest = max(bubble.fontMetrics().horizontalAdvance(ln) for ln in text.splitlines() or [""])
        bubble.setMinimumWidth(min(longest + 30, 620))
        bubble.setMaximumWidth(620)
        h.addWidget(bubble)
        self._append(row)

    def add_card(self, card: QWidget) -> None:
        self._append(card)


class InputEdit(QTextEdit):
    """多行输入：Enter 发送，Shift+Enter 换行；高度随内容增长。"""

    submitted = Signal()

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setAcceptRichText(False)
        self.setPlaceholderText(t("input.placeholder"))
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.document().documentLayout().documentSizeChanged.connect(self._fit)
        self._fit()

    def _fit(self, *_) -> None:
        h = math.ceil(self.document().size().height()) + 6
        self.setFixedHeight(max(34, min(h, 180)))

    def keyPressEvent(self, e: QKeyEvent) -> None:  # noqa: N802
        if e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not (
            e.modifiers() & Qt.KeyboardModifier.ShiftModifier
        ):
            self.submitted.emit()
            return
        super().keyPressEvent(e)


class AudioRecorder(QObject):
    """用 QtMultimedia 录音并保存为 16-bit PCM WAV（无需额外依赖）。

    macOS 会在首次录音时向「负责的应用」（如 Terminal / iTerm / VS Code）申请麦克风权限。"""

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._source: Optional[QAudioSource] = None
        self._io = None
        self._buf = bytearray()
        self._fmt: Optional[QAudioFormat] = None
        self.started_at = 0.0

    @property
    def recording(self) -> bool:
        return self._source is not None

    def start(self) -> Optional[str]:
        """开始录音；失败时返回错误信息。"""
        device = QMediaDevices.defaultAudioInput()
        if device.isNull():
            return t("rec.no_device")
        fmt = QAudioFormat()
        fmt.setSampleRate(16000)
        fmt.setChannelCount(1)
        fmt.setSampleFormat(QAudioFormat.SampleFormat.Int16)
        if not device.isFormatSupported(fmt):
            fmt = device.preferredFormat()
        self._fmt = fmt
        self._buf = bytearray()
        self._source = QAudioSource(device, fmt, self)
        self._io = self._source.start()
        if self._io is None:
            self._source = None
            return t("rec.failed", e="QAudioSource.start() returned None")
        self._io.readyRead.connect(self._read)
        self.started_at = time.monotonic()
        return None

    def _read(self) -> None:
        if self._io is not None:
            self._buf += bytes(self._io.readAll().data())

    def stop(self, directory: Path = RECORDINGS_DIR) -> tuple[Optional[str], Optional[str]]:
        """停止并写入 WAV。返回 (文件路径, 警告信息)。"""
        if self._source is None:
            return None, None
        self._read()
        self._source.stop()
        self._source, self._io = None, None
        return write_wav(bytes(self._buf), self._fmt, directory)


def write_wav(raw: bytes, fmt: QAudioFormat, directory: Path) -> tuple[Optional[str], Optional[str]]:
    """把 Qt 采集到的原始 PCM 写成 16-bit WAV；Float / Int32 样本会转换为 Int16。"""
    sf = fmt.sampleFormat()
    if sf == QAudioFormat.SampleFormat.Float:
        floats = array.array("f", raw[: len(raw) // 4 * 4])
        samples = array.array("h", (max(-32768, min(32767, int(x * 32767))) for x in floats))
    elif sf == QAudioFormat.SampleFormat.Int32:
        ints = array.array("i", raw[: len(raw) // 4 * 4])
        samples = array.array("h", (x >> 16 for x in ints))
    elif sf == QAudioFormat.SampleFormat.UInt8:
        samples = array.array("h", ((b - 128) << 8 for b in raw))
    else:
        samples = array.array("h", raw[: len(raw) // 2 * 2])
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{time.strftime('voice-%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}.wav"
    with wave.open(str(path), "wb") as w:
        w.setnchannels(fmt.channelCount())
        w.setsampwidth(2)
        w.setframerate(fmt.sampleRate())
        w.writeframes(samples.tobytes())
    peak = max((abs(s) for s in samples), default=0)
    return str(path), (t("rec.silent") if peak < 50 else None)


class AttachmentChip(QFrame):
    """输入框上方的附件标签：图标 + 文件名 + 删除按钮。"""

    removed = Signal(str)

    def __init__(self, path: str, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("Chip")
        self.path = path
        h = QHBoxLayout(self)
        h.setContentsMargins(10, 3, 4, 3)
        h.setSpacing(4)
        name = Path(path).name
        lb = label(f"{KIND_ICONS.get(file_kind(path), '📎')}  {name if len(name) <= 28 else name[:25] + '…'}")
        lb.setStyleSheet("font-size: 12px;")
        lb.setToolTip(path)
        h.addWidget(lb)
        x = QToolButton()
        x.setText("✕")
        x.setToolTip(t("att.remove"))
        x.setStyleSheet("padding: 0 4px; font-size: 11px;")
        x.clicked.connect(lambda: self.removed.emit(self.path))
        h.addWidget(x)


# ---------------------------------------------------------------------------
# 账户与用量
# ---------------------------------------------------------------------------


def usage_color(percent: Optional[float], hot: bool) -> str:
    if percent is None:
        return C["faint"]
    if hot or percent >= 100:
        return C["red"]
    return C["orange"] if percent >= 80 else C["green"]


class ProviderRow(QFrame):
    """一个厂商的账户 / 配额卡片。"""

    refresh_clicked = Signal(str)
    login_clicked = Signal(str)

    def __init__(self, provider: str, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("Provider")
        self.provider = provider
        self.status: Optional[ProviderStatus] = None
        title, _cli = PROVIDERS[provider]

        v = QVBoxLayout(self)
        v.setContentsMargins(10, 8, 10, 8)
        v.setSpacing(4)

        head = QHBoxLayout()
        head.setSpacing(6)
        dot = label("●")
        dot.setStyleSheet(f"color: {PROVIDER_COLORS[provider]}; font-size: 10px;")
        head.addWidget(dot)
        name = shrinkable(label(title))
        name.setStyleSheet("font-weight: 600; font-size: 12px;")
        name.setToolTip(title)
        head.addWidget(name, 1)
        self.pill = label("…", "Pill")
        head.addWidget(self.pill)
        v.addLayout(head)

        self.account = label("", "Faint", wrap=True)
        v.addWidget(self.account)
        self.limit_banner = label("", wrap=True)
        self.limit_banner.setStyleSheet(f"color: {C['red']}; font-weight: 600; font-size: 12px;")
        self.limit_banner.hide()
        v.addWidget(self.limit_banner)

        self.bars = QVBoxLayout()
        self.bars.setSpacing(6)
        v.addLayout(self.bars)

        self.source = label("", "Faint", wrap=True)
        v.addWidget(self.source)
        self.note = label("", "Faint", wrap=True)
        self.note.hide()
        v.addWidget(self.note)

        btns = QHBoxLayout()
        btns.addStretch(1)
        self.refresh_btn = QPushButton("↻")
        self.refresh_btn.setObjectName("SmallBtn")
        self.refresh_btn.setToolTip(t("acct.refresh_tip_claude") if provider == "anthropic" else t("acct.refresh_tip"))
        self.refresh_btn.clicked.connect(lambda: self.refresh_clicked.emit(self.provider))
        self.login_btn = QPushButton(t("acct.login"))
        self.login_btn.setObjectName("SmallBtn")
        self.login_btn.setToolTip(t("acct.login_tip", cmd=accounts.LOGIN_COMMANDS[provider]))
        self.login_btn.clicked.connect(lambda: self.login_clicked.emit(self.provider))
        btns.addWidget(self.refresh_btn)
        btns.addWidget(self.login_btn)
        v.addLayout(btns)

    def _pill(self, text: str, color: str) -> None:
        self.pill.setText(text)
        self.pill.setStyleSheet(f"color: {color}; border: 1px solid {color};")

    def set_busy(self, busy: bool) -> None:
        self.refresh_btn.setEnabled(not busy)
        self.refresh_btn.setText("…" if busy else "↻")
        if busy:
            self._pill(t("acct.refreshing"), C["muted"])

    def show_note(self, text: str) -> None:
        self.note.setText(text)
        self.note.setVisible(bool(text))

    def _window_block(self, w, st: ProviderStatus) -> QWidget:
        """一个配额窗口：上面一行「名称 …… 百分比 · 重置时间」，下面是进度条。"""
        pct = 0.0 if w.expired else (w.used_percent or 0.0)
        # 厂商处于限额时，接近满的窗口也标红（日志可能滞后于真实状态）
        color = usage_color(pct, st.limited and pct >= 90)
        block = QWidget()
        bv = QVBoxLayout(block)
        bv.setContentsMargins(0, 0, 0, 0)
        bv.setSpacing(2)
        top = QHBoxLayout()
        top.setSpacing(6)
        top.addWidget(label(w.label, "Faint"))
        if w.expired:
            txt = t("acct.reset_done")
        else:
            txt = f"{pct:.0f}%" + (f" · {t('acct.resets', t=fmt_reset(w.resets_at))}" if w.resets_at else "")
        val = shrinkable(label(txt, "Faint"))
        val.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        val.setStyleSheet(f"color: {color if pct >= 80 and not w.expired else C['muted']}; font-size: 11px;")
        top.addWidget(val, 1)
        bv.addLayout(top)
        bar = QProgressBar()
        bar.setRange(0, 100)
        bar.setTextVisible(False)
        bar.setFixedHeight(6)
        bar.setValue(int(min(pct, 100)))
        bar.setStyleSheet(f"QProgressBar::chunk {{ background: {color}; border-radius: 3px; }}")
        bv.addWidget(bar)
        block.setToolTip(t("acct.window_tip", label=w.label, pct=f"{pct:.1f}")
                         + (f" · {t('acct.resets', t=fmt_reset(w.resets_at))}" if w.resets_at else ""))
        return block

    def render(self, st: Optional[ProviderStatus] = None) -> None:
        st = st or self.status
        if st is None:
            return
        self.status = st
        if not st.installed:
            self._pill(t("acct.not_installed"), C["faint"])
        elif st.limited:
            self._pill(t("acct.limited"), C["red"])
        elif st.logged_in:
            self._pill(st.status_text or t("acct.logged_in"), C["green"])
        elif st.logged_in is False:
            self._pill(st.status_text or t("acct.logged_out"), C["orange"])
        else:
            self._pill(st.status_text or t("acct.unknown"), C["muted"])

        acct = " · ".join(x for x in (st.account, st.plan) if x)
        if st.error:
            acct = (acct + "\n" if acct else "") + f"⚠ {st.error}"
        self.account.setText(acct or "—")

        if st.limited:
            until = fmt_reset(st.limit_reset)
            self.limit_banner.setText(t("acct.limited_until", t=until) if until else t("acct.limited_plain"))
            self.limit_banner.show()
        else:
            self.limit_banner.hide()

        clear_layout(self.bars)
        for w in st.windows:
            self.bars.addWidget(self._window_block(w, st))
        self.source.setText(st.source)


class AccountsSection(QWidget):
    """配置面板中可折叠的「Account & Usage」区域。"""

    PROVIDER_ORDER = ("anthropic", "openai", "google")

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(6)
        self.toggle = QToolButton()
        self.toggle.setCheckable(True)
        self.toggle.setChecked(True)
        self.toggle.setStyleSheet(f"color: {C['muted']}; font-size: 11px; font-weight: 600; padding: 2px 0;")
        self.toggle.toggled.connect(self._on_toggle)
        v.addWidget(self.toggle)
        self.body = QWidget()
        b = QVBoxLayout(self.body)
        b.setContentsMargins(0, 0, 0, 0)
        b.setSpacing(6)
        self.rows: dict[str, ProviderRow] = {}
        for p in self.PROVIDER_ORDER:
            row = ProviderRow(p)
            row.refresh_clicked.connect(lambda prov: self.refresh(prov, with_usage=True))
            row.login_clicked.connect(self.login)
            self.rows[p] = row
            b.addWidget(row)
        v.addWidget(self.body)
        self._on_toggle(True)
        self._tasks: dict[str, asyncio.Task] = {}

        # 每分钟重绘（限额到期后自动解除），每 5 分钟重新读取 Codex 本地日志（不发请求）
        self._tick = QTimer(self)
        self._tick.setInterval(60_000)
        self._tick.timeout.connect(self._on_tick)
        self._tick.start()
        self._ticks = 0

    def _on_toggle(self, on: bool) -> None:
        self.body.setVisible(on)
        # QToolButton 会把单个 & 当作快捷键标记，需写成 &&
        self.toggle.setText(("▾  " if on else "▸  ") + t("acct.section").replace("&", "&&"))

    def _on_tick(self) -> None:
        self._ticks += 1
        for row in self.rows.values():
            row.render()
        if self._ticks % 5 == 0:
            self.refresh("openai")

    def refresh_all(self) -> None:
        for p in self.PROVIDER_ORDER:
            self.refresh(p)

    def refresh(self, provider: str, with_usage: bool = False) -> Optional[asyncio.Task]:
        """with_usage 仅对 Anthropic 有意义：发送一条极小请求获取实时配额。"""
        if provider in self._tasks and not self._tasks[provider].done():
            return self._tasks[provider]
        kwargs = {"with_usage": True} if (with_usage and provider == "anthropic") else {}
        task = asyncio.ensure_future(self._refresh(provider, kwargs))
        self._tasks[provider] = task
        return task

    async def _refresh(self, provider: str, kwargs: dict) -> None:
        row = self.rows[provider]
        row.set_busy(True)
        try:
            st = await accounts.fetch(provider, **kwargs)
        except Exception as e:
            title, cli = PROVIDERS[provider]
            st = ProviderStatus(provider, title, cli, installed=True, error=t("acct.fetch_failed", e=repr(e)))
        finally:
            row.set_busy(False)
        row.render(st)

    def cancel_all(self) -> None:
        for task in self._tasks.values():
            if not task.done():
                task.cancel()
        self._tick.stop()

    def note_failure(self, vendor: str, error: str) -> None:
        """Agent 运行失败时调用：若是限额错误，记录解除时间并立即标红。"""
        until = parse_limit_message(error)
        if not until or vendor not in self.rows:
            return
        accounts.record_limit(vendor, until, error)
        row = self.rows[vendor]
        if row.status is not None:
            row.status.limited_until = max(row.status.limited_until or 0, until)
            row.render()

    def login(self, provider: str) -> None:
        row = self.rows[provider]
        try:
            argv, _script = accounts.open_login_terminal(provider)
        except Exception as e:
            row.show_note(t("acct.terminal_fail", e=e))
            return
        row.show_note(t("acct.terminal_opened", app=argv[2]))


# ---------------------------------------------------------------------------
# 右侧配置面板
# ---------------------------------------------------------------------------


@dataclass
class RunSettings:
    workers: list[AgentSpec]
    leader: AgentSpec
    mode: str                 # judge / cowork
    review: bool              # Judge：低确信度时二次复审
    pool: list[AgentSpec]     # Judge：复审候选池
    rounds: int               # Co-work：基础轮数
    max_rounds: int           # Co-work：最多延长到的轮数
    extend: bool              # Co-work：低确信度时延长讨论



class ConfigPanel(QFrame):
    reload_requested = Signal()
    WIDTH = 300

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("ConfigPanel")
        self.setMinimumWidth(0)
        self.setMaximumWidth(self.WIDTH)
        self.cfg: Optional[Config] = None
        self.checks: dict[str, QCheckBox] = {}

        self._inner = QWidget()
        self._inner.setFixedWidth(self.WIDTH)
        inner_v = QVBoxLayout(self._inner)
        inner_v.setContentsMargins(0, 0, 0, 0)
        inner_v.setSpacing(0)

        # 可滚动的主体
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget()
        body.setObjectName("PanelBody")
        lay = QVBoxLayout(body)
        lay.setContentsMargins(18, 16, 16, 12)
        lay.setSpacing(8)
        scroll.setWidget(body)
        inner_v.addWidget(scroll, 1)

        lay.addWidget(label(t("panel.title"), "AppTitle"))
        self.path_label = label("", "Faint", wrap=True)
        lay.addWidget(self.path_label)
        self.error = label("", wrap=True)
        self.error.setStyleSheet(f"color: {C['red']}; border: 1px solid {C['red']}; border-radius: 8px; padding: 8px;")
        self.error.hide()
        lay.addWidget(self.error)

        lay.addSpacing(6)
        lay.addWidget(label(t("mode.section"), "Section"))
        self.mode_combo = QComboBox()
        self.mode_combo.addItem(t("mode.judge"), "judge")
        self.mode_combo.addItem(t("mode.cowork"), "cowork")
        lay.addWidget(self.mode_combo)
        self.mode_hint = label("", "Faint", wrap=True)
        lay.addWidget(self.mode_hint)
        self.mode_combo.currentIndexChanged.connect(self._update_hints)

        lay.addSpacing(10)
        lay.addWidget(label("WORKERS", "Section"))
        self.workers_box = QVBoxLayout()
        self.workers_box.setSpacing(2)
        lay.addLayout(self.workers_box)

        self.pool_label = label("", "Faint", wrap=True)
        self.pool_label.setToolTip(t("pool.tip"))
        lay.addWidget(self.pool_label)
        self._pool_timer = QTimer(self)
        self._pool_timer.setInterval(1500)
        self._pool_timer.timeout.connect(self._update_pool_label)
        self._pool_timer.start()

        lay.addSpacing(10)
        lay.addWidget(label("LEADER", "Section"))
        self.leader_combo = QComboBox()
        self.leader_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        lay.addWidget(self.leader_combo)
        self.leader_cmd = label("", "Faint", wrap=True)
        lay.addWidget(self.leader_cmd)
        self.leader_combo.currentIndexChanged.connect(self._update_hints)
        self.leader_combo.currentIndexChanged.connect(lambda _i: self.request_pool_sync())

        lay.addSpacing(10)
        # Judge 模式：二次复审；Co-work 模式：延长讨论（随模式切换显示）
        self.review_section = QWidget()
        rs = QVBoxLayout(self.review_section)
        rs.setContentsMargins(0, 0, 0, 0)
        rs.setSpacing(8)
        rs.addWidget(label(t("panel.second"), "Section"))
        self.review = QCheckBox(t("panel.review_cb"))
        self.review.toggled.connect(self._update_hints)
        rs.addWidget(self.review)
        self.review_hint = label("", "Faint", wrap=True)
        rs.addWidget(self.review_hint)
        lay.addWidget(self.review_section)

        self.discussion_section = QWidget()
        ds = QVBoxLayout(self.discussion_section)
        ds.setContentsMargins(0, 0, 0, 0)
        ds.setSpacing(8)
        ds.addWidget(label(t("panel.discussion"), "Section"))
        self.extend = QCheckBox()
        self.extend.toggled.connect(self._update_hints)
        ds.addWidget(self.extend)
        self.extend_hint = label("", "Faint", wrap=True)
        ds.addWidget(self.extend_hint)
        lay.addWidget(self.discussion_section)

        lay.addSpacing(12)
        self.accounts = AccountsSection()
        lay.addWidget(self.accounts)
        lay.addStretch(1)

        # 固定在底部的按钮
        bottom = QWidget()
        bottom.setObjectName("PanelBody")
        bv = QVBoxLayout(bottom)
        bv.setContentsMargins(18, 8, 18, 16)
        bv.setSpacing(8)
        self.saved = label("", "Faint")
        bv.addWidget(self.saved)
        btns = QHBoxLayout()
        self.save_btn = QPushButton(t("panel.save"))
        self.save_btn.setObjectName("Primary")
        self.save_btn.clicked.connect(self._save)
        reload_btn = QPushButton(t("panel.reload"))
        reload_btn.clicked.connect(self.reload_requested)
        btns.addWidget(self.save_btn)
        btns.addWidget(reload_btn)
        bv.addLayout(btns)
        open_btn = QPushButton(t("panel.open"))
        open_btn.clicked.connect(self._open_file)
        bv.addWidget(open_btn)
        inner_v.addWidget(bottom)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self._inner)

        self._anim = QPropertyAnimation(self, b"maximumWidth", self)
        self._anim.setDuration(220)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)

    # —— 折叠 ——
    def set_expanded(self, on: bool, animate: bool = True) -> None:
        self._anim.stop()
        if not animate:
            self.setMaximumWidth(self.WIDTH if on else 0)
            return
        self._anim.setStartValue(self.maximumWidth())
        self._anim.setEndValue(self.WIDTH if on else 0)
        self._anim.start()

    # —— 数据 ——
    def show_error(self, path: Path, message: str) -> None:
        self.cfg = None
        self.path_label.setText(str(path))
        self.error.setText(t("panel.error", msg=message))
        self.error.show()
        self.save_btn.setEnabled(False)

    def load(self, cfg: Config) -> None:
        self.cfg = cfg
        self.error.hide()
        self.save_btn.setEnabled(True)
        self.path_label.setText(str(cfg.path))
        self.saved.setText("")

        clear_layout(self.workers_box)
        self.checks.clear()
        self.model_combos: dict[str, QComboBox] = {}
        for i, spec in enumerate(cfg.workers):
            cb = QCheckBox(spec.name if spec.installed else t("panel.not_installed", name=spec.name, cli=spec.command[0]))
            cb.setChecked(spec.enabled and spec.installed)
            cb.setEnabled(spec.installed)
            cb.setIcon(color_dot(AGENT_COLORS[i % len(AGENT_COLORS)]))
            cb.setToolTip(shlex.join(spec.argv_template()))
            self.workers_box.addWidget(cb)
            self.checks[spec.name] = cb
            cb.toggled.connect(lambda _on: self.request_pool_sync())
            if spec.model_flag:
                combo = self._model_combo(spec)
                cb.toggled.connect(combo.setEnabled)
                combo.setEnabled(cb.isChecked())
                row = QHBoxLayout()
                row.setContentsMargins(26, 0, 0, 4)
                row.addWidget(combo)
                holder = QWidget()
                holder.setLayout(row)
                self.workers_box.addWidget(holder)

        self.leader_combo.blockSignals(True)
        self.leader_combo.clear()
        for spec in cfg.leaders:
            text = spec.name if spec.installed else t("panel.leader_not_installed", name=spec.name)
            self.leader_combo.addItem(text, spec.name)
        self.leader_combo.setCurrentIndex(max(0, self.leader_combo.findData(cfg.default_leader)))
        self.leader_combo.blockSignals(False)
        self.review.setChecked(cfg.review_on_low_confidence)
        self.request_pool_sync()
        self.mode_combo.blockSignals(True)
        self.mode_combo.setCurrentIndex(max(0, self.mode_combo.findData(cfg.mode)))
        self.mode_combo.blockSignals(False)
        self.extend.setText(t("panel.extend_cb"))
        self.extend.setChecked(cfg.cowork_extend)
        self.extend.setEnabled(cfg.cowork_max_rounds > cfg.cowork_rounds)
        self._update_hints()

    def _model_combo(self, spec: AgentSpec) -> QComboBox:
        combo = QComboBox()
        combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        combo.setMinimumContentsLength(12)
        combo.addItem(t("panel.model_default"), "")
        models = list(spec.available_models)
        if spec.selected_model and spec.selected_model not in models:
            models.append(spec.selected_model)
        for m in models:
            combo.addItem(m, m)
        combo.setCurrentIndex(max(0, combo.findData(spec.selected_model)))
        combo.setToolTip(t("panel.model_tip", name=spec.name, flag=spec.model_flag))
        combo.currentIndexChanged.connect(lambda _i, n=spec.name, c=combo: self._on_model_changed(n, c.currentData()))
        self.model_combos[spec.name] = combo
        return combo

    def _on_model_changed(self, worker: str, model: str) -> None:
        """切换模型：立即更新内存中的配置，并在后台线程写入 config.json。"""
        if not self.cfg:
            return
        spec = next(w for w in self.cfg.workers if w.name == worker)
        spec.selected_model = model or ""
        self.checks[worker].setToolTip(shlex.join(spec.argv_template()))
        asyncio.ensure_future(self._persist_model(worker, spec.selected_model))
        self.request_pool_sync()  # 换了模型：关闭旧进程，预热新模型的进程

    async def _persist_model(self, worker: str, model: str) -> None:
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, save_worker_model, self.cfg.path, worker, model)
        except (OSError, ConfigError) as e:
            self.saved.setText(t("panel.save_failed", e=e))
            return
        self.saved.setText(t("panel.model_saved", name=worker, model=model or t("panel.model_default")))
        log.info("[Config] %s model → %s", worker, model or "(CLI default)")

    def pool_specs(self) -> list[AgentSpec]:
        """当前需要常驻的 Agent：已勾选的 Workers + 选中的 Leader。"""
        if not self.cfg:
            return []
        specs = [w for w in self.cfg.workers if self.checks.get(w.name) and self.checks[w.name].isChecked()]
        if self.leader_combo.currentData():
            specs.append(self.cfg.get_leader(self.leader_combo.currentData()))
        return specs

    def request_pool_sync(self) -> None:
        """防抖：短时间内的多次改动只触发一次同步。"""
        if not hasattr(self, "_sync_timer"):
            self._sync_timer = QTimer(self)
            self._sync_timer.setSingleShot(True)
            self._sync_timer.setInterval(400)
            self._sync_timer.timeout.connect(lambda: asyncio.ensure_future(self._sync_pool()))
        self._sync_timer.start()

    async def _sync_pool(self) -> None:
        try:
            await POOL.sync([replace(s) for s in self.pool_specs()])
        except Exception:
            log.exception("pool sync failed")
        self._update_pool_label()

    def _update_pool_label(self) -> None:
        items = POOL.status()
        if not items:
            self.pool_label.setText(t("pool.none"))
            return
        marks = [f"{name} {'◐' if busy else ('●' if alive else '○')}" for name, alive, busy in items]
        self.pool_label.setText(t("pool.status", items=" · ".join(marks)))

    @property
    def mode(self) -> str:
        return self.mode_combo.currentData() or "judge"

    def _update_hints(self) -> None:
        if not (self.cfg and self.leader_combo.currentData()):
            return
        cowork = self.mode == "cowork"
        self.mode_hint.setText(t("mode.cowork_hint", rounds=self.cfg.cowork_rounds) if cowork else t("mode.judge_hint"))
        self.review_section.setVisible(not cowork)
        self.discussion_section.setVisible(cowork)
        self.extend_hint.setText(t("panel.extend_on", max=self.cfg.cowork_max_rounds)
                                 if self.extend.isChecked() and self.extend.isEnabled()
                                 else t("panel.extend_off", rounds=self.cfg.cowork_rounds))
        leader = self.cfg.get_leader(self.leader_combo.currentData())
        self.leader_cmd.setText(shlex.join(leader.argv_template()))
        if not self.review.isChecked():
            self.review_hint.setText(t("panel.review_off"))
            return
        reviewer = select_reviewer(self.cfg.leaders, leader)
        self.review_hint.setText(t("panel.reviewer", name=reviewer.name, vendor=reviewer.vendor) if reviewer
                                 else t("panel.reviewer_self", name=leader.name))

    def selection(self) -> "RunSettings":
        """当前面板上的运行设置（Agent 均为副本，运行中修改配置不影响本次运行）。"""
        assert self.cfg is not None
        return RunSettings(
            workers=[replace(w) for w in self.cfg.workers if self.checks[w.name].isChecked()],
            leader=replace(self.cfg.get_leader(self.leader_combo.currentData())),
            mode=self.mode,
            review=self.review.isChecked(),
            pool=[replace(s) for s in self.cfg.leaders],
            rounds=self.cfg.cowork_rounds,
            max_rounds=self.cfg.cowork_max_rounds,
            extend=self.extend.isChecked() and self.extend.isEnabled(),
        )

    def _save(self) -> None:
        if not self.cfg:
            return
        try:
            save_selection(self.cfg.path, {n: cb.isChecked() for n, cb in self.checks.items()},
                           self.leader_combo.currentData(), self.review.isChecked(),
                           mode=self.mode, cowork_extend=self.extend.isChecked())
        except (OSError, ConfigError) as e:
            self.saved.setText(t("panel.save_failed", e=e))
            return
        self.saved.setText(t("panel.saved", time=time.strftime("%H:%M:%S")))

    def _open_file(self) -> None:
        if self.cfg:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.cfg.path)))


# ---------------------------------------------------------------------------
# 主窗口
# ---------------------------------------------------------------------------


@dataclass
class Session:
    title: str
    view: ChatView
    item: QListWidgetItem
    db_id: Optional[int] = None      # 首次保存后才写入数据库
    loaded: bool = True              # 历史会话在首次打开时才渲染
    task: Optional[asyncio.Task] = None
    untitled: bool = True            # 仍是默认标题（首个问题会成为标题）
    turns: list = field(default_factory=list)  # [(用户问题, 最终答案)]：多轮对话的上下文

    @property
    def busy(self) -> bool:
        return self.task is not None and not self.task.done()


class MainWindow(QMainWindow):
    run_finished = Signal(object)              # RunOutcome / None，便于测试脚本等待
    language_requested = Signal(str)           # 用户点击了语言切换

    def __init__(self, config_path: Path = DEFAULT_CONFIG_PATH, db_path: Path = DEFAULT_DB_PATH,
                 select_db_id: Optional[int] = None):
        super().__init__()
        self.config_path = config_path
        self.store = HistoryStore(db_path)
        self.sessions: list[Session] = []
        self.setWindowTitle("OmniCouncil")
        self.resize(1240, 820)
        self.setMinimumSize(860, 540)

        root = QWidget()
        root.setObjectName("Main")
        h = QHBoxLayout(root)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(0)
        h.addWidget(self._build_sidebar())
        h.addWidget(self._build_center(), 1)
        self.panel = ConfigPanel()
        self.panel.reload_requested.connect(self.reload_config)
        h.addWidget(self.panel)
        self.setCentralWidget(root)

        self.attachments: list[str] = []
        self.recorder = AudioRecorder(self)
        self._rec_timer = QTimer(self)
        self._rec_timer.setInterval(250)
        self._rec_timer.timeout.connect(self._update_rec_hint)
        self.reload_config()
        self._load_history()
        self.new_session()
        if select_db_id is not None:
            for i, s in enumerate(self.sessions):
                if s.db_id == select_db_id:
                    self.session_list.setCurrentRow(i)
                    break
        QTimer.singleShot(0, self.panel.accounts.refresh_all)

    # —— 构建 ——
    def _build_sidebar(self) -> QWidget:
        side = QFrame()
        side.setObjectName("Sidebar")
        side.setFixedWidth(230)
        v = QVBoxLayout(side)
        v.setContentsMargins(12, 16, 12, 12)
        v.setSpacing(10)
        v.addWidget(label("  OmniCouncil", "AppTitle"))
        new_btn = QPushButton(t("sidebar.new"))
        new_btn.clicked.connect(self.new_session)
        v.addWidget(new_btn)
        v.addWidget(label(t("sidebar.history"), "Section"))
        self.session_list = QListWidget()
        self.session_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.session_list.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.session_list.currentRowChanged.connect(self._switch_session)
        self.session_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.session_list.customContextMenuRequested.connect(self._session_menu)
        v.addWidget(self.session_list, 1)
        self.footer = label("", "Faint", wrap=True)
        v.addWidget(self.footer)
        return side

    def _build_center(self) -> QWidget:
        center = QWidget()
        v = QVBoxLayout(center)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        header = QFrame()
        header.setObjectName("Header")
        hh = QHBoxLayout(header)
        hh.setContentsMargins(20, 10, 12, 10)
        self.header_title = shrinkable(label(t("session.new"), "HeaderTitle"))
        hh.addWidget(self.header_title, 1)
        self.lang_btn = QToolButton()
        self.lang_btn.setObjectName("LangBtn")
        self.lang_btn.setText(t("lang.switch"))
        self.lang_btn.setToolTip(t("lang.switch_tip"))
        self.lang_btn.clicked.connect(self._request_language)
        hh.addWidget(self.lang_btn)
        self.config_toggle = QToolButton()
        self.config_toggle.setText(t("header.config"))
        self.config_toggle.setCheckable(True)
        self.config_toggle.setChecked(True)
        self.config_toggle.toggled.connect(lambda on: self.panel.set_expanded(on))
        hh.addWidget(self.config_toggle)
        v.addWidget(header)

        self.stack = QStackedWidget()
        v.addWidget(self.stack, 1)

        wrap = QWidget()
        wl = QVBoxLayout(wrap)
        wl.setContentsMargins(24, 8, 24, 14)
        wl.setSpacing(6)
        self.chips_row = QWidget()
        self._chips_layout = QHBoxLayout(self.chips_row)
        self._chips_layout.setContentsMargins(4, 0, 4, 0)
        self._chips_layout.setSpacing(6)
        self._chips_layout.addStretch(1)
        self.chips_row.hide()
        wl.addWidget(self.chips_row)
        composer = QFrame()
        composer.setObjectName("Composer")
        shadow(composer)
        cl = QHBoxLayout(composer)
        cl.setContentsMargins(8, 8, 8, 8)
        cl.setSpacing(6)
        self.attach_btn = QPushButton("📎")
        self.attach_btn.setObjectName("ToolBtn")
        self.attach_btn.setFixedSize(30, 30)
        self.attach_btn.setToolTip(t("att.attach_tip"))
        self.attach_btn.clicked.connect(self.pick_attachments)
        cl.addWidget(self.attach_btn, 0, Qt.AlignmentFlag.AlignBottom)
        self.mic_btn = QPushButton("🎤")
        self.mic_btn.setObjectName("ToolBtn")
        self.mic_btn.setCheckable(True)
        self.mic_btn.setFixedSize(30, 30)
        self.mic_btn.setToolTip(t("rec.tip"))
        self.mic_btn.toggled.connect(self.toggle_recording)
        cl.addWidget(self.mic_btn, 0, Qt.AlignmentFlag.AlignBottom)
        self.input = InputEdit()
        self.input.submitted.connect(self.on_send)
        cl.addWidget(self.input, 1)
        self.send_btn = QPushButton("↑")
        self.send_btn.setObjectName("SendBtn")
        self.send_btn.setFixedSize(30, 30)
        self.send_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.send_btn.clicked.connect(self.on_send_clicked)
        cl.addWidget(self.send_btn, 0, Qt.AlignmentFlag.AlignBottom)
        wl.addWidget(composer)
        self.hint = label("", "Faint")
        self.hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        wl.addWidget(self.hint)
        v.addWidget(wrap)
        return center

    # —— 附件 / 录音 ——
    def pick_attachments(self) -> None:
        exts = " ".join(f"*{e}" for k in ("image", "pdf", "text", "audio") for e in sorted(_FILE_EXTS[k]))
        files, _ = QFileDialog.getOpenFileNames(self, t("att.pick"), str(Path.home()),
                                                f"{t('att.filter')} ({exts});;{t('att.all')} (*)")
        for f in files:
            self.add_attachment(f)

    def add_attachment(self, path: str) -> None:
        if path in self.attachments:
            return
        self.attachments.append(path)
        chip = AttachmentChip(path)
        chip.removed.connect(self.remove_attachment)
        self._chips_layout.insertWidget(self._chips_layout.count() - 1, chip)
        self.chips_row.show()

    def remove_attachment(self, path: str) -> None:
        if path in self.attachments:
            self.attachments.remove(path)
        for chip in self.chips_row.findChildren(AttachmentChip):
            if chip.path == path:
                chip.deleteLater()
        self.chips_row.setVisible(bool(self.attachments))

    def clear_attachments(self) -> None:
        for p in list(self.attachments):
            self.remove_attachment(p)

    def toggle_recording(self, on: bool) -> None:
        if on:
            err = self.recorder.start()
            if err:
                self.mic_btn.blockSignals(True)
                self.mic_btn.setChecked(False)
                self.mic_btn.blockSignals(False)
                self.hint.setText(err)
                return
            self._rec_timer.start()
            self._update_rec_hint()
        else:
            self._rec_timer.stop()
            path, warning = self.recorder.stop()
            self._refresh_composer()
            if path:
                self.add_attachment(path)
            if warning:
                self.hint.setText(warning)

    def _update_rec_hint(self) -> None:
        secs = int(time.monotonic() - self.recorder.started_at)
        self.hint.setText(f"⏺ {secs // 60}:{secs % 60:02d}  ·  {t('rec.recording')}")

    @staticmethod
    def _stash_attachments(paths: list[str]) -> list[str]:
        """把附件复制到 data/attachments/<id>/，保证历史记录与后续解析不受原文件移动 / 删除影响。"""
        if not paths:
            return []
        target = ATTACHMENTS_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        target.mkdir(parents=True, exist_ok=True)
        out = []
        for p in paths:
            dest = target / Path(p).name
            _shutil.copy2(p, dest)
            out.append(str(dest))
        return out

    # —— 语言 ——
    @property
    def any_busy(self) -> bool:
        return any(s.busy for s in self.sessions)

    def _request_language(self) -> None:
        if self.any_busy:
            self.hint.setText(t("lang.busy"))
            return
        self.language_requested.emit("zh" if i18n.current() == "en" else "en")

    # —— 配置 ——
    def reload_config(self) -> None:
        try:
            self.panel.load(load_config(self.config_path))
        except (ConfigError, OSError) as e:
            self.panel.show_error(self.config_path, str(e))
            if not self.config_toggle.isChecked():
                self.config_toggle.setChecked(True)
        self._refresh_composer()

    @property
    def hotkey_spec(self) -> str:
        return self.panel.cfg.hotkey if self.panel.cfg else DEFAULT_HOTKEY

    def set_hotkey_status(self, ok: bool, spec: str) -> None:
        key = pretty_hotkey(spec)
        self.footer.setText(t("sidebar.hotkey_ok", key=key) if ok else t("sidebar.hotkey_fail", key=key))
        self.footer.setToolTip("" if ok else t("sidebar.hotkey_fail_tip"))

    # —— 全局唤醒 ——
    def summon(self) -> None:
        if self.isMinimized():
            self.showNormal()
        self.show()
        self.raise_()
        self.activateWindow()
        activate_app()
        self.input.setFocus()

    # —— 会话 ——
    @property
    def current(self) -> Optional[Session]:
        i = self.session_list.currentRow()
        return self.sessions[i] if 0 <= i < len(self.sessions) else None

    def _add_session(self, title: str, at_top: bool, db_id: Optional[int] = None, loaded: bool = True,
                     tooltip: str = "", untitled: bool = False) -> Session:
        view = ChatView()
        self.stack.addWidget(view)
        item = QListWidgetItem(title)
        item.setToolTip(tooltip or title)
        s = Session(title, view, item, db_id=db_id, loaded=loaded, untitled=untitled)
        if at_top:
            self.session_list.insertItem(0, item)
            self.sessions.insert(0, s)
        else:
            self.session_list.addItem(item)
            self.sessions.append(s)
        return s

    def _load_history(self) -> None:
        try:
            rows = self.store.list_sessions()
        except Exception as e:  # 历史损坏不应阻止应用启动
            log.error(t("log.history_failed", e=e))
            return
        for row in rows:
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(row["updated_at"]))
            self._add_session(row["title"], at_top=False, db_id=row["id"], loaded=False,
                              tooltip=t("session.tooltip", title=row["title"], when=when, n=row["n_runs"]))
        log.info(t("log.history", n=len(rows), path=self.store.path))

    def _ensure_loaded(self, s: Session) -> None:
        if s.loaded or s.db_id is None:
            return
        s.loaded = True
        for record in self.store.get_runs(s.db_id):
            v = record.get("verdict")
            if v and v.get("ok"):
                q = record["question"] or t("att.only")
                if record.get("attachments"):
                    q += " [" + ", ".join(Path(a).name for a in record["attachments"]) + "]"
                s.turns.append((q, (v.get("extra") or {}).get("final_answer") or v.get("output", "")))
            s.view.add_user(record["question"], record.get("attachments"))
            card = RunCard(record.get("workers", []), record.get("leader", "Leader"), mode=record.get("mode", "judge"),
                           rounds=len(record.get("rounds", [])) or 3, context_turns=record.get("context_turns", 0),
                           worker_models=record.get("worker_models"))
            card.replay(record)
            s.view.add_card(card)

    def new_session(self) -> None:
        # 当前会话还是空的就直接复用
        cur = self.current
        if cur and not cur.busy and cur.db_id is None and cur.view.is_empty:
            self.input.setFocus()
            return
        self._add_session(t("session.new"), at_top=True, untitled=True)
        self.session_list.setCurrentRow(0)
        self.input.setFocus()

    def _switch_session(self, row: int) -> None:
        if 0 <= row < len(self.sessions):
            s = self.sessions[row]
            self._ensure_loaded(s)
            self.stack.setCurrentWidget(s.view)
            self.header_title.setText(s.title)
            self._refresh_composer()

    def _session_menu(self, pos: QPoint) -> None:
        item = self.session_list.itemAt(pos)
        if item is None:
            return
        s = self.sessions[self.session_list.row(item)]
        menu = QMenu(self)
        act = menu.addAction(t("session.delete"))
        if menu.exec(self.session_list.mapToGlobal(pos)) is act:
            ok = QMessageBox.question(self, t("session.delete"), t("session.delete_confirm", title=s.title))
            if ok == QMessageBox.StandardButton.Yes:
                self.delete_session(s)

    def delete_session(self, s: Session) -> None:
        if s.busy:
            s.task.cancel()
        if s.db_id is not None:
            self.store.delete_session(s.db_id)
        i = self.sessions.index(s)
        self.sessions.pop(i)
        self.session_list.takeItem(i)
        self.stack.removeWidget(s.view)
        s.view.deleteLater()
        if not self.sessions:
            self.new_session()

    def _set_title(self, s: Session, title: str) -> None:
        s.title = title
        s.untitled = False
        self._refresh_item(s)
        if s is self.current:
            self.header_title.setText(title)

    def _refresh_item(self, s: Session) -> None:
        s.item.setText(("● " if s.busy else "") + s.title)

    def _refresh_composer(self) -> None:
        s = self.current
        busy = bool(s and s.busy)
        ready = self.panel.cfg is not None
        self.send_btn.setProperty("busy", busy)
        self.send_btn.setText("■" if busy else "↑")
        self.send_btn.setToolTip(t("btn.stop") if busy else t("btn.send"))
        self.send_btn.setEnabled(ready)
        self.send_btn.style().unpolish(self.send_btn)
        self.send_btn.style().polish(self.send_btn)
        if not ready:
            self.hint.setText(t("hint.bad_config"))
        elif busy:
            self.hint.setText(t("hint.busy"))
        else:
            self.hint.setText(t("hint.ready"))

    # —— 发送 / 停止 ——
    def on_send_clicked(self) -> None:
        s = self.current
        if s and s.busy:
            s.task.cancel()
        else:
            self.on_send()

    def on_send(self) -> None:
        s = self.current
        text = self.input.toPlainText().strip()
        if not s or s.busy or not (text or self.attachments) or self.panel.cfg is None or self.recorder.recording:
            return
        st = self.panel.selection()
        try:
            attachments = self._stash_attachments(self.attachments)
        except OSError as e:
            self.hint.setText(str(e))
            return
        self.input.clear()
        self.clear_attachments()
        if s.untitled:
            title = text or " ".join(Path(a).name for a in attachments)
            self._set_title(s, title if len(title) <= 22 else title[:22] + "…")
        s.view.add_user(text, attachments)
        context_turns = min(len(s.turns), CONTEXT_TURNS)
        card = RunCard([w.name for w in st.workers], st.leader.name, mode=st.mode, rounds=st.rounds,
                       context_turns=context_turns, worker_models=[w.selected_model for w in st.workers])
        s.view.add_card(card)
        if not st.workers:
            card.error(t("card.no_workers"))
            return
        s.task = asyncio.ensure_future(self._run(s, card, text, context_turns, st, attachments))
        self._refresh_item(s)
        self._refresh_composer()

    async def _run(self, s: Session, card: RunCard, text: str, context_turns: int, st: RunSettings,
                   attachments: Optional[list[str]] = None) -> None:
        outcome: Optional[RunOutcome] = None
        t0 = time.monotonic()

        def on_event(kind: str, p: dict) -> None:
            card.on_event(kind, p)
            r = p.get("result")
            if r is not None and not r.ok and "spec" in p:  # 限额错误同步到「账户与用量」
                self.panel.accounts.note_failure(p["spec"].vendor, r.error)

        preprocess: list[dict] = []
        try:
            question = text
            if attachments:  # 前置解析：Leader 先把附件转成文字，再与问题拼接
                question, preprocess = await preprocess_multimodal(text, attachments, st.leader, st.pool, on_event)
                card.add_local_files(preprocess)
            prompt = build_context_prompt(s.turns, question)  # 近期对话拼成文本前缀，交给 Workers 与 Leader
            if st.mode == "cowork":
                outcome = await run_cowork_loop(prompt, st.workers, st.leader, on_event, rounds=st.rounds,
                                                max_rounds=st.max_rounds, extend_on_low=st.extend)
            else:
                outcome = await orchestrate(prompt, st.workers, st.leader, st.review, on_event, reviewer_pool=st.pool)
            card.finish(outcome)
            if outcome.verdict is not None and outcome.verdict.ok:
                turn_q = text or t("att.only")
                if attachments:
                    turn_q += " [" + ", ".join(Path(a).name for a in attachments) + "]"
                s.turns.append((turn_q, outcome.verdict.extra.get("final_answer") or outcome.verdict.output))
            self._save_run(s, card, text, st.workers, st.leader, outcome, time.monotonic() - t0, context_turns,
                           attachments or [], preprocess)
        except asyncio.CancelledError:
            card.cancelled()
        except Exception as e:  # 界面层兜底，避免异常吞没在事件循环里
            log.exception("run failed")
            card.error(t("card.unexpected", e=repr(e)))
        finally:
            s.task = None
            self._refresh_item(s)
            self._refresh_composer()
            self.run_finished.emit(outcome)

    def _save_run(self, s: Session, card: RunCard, text: str, workers: list[AgentSpec], leader: AgentSpec,
                  outcome: RunOutcome, total: float, context_turns: int = 0,
                  attachments: Optional[list[str]] = None, preprocess: Optional[list[dict]] = None) -> None:
        try:
            record = outcome_to_record(text, workers, leader, outcome, total)
            record["context_turns"] = context_turns
            record["attachments"] = attachments or []
            record["preprocess"] = preprocess or []
            record["worker_models"] = [w.selected_model for w in workers]
            if card.reviewer:
                record["reviewer"] = vars(card.reviewer)
            if s.db_id is None:
                s.db_id = self.store.create_session(s.title)
            self.store.add_run(s.db_id, record)
        except Exception as e:
            log.exception("failed to save history")
            card._tail.addWidget(banner(t("card.save_failed", e=e), C["orange"]))

    def closeEvent(self, e) -> None:  # noqa: N802
        if self.recorder.recording:
            self.recorder.stop()
        for s in self.sessions:
            if s.busy:
                s.task.cancel()
        self.panel.accounts.cancel_all()
        kill_running_processes()
        self.store.close()
        super().closeEvent(e)


# ---------------------------------------------------------------------------
# 应用控制器：负责窗口（含切换语言时重建窗口）与全局快捷键
# ---------------------------------------------------------------------------


class AppController:
    def __init__(self, config_path: Path, db_path: Path):
        self.config_path = config_path
        self.db_path = db_path
        self.win: Optional[MainWindow] = None
        self.hotkey: Optional[GlobalHotkey] = None
        self.hotkey_ok = False

    def start(self) -> None:
        try:
            i18n.set_language(load_config(self.config_path).language)
        except (ConfigError, OSError):
            i18n.set_language(i18n.DEFAULT_LANGUAGE)
        self.win = self._make_window()
        self.win.show()
        # 回调通过控制器转发，窗口重建后快捷键仍指向当前窗口
        self.hotkey = GlobalHotkey(self.win.hotkey_spec, lambda: self.win and self.win.summon())
        self.hotkey_ok, msg = self.hotkey.register()
        (log.info if self.hotkey_ok else log.warning)("[Hotkey] %s", msg)
        self.win.set_hotkey_status(self.hotkey_ok, self.hotkey.spec)

    def _make_window(self, select_db_id: Optional[int] = None) -> MainWindow:
        win = MainWindow(self.config_path, self.db_path, select_db_id=select_db_id)
        win.language_requested.connect(self.switch_language)
        return win

    def switch_language(self, lang: str) -> None:
        old = self.win
        if old is None or old.any_busy:
            return
        try:
            save_language(self.config_path, lang)
        except (ConfigError, OSError) as e:
            log.warning("could not save language: %s", e)
        i18n.set_language(lang)
        cur = old.current
        new = self._make_window(select_db_id=cur.db_id if cur else None)
        new.setGeometry(old.geometry())
        if not old.config_toggle.isChecked():
            new.config_toggle.setChecked(False)
            new.panel.set_expanded(False, animate=False)
        new.input.setPlainText(old.input.toPlainText())
        if self.hotkey:
            new.set_hotkey_status(self.hotkey_ok, self.hotkey.spec)
        self.win = new
        new.show()          # 先显示新窗口，再关闭旧窗口（避免「最后一个窗口关闭」导致退出）
        old.close()
        old.deleteLater()
        new.input.setFocus()


def main() -> None:
    parser = argparse.ArgumentParser(description="OmniCouncil desktop app")
    parser.add_argument("--config", "-c", type=Path, default=DEFAULT_CONFIG_PATH, help="config file path")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help="history database path")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="[%(name)s] %(message)s")
    if ensure_login_path():  # launched from Finder / the app bundle: find the CLIs like a terminal would
        log.info("PATH taken from the login shell")

    app = QApplication(sys.argv)
    app.setApplicationName("OmniCouncil")
    try:
        app.styleHints().setColorScheme(Qt.ColorScheme.Dark)  # 让原生标题栏也使用暗色
    except AttributeError:
        pass
    app.setStyleSheet(QSS)

    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)
    controller = AppController(args.config, args.db)
    controller.start()
    with loop:
        loop.run_forever()
    if controller.hotkey:
        controller.hotkey.unregister()


if __name__ == "__main__":
    main()
