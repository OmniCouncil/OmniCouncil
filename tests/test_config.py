"""Config template, migration, validation, model-flag injection and concurrent saves."""

import json
import threading

import pytest

from conftest import config


PH = config.PROMPT_PLACEHOLDER


def test_template_matches_migrated_defaults(tmp_path):
    p = tmp_path / "config.json"
    assert config.ensure_config(p)
    first = json.loads(p.read_text())
    config.load_config(p)  # loading must not need any further migration
    assert json.loads(p.read_text()) == first
    assert config.migrate_config(json.loads(p.read_text())) is False


def test_template_has_no_personal_choices():
    tpl = json.loads(config.TEMPLATE_PATH.read_text())
    assert all(w["selected_model"] == "" for w in tpl["workers"])
    assert tpl["language"] == "en" and tpl["mode"] == "judge"


def test_migrates_old_config(tmp_path):
    old = {
        "workers": [
            {"name": "Claude", "command": ["claude", "--strict-mcp-config", "-p", PH]},
            {"name": "Gemini", "command": ["agy", "--sandbox", "--model", "gemini-x", "-p", PH]},
        ],
        "leader": {"name": "Opus", "command": ["claude", "--model", "opus", "-p", PH]},
    }
    p = tmp_path / "config.json"
    p.write_text(json.dumps(old))
    cfg = config.load_config(p)
    claude, gemini = cfg.workers
    assert claude.command[:3] == ["claude", "--tools", ""]          # tools disabled
    assert claude.model_flag == "--model" and claude.selected_model == ""
    assert claude.is_persistent is True
    assert gemini.selected_model == "gemini-x"                       # moved out of the command
    assert "gemini-x" not in gemini.command
    assert "gemini-x" in gemini.available_models
    assert cfg.leaders[0].name == "Opus"                             # old single-leader format


@pytest.mark.parametrize("command,model,flag,expected", [
    (["claude", "--tools", "", "-p", PH], "haiku", "--model", ["claude", "--tools", "", "--model", "haiku", "-p", PH]),
    (["codex", "exec", "--sandbox", "read-only", PH], "gpt-x", "-m", ["codex", "exec", "--sandbox", "read-only", "-m", "gpt-x", PH]),
    (["agy", "--model", "old", "-p", PH], "new", "--model", ["agy", "--model", "new", "-p", PH]),
    (["claude", "-p", PH], "", "--model", ["claude", "-p", PH]),
    (["sh", "-c", f"run '{PH}'"], "x", "-m", ["sh", "-c", f"run '{PH}'"]),  # embedded prompt: never inject
])
def test_model_flag_injection(command, model, flag, expected):
    spec = config.AgentSpec("a", command, selected_model=model, model_flag=flag)
    assert spec.argv_template() == expected


def test_build_replaces_placeholder_only():
    spec = config.AgentSpec("a", ["cli", "-p", PH])
    assert spec.build("hello {prompt} 'quotes'") == ["cli", "-p", "hello {prompt} 'quotes'"]


def test_env_unset_removes_only_listed_variables(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret")
    monkeypatch.setenv("KEEP_ME", "1")
    env = config.AgentSpec("a", ["x", PH], env_unset=["ANTHROPIC_API_KEY"]).env()
    assert "ANTHROPIC_API_KEY" not in env and env["KEEP_ME"] == "1"


def test_concurrent_saves_are_not_lost(tmp_path):
    p = tmp_path / "config.json"
    config.ensure_config(p)
    jobs = [("Claude", "haiku"), ("Gemini", "gemini-3.1-pro-high"), ("Codex", "gpt-5.6-luna")] * 15
    threads = [threading.Thread(target=config.save_worker_model, args=(p, *j)) for j in jobs]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    got = {w.name: w.selected_model for w in config.load_config(p).workers}
    assert got == {"Claude": "haiku", "Gemini": "gemini-3.1-pro-high", "Codex": "gpt-5.6-luna"}


@pytest.mark.parametrize("patch,message", [
    ({"mode": "party"}, "mode"),
    ({"cowork_rounds": 5, "cowork_max_rounds": 3}, "cowork_rounds"),
    ({"default_leader": "Nobody"}, "Nobody"),
])
def test_validation_errors(tmp_path, patch, message):
    p = tmp_path / "config.json"
    config.ensure_config(p)
    data = json.loads(p.read_text())
    data.update(patch)
    p.write_text(json.dumps(data))
    with pytest.raises(config.ConfigError, match=message):
        config.load_config(p)


def test_worker_without_placeholder_is_rejected(tmp_path):
    p = tmp_path / "config.json"
    config.ensure_config(p)
    data = json.loads(p.read_text())
    data["workers"][0]["command"] = ["claude", "-p"]
    p.write_text(json.dumps(data))
    with pytest.raises(config.ConfigError, match="prompt"):
        config.load_config(p)
