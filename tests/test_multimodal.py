"""Attachment routing and file-access argument building."""

import asyncio

import pytest

from conftest import config, engine

PH = config.PROMPT_PLACEHOLDER


@pytest.mark.parametrize("name,kind", [
    ("a.PNG", "image"), ("b.jpeg", "image"), ("c.pdf", "pdf"), ("d.wav", "audio"), ("e.m4a", "audio"),
    ("f.md", "text"), ("g.csv", "text"), ("h.exe", None),
])
def test_file_kind(name, kind):
    assert engine.file_kind(name) == kind


def test_claude_file_spec_enables_read_and_adds_dir(tmp_path):
    f = tmp_path / "x.png"
    f.write_bytes(b"")
    leader = config.AgentSpec("Opus", ["claude", "--tools", "", "-p", PH], file_flag="--add-dir",
                              file_arg="dir", file_tools="Read", file_types=["image"], is_persistent=True)
    spec = engine.build_file_spec(leader, [str(f)])
    assert spec.command == ["claude", "--tools", "Read", "--add-dir", str(tmp_path.resolve()), "-p", PH]
    assert spec.is_persistent is False


def test_codex_file_spec_joins_flag_and_path(tmp_path):
    f = tmp_path / "x.png"
    f.write_bytes(b"")
    leader = config.AgentSpec("Codex", ["codex", "exec", PH], file_flag="--image=", file_arg="file",
                              file_types=["image"])
    assert engine.build_file_spec(leader, [str(f)]).command == ["codex", "exec", f"--image={f.resolve()}", PH]


def test_routing_text_local_audio_to_capable_leader(agent, tmp_path):
    (tmp_path / "notes.txt").write_text("vendor offered 12%")
    (tmp_path / "pic.png").write_bytes(b"")
    (tmp_path / "voice.wav").write_bytes(b"")
    image_reader = agent("Opus", 'print("IMAGE TEXT")', file_flag="--add-dir", file_types=["image", "pdf", "text"])
    audio_reader = agent("Gemini", 'print("TRANSCRIPT")', file_flag="--add-dir", file_types=["image", "audio"])
    events = []
    prompt, records = asyncio.run(engine.preprocess_multimodal(
        "what?", [str(tmp_path / n) for n in ("notes.txt", "pic.png", "voice.wav")], image_reader,
        [image_reader, audio_reader], lambda k, p: events.append((k, p["spec"].name))))
    by_file = {r["files"][0]: r["processor"] for r in records}
    assert by_file == {"notes.txt": "local", "pic.png": "Opus", "voice.wav": "Gemini"}
    assert "vendor offered 12%" in prompt and "IMAGE TEXT" in prompt and "TRANSCRIPT" in prompt
    assert prompt.rstrip().endswith("what?")
    assert ("preprocess_done", "Gemini") in events


def test_no_attachments_returns_question_unchanged():
    leader = config.AgentSpec("L", ["x", PH])
    assert asyncio.run(engine.preprocess_multimodal("q", [], leader)) == ("q", [])
