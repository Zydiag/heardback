import json
import os
from pathlib import Path
import stat

import pytest

from jv import shot


@pytest.fixture
def fake_commands(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "argv.jsonl"
    pgrep = bindir / "pgrep"
    pgrep.write_text("#!/usr/bin/python3\nimport os,sys\nopen(os.environ['SHOT_LOG'],'a').write('pgrep '+repr(sys.argv[1:])+'\\n')\nsys.exit(0 if os.getenv('SHOT_LOCKED') else 1)\n")
    pgrep.chmod(0o755)
    wl_copy = bindir / "wl-copy"
    wl_copy.write_text("#!/usr/bin/python3\nimport os,sys\nsys.stdin.buffer.read()\nopen(os.environ['SHOT_LOG'],'a').write('wl-copy '+repr(sys.argv[1:])+'\\n')\n")
    wl_copy.chmod(0o755)
    grim = bindir / "grimblast"
    grim.write_text("#!/usr/bin/python3\nimport os,subprocess,sys\nopen(os.environ['SHOT_LOG'],'a').write('grimblast '+repr(sys.argv[1:])+'\\n')\nmode=os.getenv('SHOT_GRIM_MODE','ok')\nif mode == 'fail': sys.exit(2)\nif mode == 'empty': sys.exit(0)\nopen(sys.argv[-1],'wb').write(b'fake png')\nsubprocess.run(['wl-copy'],input=b'fake png',check=True)\nif mode == 'linger': subprocess.Popen(['/usr/bin/python3','-c','import time; time.sleep(8)'])\n")
    grim.chmod(0o755)
    monkeypatch.setenv("PATH", str(bindir))
    monkeypatch.setenv("SHOT_LOG", str(log))
    return log, grim


def test_capture_fixed_argv_private_file_and_cap(fake_commands, run_jv):
    log, _ = fake_commands
    root = Path(os.environ["XDG_STATE_HOME"]) / "heardback" / "shots"
    root.mkdir(parents=True)
    for i in range(25):
        p = root / f"old-{i:02}.png"
        p.write_bytes(b"old")
        os.utime(p, (i + 1, i + 1))
    code, result, _ = run_jv("shot", "window", "--json")
    assert code == 0 and result["say"] == "Screenshot copied."
    assert len(list(root.glob("*.png"))) == shot.CAP
    newest = shot.latest()
    assert newest.read_bytes() == b"fake png"
    assert stat.S_IMODE(newest.stat().st_mode) == 0o600
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    calls = log.read_text().splitlines()
    assert "pgrep ['-x', 'hyprlock']" in calls
    assert any(line.startswith("grimblast ['copysave', 'active', '") for line in calls)
    assert "wl-copy []" in calls
    assert str(root) not in json.dumps(result)


def test_latest_without_shots_directory_says_no_screenshot(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    with pytest.raises(shot.JvError) as exc:
        shot.latest()
    assert exc.value.extra["say"] == "Take a screenshot first."


def test_locked_screen_refuses_before_capture(fake_commands, monkeypatch, run_jv):
    log, _ = fake_commands
    monkeypatch.setenv("SHOT_LOCKED", "1")
    code, result, _ = run_jv("shot", "screen", "--json")
    assert code == 4 and result["say"] == "I can't take a screenshot while the screen is locked."
    assert not any("grimblast" in line for line in log.read_text().splitlines())
    assert not (Path(os.environ["XDG_STATE_HOME"]) / "heardback" / "shots").exists()


def test_dry_run_never_calls_program_or_exposes_path(fake_commands, run_jv):
    log, _ = fake_commands
    code, result, _ = run_jv("shot", "area", "--dry-run", "--json")
    assert code == 0 and result["dry_run"] is True
    assert "path" not in json.dumps(result) and "shots" not in json.dumps(result)
    assert not log.exists()


def test_missing_grimblast_is_plain_sentence(fake_commands, monkeypatch, run_jv):
    _, grim = fake_commands
    grim.unlink()
    code, result, _ = run_jv("shot", "screen", "--json")
    assert code == 1 and result["say"] == "I couldn't take the screenshot."


@pytest.mark.parametrize("mode", ["fail", "empty"])
def test_grimblast_failure_or_missing_file_is_plain_sentence(fake_commands, monkeypatch, run_jv, mode):
    monkeypatch.setenv("SHOT_GRIM_MODE", mode)
    code, result, _ = run_jv("shot", "screen", "--json")
    assert code == 1 and result["say"] == "I couldn't take the screenshot."
    assert not list((Path(os.environ["XDG_STATE_HOME"]) / "heardback" / "shots").glob("*.png"))


def test_area_cancel_is_plain_sentence(fake_commands, monkeypatch, run_jv):
    monkeypatch.setenv("SHOT_GRIM_MODE", "fail")
    spoken = []
    monkeypatch.setattr(shot, "speak", lambda args: spoken.append(args["text"]))
    code, result, _ = run_jv("shot", "area", "--json")
    assert code == 1 and result["say"] == "Area selection cancelled."
    assert spoken == ["Select the area."]


def test_screen_and_area_use_fixed_targets(fake_commands, monkeypatch, run_jv):
    log, _ = fake_commands
    assert run_jv("shot", "screen", "--json")[0] == 0
    assert run_jv("shot", "area", "--json")[0] == 0
    calls = log.read_text()
    assert "'screen'" in calls and "'area'" in calls


def test_capture_returns_while_clipboard_server_lingers(fake_commands, run_jv, monkeypatch):
    # Live 17:38: wl-copy's background server held grimblast's stdout pipe, so the shot waited 30 s and failed.
    import time
    monkeypatch.setenv("SHOT_GRIM_MODE", "linger")
    t = time.monotonic()
    code, result, _ = run_jv("shot", "screen", "--json")
    assert code == 0 and result["say"] == "Screenshot copied." and time.monotonic() - t < 5
