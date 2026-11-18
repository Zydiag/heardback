import subprocess
from types import SimpleNamespace

import pytest

from heardback import cli, doctor


@pytest.fixture
def healthy(monkeypatch):
    def command(*argv):
        out = {("tmux", "-V"): "tmux 3.5", ("pactl", "info"): "Server Name: PulseAudio (on PipeWire 1.4)",
               ("claude", "--version"): "2.1.312 (Claude Code)",
               ("claude", "auth", "status"): '{"loggedIn":true,"email":"private@example.invalid"}'}[argv]
        return subprocess.CompletedProcess(argv, 0, out, "")
    monkeypatch.setattr(doctor, "_command", command)
    monkeypatch.setattr(doctor, "missing", lambda: [])
    monkeypatch.setattr(doctor, "default_device", lambda: "cpu")
    monkeypatch.setattr(doctor.voice_config, "load", lambda: SimpleNamespace(stt_device="cpu"))
    monkeypatch.setattr(doctor.desktop, "unavailable", lambda name: None)
    return command


def test_all_required_ok_and_private_login_not_printed(healthy, capsys):
    assert cli.main(["doctor"]) == 0
    out = capsys.readouterr().out
    for name in ("python", "tmux", "pipewire", "claude", "claude-login", "config", "wake", "voice", "voice-device"):
        assert f"ok {name}:" in out
    assert "push-to-talk" in out and "CPU" in out
    assert "private@example.invalid" not in out
    assert all(line.startswith(("ok ", "missing ", "warn ")) for line in out.splitlines())


def test_base_install_and_missing_system_programs(monkeypatch, capsys):
    monkeypatch.setattr(doctor, "_command", lambda *argv: None)
    monkeypatch.setattr(doctor, "missing", lambda: ["torch"])
    assert doctor.run() == 1
    out = capsys.readouterr().out
    assert "warn voice:" in out and "install heardback[voice] or [voice-cpu]" in out
    for name in ("tmux", "pipewire", "claude", "claude-login"):
        assert f"missing {name}:" in out
    assert all("; fix: " in line for line in out.splitlines() if not line.startswith("ok "))


def test_untested_version_warns_but_stays_usable(healthy, monkeypatch, capsys):
    monkeypatch.setattr(doctor, "_command", lambda *argv: subprocess.CompletedProcess(argv, 0, "99.0.0", "")
                        if argv == ("claude", "--version") else healthy(*argv))
    assert doctor.run() == 0
    assert "warn claude: 99.0.0 (untested; tested" in capsys.readouterr().out


@pytest.mark.parametrize("login", ['{"loggedIn":false}', '[]', 'not JSON', '{"email":"private@example.invalid"}'])
def test_login_must_report_logged_in(healthy, monkeypatch, capsys, login):
    monkeypatch.setattr(doctor, "_command", lambda *argv: subprocess.CompletedProcess(argv, 0, login, "")
                        if argv == ("claude", "auth", "status") else healthy(*argv))
    assert doctor.run() == 1
    out = capsys.readouterr().out
    assert "missing claude-login:" in out and "private@example.invalid" not in out


def test_invalid_config_is_one_line_with_a_fix(healthy, monkeypatch, tmp_path, capsys):
    path = tmp_path / "bad.toml"
    path.write_text('[user]\nname = "secret"\nbad = 3')
    monkeypatch.setenv("HEARDBACK_CONFIG", str(path))
    assert doctor.run() == 1
    out = capsys.readouterr().out
    assert "missing config:" in out and "fix: correct" in out
    assert "secret" not in out


def test_cuda_without_hardware_has_cpu_fix(healthy, monkeypatch, capsys):
    monkeypatch.setattr(doctor.voice_config, "load", lambda: SimpleNamespace(stt_device="cuda"))
    assert doctor.run() == 1
    assert 'missing voice-device: CUDA configured; CPU available; fix: set stt_device = "cpu"' in capsys.readouterr().out


def test_command_failures_are_handled(monkeypatch):
    def fail(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], 10)
    monkeypatch.setattr(doctor.subprocess, "run", fail)
    assert doctor._command("claude", "auth", "status") is None


def test_wake_model_found(healthy, monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    model = tmp_path / "heardback/models/hey_claude.onnx"
    model.parent.mkdir(parents=True)
    model.touch()
    assert doctor.run() == 0
    assert "ok wake: wake model found" in capsys.readouterr().out


def test_unknown_voice_settings_keep_one_line_per_check(monkeypatch, capsys):
    path = doctor.voice_config.config_path()
    path.parent.mkdir(parents=True)
    path.write_text('unknown = "private value"\n')
    monkeypatch.setattr(doctor, "_command", lambda *argv: None)
    assert doctor.run() == 1
    out = capsys.readouterr().out
    assert "warn config: voice.toml has ignored settings; fix:" in out
    assert all(line.startswith(("ok ", "missing ", "warn ")) for line in out.splitlines())
    assert "private value" not in out
