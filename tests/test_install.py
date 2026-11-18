import json
import os

import pytest

from heardback.status import install
from heardback.migrate import LEGACY_NAME
from jv.verb import JvError

USER_STOP = {"hooks": [{"type": "command", "command": "notify-send done"}]}


def load(p):
    return json.loads(p.read_text())


def test_install_into_missing(tmp_path):
    p = tmp_path / "settings.json"
    assert install.install_hooks(p) is None
    assert load(p) == {"hooks": install.HOOKS}
    assert load(p)["hooks"]["Stop"] == [{"hooks": [{"type": "command", "command": "jv-hook", "timeout": 2}]}]


def test_install_keeps_other_hooks(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"model": "opus", "hooks": {"Stop": [USER_STOP]}}))
    install.install_hooks(p)
    d = load(p)
    assert d["model"] == "opus" and d["hooks"]["Stop"] == [USER_STOP, *install.HOOKS["Stop"]]


def test_install_twice_no_dupes(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"hooks": {"Stop": [USER_STOP]}}))
    install.install_hooks(p)
    once = load(p)
    install.install_hooks(p)
    assert load(p) == once


def test_invalid_json_untouched(tmp_path):
    p = tmp_path / "settings.json"
    p.write_bytes(b'{"hooks": ')
    with pytest.raises(JvError, match="not valid JSON; not touched"):
        install.install_hooks(p)
    with pytest.raises(JvError):
        install.uninstall_hooks(p)
    assert p.read_bytes() == b'{"hooks": ' and list(tmp_path.iterdir()) == [p]


def test_uninstall_roundtrip(tmp_path):
    p = tmp_path / "settings.json"
    orig = {"model": "opus", "hooks": {"Stop": [USER_STOP]}}
    p.write_text(json.dumps(orig))
    install.install_hooks(p)
    install.uninstall_hooks(p)
    assert load(p) == orig
    p.write_text(json.dumps({"model": "opus"}))
    install.install_hooks(p)
    install.uninstall_hooks(p)
    assert load(p) == {"model": "opus"}


def test_backup_written(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text('{"a": 1}')
    os.chmod(p, 0o640)
    b = install.install_hooks(p)
    assert b.name.startswith("settings.json.bak-heardback-") and b.read_text() == '{"a": 1}'
    assert os.stat(p).st_mode & 0o777 == 0o640


def test_verbs(run_jv, monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    code, out, _ = run_jv("status", "install-hooks", "--json")
    assert code == 0 and load(tmp_path / "settings.json") == {"hooks": install.HOOKS}
    assert run_jv("status", "uninstall-hooks")[0] == 0 and load(tmp_path / "settings.json") == {}
    (tmp_path / "settings.json").write_text("nope")
    code, _, err = run_jv("status", "install-hooks")
    assert code == 1 and "not touched" in err


def test_install_mixed_group(tmp_path):
    p = tmp_path / "settings.json"
    ours = {"type": "command", "command": "jv-hook"}
    p.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [USER_STOP["hooks"][0], ours]}], "PreToolUse": [{"hooks": [ours]}]}}))
    install.install_hooks(p)
    d = load(p)
    assert d["hooks"]["Stop"] == [USER_STOP, *install.HOOKS["Stop"]] and "PreToolUse" not in d["hooks"]
    install.install_hooks(p)
    assert load(p) == d


@pytest.mark.parametrize("bad", [{"hooks": None}, {"hooks": []}, {"hooks": {"Stop": {}}}, {"hooks": {"Stop": ["x"]}},
                                 {"hooks": {"Stop": [{"hooks": "x"}]}}, {"hooks": {"Stop": [{"hooks": ["x"]}]}}])
def test_wrong_types_untouched(tmp_path, bad):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps(bad))
    before = p.read_bytes()
    for fn in (install.install_hooks, install.uninstall_hooks):
        with pytest.raises(JvError, match="not touched"):
            fn(p)
    assert p.read_bytes() == before and list(tmp_path.iterdir()) == [p]


def test_uninstall_noop(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"model": "opus", "hooks": {"Stop": [USER_STOP]}}))
    before = p.read_bytes()
    assert install.uninstall_hooks(p) is None
    assert p.read_bytes() == before and list(tmp_path.iterdir()) == [p]


def test_symlinked_settings(tmp_path):
    real = tmp_path / "dotfiles" / "settings.json"
    real.parent.mkdir()
    real.write_text('{"a": 1}')
    p = tmp_path / "settings.json"
    p.symlink_to(real)
    install.install_hooks(p)
    assert p.is_symlink() and load(real)["hooks"] == install.HOOKS
    install.uninstall_hooks(p)
    assert p.is_symlink() and load(real) == {"a": 1}


IDLE_ORIG = "general {\n    lock_cmd = hyprlock\n}\n\nlistener {\n    timeout = 480\n    on-timeout = loginctl lock-session\n}\n"


def idle_conf(tmp_path, text=IDLE_ORIG):
    p = tmp_path / "hypr" / "hypridle.conf"
    p.parent.mkdir(exist_ok=True)
    p.write_text(text)
    return p


def test_idle_install_once(tmp_path):
    p = idle_conf(tmp_path)
    b = install.install_idle(p)
    assert b.name.startswith("hypridle.conf.bak-heardback-") and b.read_text() == IDLE_ORIG
    text = p.read_text()
    assert text.startswith(IDLE_ORIG) and text.endswith(install.IDLE_BLOCK) and "timeout = 180\n" in text
    assert install.install_idle(p) is None and p.read_text() == text  # twice: no duplicate, no write


def test_idle_legacy_block_is_replaced_or_removed(tmp_path):
    legacy = install.IDLE_BLOCK.replace("heardback", LEGACY_NAME)
    p = idle_conf(tmp_path, IDLE_ORIG + "\n" + legacy)
    install.install_idle(p)
    assert p.read_text() == IDLE_ORIG + "\n" + install.IDLE_BLOCK
    p.write_text(IDLE_ORIG + "\n" + legacy)
    install.uninstall_idle(p)
    assert p.read_text() == IDLE_ORIG


@pytest.mark.parametrize("orig", [IDLE_ORIG, "general {}", ""])
def test_idle_uninstall_exact(tmp_path, orig):
    p = idle_conf(tmp_path, orig)
    install.install_idle(p)
    assert install.uninstall_idle(p) and p.read_text() == orig
    n = len(list(p.parent.iterdir()))
    assert install.uninstall_idle(p) is None and len(list(p.parent.iterdir())) == n  # nothing ours: no write, no backup


def test_idle_missing_and_broken(tmp_path):
    with pytest.raises(JvError, match="hypridle.conf not found"):
        install.install_idle(tmp_path / "hypridle.conf")
    assert install.uninstall_idle(tmp_path / "hypridle.conf") is None and list(tmp_path.iterdir()) == []
    for bad in (b"\xff\xfe", (IDLE_ORIG + "# heardback-away begin\nlistener {\n").encode()):
        p = tmp_path / "hypridle.conf"
        p.write_bytes(bad)
        for fn in (install.install_idle, install.uninstall_idle):
            with pytest.raises(JvError, match="not touched"):
                fn(p)
        assert p.read_bytes() == bad and list(tmp_path.iterdir()) == [p]


def test_idle_symlink(tmp_path):
    (tmp_path / "dotfiles").mkdir()
    real = idle_conf(tmp_path / "dotfiles")
    link = tmp_path / "hypridle.conf"
    link.symlink_to(real)
    install.install_idle(link)
    assert link.is_symlink() and real.read_text().endswith(install.IDLE_BLOCK)
    install.uninstall_idle(link)
    assert link.is_symlink() and real.read_text() == IDLE_ORIG


def test_idle_verbs(run_jv, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    (tmp_path / "claude").mkdir()
    code, _, err = run_jv("status", "install-hooks", "--idle")
    assert code == 1 and "hypridle.conf not found" in err and not (tmp_path / "hypr").exists()
    p = idle_conf(tmp_path)
    code, out, _ = run_jv("status", "install-hooks", "--idle", "--json")
    assert code == 0 and out["hypridle"] == str(p) and p.read_text().endswith(install.IDLE_BLOCK)
    assert load(tmp_path / "claude" / "settings.json") == {"hooks": install.HOOKS}  # --idle is additive
    code, out, _ = run_jv("status", "uninstall-hooks", "--json")
    assert code == 0 and p.read_text() == IDLE_ORIG and out["hypridle_backup"]


def test_idle_install_checks_settings_first(run_jv, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    p = idle_conf(tmp_path)
    (tmp_path / "claude").mkdir()
    (tmp_path / "claude" / "settings.json").write_text("nope")
    code, _, err = run_jv("status", "install-hooks", "--idle")
    assert code == 1 and "not touched" in err and p.read_text() == IDLE_ORIG


def test_uninstall_checks_idle_first(run_jv, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    (tmp_path / "claude").mkdir()
    assert run_jv("status", "install-hooks")[0] == 0
    p = idle_conf(tmp_path)
    p.write_text(IDLE_ORIG + "# heardback-away begin\n")
    code, _, err = run_jv("status", "uninstall-hooks")
    assert code == 1 and "not touched" in err
    assert load(tmp_path / "claude" / "settings.json") == {"hooks": install.HOOKS}  # nothing half-done


def test_idle_verb_prints_restart(run_jv, monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    idle_conf(tmp_path)
    code, out, _ = run_jv("status", "install-hooks", "--idle")
    assert code == 0 and """hyprctl dispatch 'hl.dsp.exec_cmd("hypridle")'""" in out
