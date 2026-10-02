"""Shared fixtures. Tests never call a real model: every CLI is a small fake script."""

from __future__ import annotations

import os
import stat
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from omnicouncil import config, i18n, storage  # noqa: E402


@pytest.fixture(autouse=True)
def _language():
    """Each test starts in English; prompts and labels depend on the UI language."""
    i18n.set_language("en")
    yield
    i18n.set_language("en")


def write_script(path: Path, body: str) -> Path:
    """Write an executable Python script that runs with the current interpreter."""
    path.write_text(f"#!{sys.executable}\n" + textwrap.dedent(body), encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


@pytest.fixture
def fakebin(tmp_path: Path) -> Path:
    d = tmp_path / "bin"
    d.mkdir()
    return d


@pytest.fixture
def agent(fakebin):
    """Factory: agent("Name", script_body, **spec_fields) → AgentSpec running a fake CLI.

    The fake CLI receives the prompt as its last argument."""

    def make(name: str, body: str, exe: str | None = None, **fields) -> config.AgentSpec:
        script = write_script(fakebin / (exe or name.lower().replace(" ", "_")), body)
        return config.AgentSpec(name=name, command=[str(script), config.PROMPT_PLACEHOLDER], **fields)

    return make


ECHO = """
import sys
print(sys.argv[-1][:200])
"""


def static(text: str, delay: float = 0.0, code: int = 0, stderr: str = "") -> str:
    """Body of a fake CLI that always prints the same answer."""
    return f"""
import sys, time
time.sleep({delay})
if {stderr!r}:
    print({stderr!r}, file=sys.stderr)
print({text!r})
sys.exit({code})
"""


# —— 模拟 claude 的 stream-json 常驻协议 ——
FAKE_CLAUDE_STREAM = """
import json, sys
history = []
session = 1
turn = 0
for line in sys.stdin:
    msg = json.loads(line)
    text = msg["message"]["content"][0]["text"]
    if text == "/clear":
        history = []
        session += 1
        print(json.dumps({"type": "conversation_reset"}), flush=True)
        print(json.dumps({"type": "result", "subtype": "success", "result": ""}), flush=True)
        continue
    if text == "CRASH":
        sys.exit(3)
    if text.startswith("SLOW"):
        import time
        time.sleep(30)
    turn += 1
    history.append(text)
    print(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "..."}]}}), flush=True)
    reply = f"session={session} turn={turn} seen={len(history)} last={text}"
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": reply}), flush=True)
"""
