"""Prompt templates, stored as editable files in ``omnicouncil/prompts/<name>.<lang>.md``.

To customize a prompt, copy it to ``<APP_HOME>/prompts/`` (same file name) and edit the copy; it takes
precedence over the built-in one. Templates use ``str.format`` placeholders such as ``{question}``;
literal braces must be doubled (``{{`` / ``}}``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from . import i18n
from .paths import PROMPTS_DIR, USER_PROMPTS_DIR


def path_for(name: str, lang: Optional[str] = None) -> Path:
    lang = lang or i18n.current()
    for base in (USER_PROMPTS_DIR, PROMPTS_DIR):
        for code in (lang, "en"):
            p = base / f"{name}.{code}.md"
            if p.is_file():
                return p
    raise FileNotFoundError(f"prompt template not found: {name}.{lang}.md")


def get(name: str, lang: Optional[str] = None) -> str:
    """Read a template (fresh each call, so edits apply without restarting)."""
    return path_for(name, lang).read_text(encoding="utf-8").rstrip("\n")
