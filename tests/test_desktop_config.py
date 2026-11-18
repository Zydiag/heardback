import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from heardback import config
from heardback.brain.claude_proc import PROMPT, build_cmd, prompt_text
from jv import tmux

EXAMPLES = Path(__file__).parents[1] / "examples"


def test_defaults_and_xdg(monkeypatch, tmp_path):
    monkeypatch.delenv("HEARDBACK_CONFIG", raising=False)
    assert config.config_path() == Path(os.environ["XDG_CONFIG_HOME"]) / "heardback/config.toml"
    assert config.load() == config.Config()
    assert config.load().name is None and config.load().session is None
    assert config.load().project_roots == config.load().claude_dirs == ("~",)
    assert config.load().aliases == {}
    assert config.load().modes == {"default": ["@DEFAULT_SOURCE@"]}
    assert prompt_text().startswith("You are heardback, a voice assistant on the user's Linux laptop.")
    monkeypatch.delenv("XDG_CONFIG_HOME")
    assert config.config_path() == Path.home() / ".config/heardback/config.toml"


def test_user_config_and_prompt(monkeypatch):
    path = EXAMPLES / "config.user.toml"
    if not path.exists():
        pytest.skip("private user config is omitted from the public export")
    monkeypatch.setenv("HEARDBACK_CONFIG", str(path))
    cfg = config.load()
    assert cfg.name == tomllib.loads(path.read_text())["user"]["name"] and cfg.session == tmux.session() == "0"
    assert cfg.project_roots == ("~/work/projects", "~/work", "~/claude")
    assert cfg.claude_dirs == ("~/work", "~/claude") and cfg.claude_home == "~/claude"
    assert cfg.aliases == dict(buds="buds", earbuds="buds", headphones="buds", earphones="buds", speaker="jbl")
    assert cfg.mic == "laptop" and cfg.modes == {"laptop": ["alsa_input.*"], "buds": ["bluez_input.*", "alsa_input.*"]}
    cmd = build_cmd(["claude"], PROMPT)
    assert cmd[cmd.index("--system-prompt") + 1].startswith(f"You are heardback, a voice assistant on {cfg.name}'s Linux laptop.")
    assert "{{user}}" not in prompt_text()


def test_generic_example_and_load_once(monkeypatch, tmp_path):
    p = tmp_path / "config.toml"
    p.write_text((EXAMPLES / "config.toml").read_text())
    monkeypatch.setenv("HEARDBACK_CONFIG", str(p))
    assert config.load() == config.Config()
    p.write_text('[user]\nname = "Changed"\n')
    assert config.load().name is None


@pytest.mark.parametrize("text", [
    '[user]\nname = 3', '[tmux]\nsession = ""', '[tmux]\nproject_roots = [3]',
    '[tmux]\nclaude_dirs = []', '[devices]\naliases = {speaker = 3}',
    '[mic]\nmode = "absent"', '[mic.modes]\n"../escape" = ["x"]',
    '[mic.modes]\nusb = []', '[user]\nunknown = "x"', 'user = "x"',
])
def test_bad_config_is_rejected(monkeypatch, tmp_path, text):
    p = tmp_path / "bad.toml"
    p.write_text(text)
    monkeypatch.setenv("HEARDBACK_CONFIG", str(p))
    with pytest.raises(ValueError):
        config.load()


def test_session_current_or_first(monkeypatch):
    monkeypatch.delenv("HEARDBACK_CONFIG", raising=False)
    calls = []

    def run(*args):
        calls.append(args)
        return "current\n" if args[0] == "display-message" else "first\nsecond\n"

    monkeypatch.setattr(tmux, "tmux", run)
    assert tmux.session() == "first"
    monkeypatch.setenv("TMUX", "/tmp/tmux,123,0")
    assert tmux.session() == "current"
    assert [c[0] for c in calls] == ["list-sessions", "display-message"]


def test_named_tmux_session(tmux_server, run_jv, monkeypatch):
    monkeypatch.delenv("HEARDBACK_CONFIG", raising=False)
    tmux_server("rename-session", "-t", "0", "workbench")
    code, out, _ = run_jv("tmux", "new", "--json")
    assert code == 0 and out["target"].startswith("workbench:") and out["observed"]
    assert tmux.resolve(out["target"])["target"] == out["target"]


def test_jv_imports_without_voice_dependencies():
    code = """
import sys
class NoVoice:
    def find_spec(self, fullname, *args):
        if fullname.split('.')[0] in {'numpy', 'torch', 'torchaudio', 'faster_whisper', 'kokoro'}:
            raise AssertionError(fullname)
sys.meta_path.insert(0, NoVoice())
from jv.cli import all_verbs
assert all_verbs()
"""
    subprocess.run([sys.executable, "-c", code], check=True)
