"""Per-agent web search: argv per CLI, migration defaults, Codex Leader model pin, persistence."""

import json

from omnicouncil import config
from omnicouncil.config import AgentSpec
from omnicouncil.multimodal import build_file_spec
from omnicouncil.pool import ClaudeStream

PH = config.PROMPT_PLACEHOLDER


def test_claude_web_search_adds_only_read_only_web_tools_preapproved():
    spec = AgentSpec("C", ["claude", "--tools", "", "--strict-mcp-config", "-p", PH], web_search=True)
    argv = spec.argv_template()
    assert argv[argv.index("--tools") + 1] == "WebSearch,WebFetch"
    assert argv[argv.index("--allowedTools") + 1] == "WebSearch,WebFetch"
    assert not any(t in " ".join(argv) for t in ("Write", "Edit", "Bash"))


def test_claude_web_search_off_keeps_tools_disabled():
    spec = AgentSpec("C", ["claude", "--tools", "", "-p", PH], web_search=False)
    assert spec.argv_template() == ["claude", "--tools", "", "-p", PH]


def test_codex_search_flag_goes_before_subcommand():
    spec = AgentSpec("X", ["codex", "exec", "--sandbox", "read-only", PH], web_search=True,
                     model_flag="-m", selected_model="gpt-5.6-sol")
    assert spec.argv_template() == ["codex", "--search", "exec", "--sandbox", "read-only", "-m", "gpt-5.6-sol", PH]


def test_agy_has_no_switch():
    spec = AgentSpec("G", ["agy", "--sandbox", "-p", PH], web_search=True)
    assert not spec.supports_web_search and spec.argv_template() == ["agy", "--sandbox", "-p", PH]


def test_attachment_preprocessing_uses_read_only_without_web_tools(tmp_path):
    f = tmp_path / "a.png"
    f.write_bytes(b"")
    leader = AgentSpec("Opus", ["claude", "--tools", "", "-p", PH], web_search=True, file_flag="--add-dir",
                       file_tools="Read", file_types=["image"])
    argv = build_file_spec(leader, [str(f)]).argv_template()
    assert argv[argv.index("--tools") + 1] == "Read" and "--allowedTools" not in argv


def test_pool_stream_command_keeps_web_tools():
    spec = AgentSpec("C", ["claude", "--tools", "", "-p", PH], web_search=True, is_persistent=True)
    argv = ClaudeStream().argv(spec)
    assert "WebSearch,WebFetch" in argv and "--input-format" in argv and PH not in argv


def test_migration_defaults_and_codex_leader_pin(tmp_path):
    old = {
        "workers": [
            {"name": "Claude", "command": ["claude", "-p", PH]},
            {"name": "Gemini", "command": ["agy", "-p", PH]},
            {"name": "Codex", "command": ["codex", "exec", PH], "model_flag": "-m", "selected_model": "gpt-5.6-sol"},
        ],
        "leaders": [{"name": "Codex", "command": ["codex", "exec", PH]}],
    }
    p = tmp_path / "config.json"
    p.write_text(json.dumps(old))
    cfg = config.load_config(p)
    assert {w.name: w.web_search for w in cfg.workers} == {"Claude": True, "Gemini": False, "Codex": True}
    leader = cfg.leaders[0]
    assert leader.web_search and leader.selected_model == "gpt-5.6-sol"
    assert "-m" in leader.argv_template()


def test_save_agent_field(tmp_path):
    p = tmp_path / "config.json"
    config.ensure_config(p)
    config.save_agent_field(p, "leaders", "Claude Opus", "web_search", False)
    config.save_agent_field(p, "workers", "Claude", "web_search", False)
    cfg = config.load_config(p)
    assert cfg.get_leader("Claude Opus").web_search is False
    assert next(w for w in cfg.workers if w.name == "Claude").web_search is False
