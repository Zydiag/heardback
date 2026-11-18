import os

import pytest

from jv import wall


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".config/ml4w/settings").mkdir(parents=True)
    (tmp_path / ".config/ml4w/settings/wallpaper-folder").write_text("$HOME/walls\n")
    (tmp_path / "walls").mkdir()
    for n in ("a.jpg", "b.png", "c.png", "notes.txt"):
        (tmp_path / "walls" / n).write_text("x")
    (tmp_path / ".cache/ml4w/hyprland-dotfiles").mkdir(parents=True)
    (tmp_path / ".cache/ml4w/hyprland-dotfiles/current_wallpaper").write_text(str(tmp_path / "walls/b.png"))
    scripts = tmp_path / ".config/ml4w/scripts"
    scripts.mkdir()
    for name in ("ml4w-wallpaper", "ml4w-wallpaper-app"):
        (scripts / name).write_text(f'#!/bin/sh\necho {name} "$@" >> "$HOME/calls.txt"\n')
        (scripts / name).chmod(0o755)
    return tmp_path


def test_paths_follow_home_at_call_time(fake_home):
    assert wall._p(wall.SCRIPT) == fake_home / ".config/ml4w/scripts/ml4w-wallpaper"
    assert wall.folder() == fake_home / "walls"


def test_images_sorted_and_filtered(fake_home):
    assert [p.name for p in wall.images()] == ["a.jpg", "b.png", "c.png"]


def test_empty_folder(fake_home, run_jv):
    for p in (fake_home / "walls").iterdir():
        p.unlink()
    assert run_jv("wall", "next")[0] == 1


def test_current(fake_home, run_jv):
    out = run_jv("wall", "current", "--json")[1]
    assert out["name"] == "b" and out["say"] == "wallpaper is b"


def test_next(fake_home, run_jv):
    run_jv("wall", "next")
    assert (fake_home / "calls.txt").read_text().split()[-1].endswith("c.png")


def test_next_wraps(fake_home, run_jv):
    (fake_home / ".cache/ml4w/hyprland-dotfiles/current_wallpaper").write_text(str(fake_home / "walls/c.png"))
    run_jv("wall", "next")
    assert (fake_home / "calls.txt").read_text().split()[-1].endswith("a.jpg")


def test_next_no_current(fake_home, run_jv):
    (fake_home / ".cache/ml4w/hyprland-dotfiles/current_wallpaper").unlink()
    run_jv("wall", "next")
    assert (fake_home / "calls.txt").read_text().split()[-1].endswith("a.jpg")


def test_set_fuzzy(fake_home, run_jv):
    assert run_jv("wall", "set", "B")[0] == 0
    assert "b.png" in (fake_home / "calls.txt").read_text()


def test_set_miss_lists_closest(fake_home, run_jv):
    code, out, _ = run_jv("wall", "set", "zzzz", "--json")
    assert code == 1 and out["closest"]
    assert not (fake_home / "calls.txt").exists()


def test_random(fake_home, run_jv):
    run_jv("wall", "random")
    assert "--random" in (fake_home / "calls.txt").read_text()


def test_script_failure(fake_home, run_jv):
    (fake_home / ".config/ml4w/scripts/ml4w-wallpaper").write_text("#!/bin/sh\nexit 3\n")
    assert run_jv("wall", "random")[0] == 1


def test_show_opens_and_checks_quickshell_state(fake_home, monkeypatch, run_jv):
    (fake_home / ".config/ml4w/settings/wallpaper-app").write_text("quickshell\n")
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return wall.subprocess.CompletedProcess(argv, 0, "true\n" if argv[-1] == "isOpen" else "", "")

    monkeypatch.setattr(wall.subprocess, "run", run)
    code, out, _ = run_jv("wall", "show", "--json")
    assert code == 0 and out["say"] == "wallpaper picker open"
    assert [argv[-1] for argv, _ in calls] == ["open", "isOpen"]
    assert all(kwargs["timeout"] == wall.PICKER_TIMEOUT for _, kwargs in calls)


def test_show_fails_without_quickshell_open_state(fake_home, monkeypatch, run_jv):
    (fake_home / ".config/ml4w/settings/wallpaper-app").write_text("quickshell\n")
    monkeypatch.setattr(wall.subprocess, "run", lambda argv, **kw:
                        wall.subprocess.CompletedProcess(argv, 0, "false\n", ""))
    code, _, err = run_jv("wall", "show")
    assert code == 1 and "did not confirm open" in err


def test_show_reports_quickshell_ipc_failure(fake_home, monkeypatch, run_jv):
    (fake_home / ".config/ml4w/settings/wallpaper-app").write_text("quickshell\n")
    monkeypatch.setattr(wall.subprocess, "run", lambda argv, **kw:
                        wall.subprocess.CompletedProcess(argv, 1, "", "IPC target missing"))
    code, _, err = run_jv("wall", "show")
    assert code == 1 and "IPC target missing" in err


def test_qs_call_adds_qt_platform_for_wayland(monkeypatch):
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-1")
    monkeypatch.delenv("QT_QPA_PLATFORM", raising=False)
    seen = {}

    def run(argv, **kwargs):
        seen.update(kwargs)
        return wall.subprocess.CompletedProcess(argv, 0, "true\n", "")

    monkeypatch.setattr(wall.subprocess, "run", run)
    assert wall._qs_call("isOpen") == "true"
    assert seen["env"]["QT_QPA_PLATFORM"] == "wayland;xcb"
    assert "QT_QPA_PLATFORM" not in os.environ


def test_qs_call_preserves_explicit_qt_platform(monkeypatch):
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-1")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    seen = {}

    def run(argv, **kwargs):
        seen.update(kwargs)
        return wall.subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(wall.subprocess, "run", run)
    wall._qs_call("open")
    assert seen["env"]["QT_QPA_PLATFORM"] == "offscreen"


def test_qs_call_does_not_add_qt_platform_without_wayland(monkeypatch):
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.delenv("QT_QPA_PLATFORM", raising=False)
    seen = {}

    def run(argv, **kwargs):
        seen.update(kwargs)
        return wall.subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(wall.subprocess, "run", run)
    wall._qs_call("open")
    assert "QT_QPA_PLATFORM" not in seen["env"]


def test_show_waypaper_keeps_launcher_and_reports_starting(fake_home, monkeypatch, run_jv):
    (fake_home / ".config/ml4w/settings/wallpaper-app").write_text("waypaper\n")
    seen = {}

    class Running:
        def wait(self, timeout):
            seen["timeout"] = timeout
            raise wall.subprocess.TimeoutExpired("waypaper", timeout)

    def popen(argv, **kwargs):
        seen.update(argv=argv, **kwargs)
        return Running()

    monkeypatch.setattr(wall.subprocess, "Popen", popen)
    code, out, _ = run_jv("wall", "show", "--json")
    assert code == 0 and out["say"] == "wallpaper picker launching"
    assert seen["argv"] == [str(fake_home / ".config/ml4w/scripts/ml4w-wallpaper-app")]
    assert seen["start_new_session"] is True and seen["timeout"] == 1


def test_stems_missing_folder_is_empty(fake_home):
    (fake_home / ".config/ml4w/settings/wallpaper-folder").unlink()
    assert wall.stems() == []


def test_apply_failure_reports_stderr(fake_home, run_jv):
    (fake_home / ".config/ml4w/scripts/ml4w-wallpaper").write_text("#!/bin/sh\necho boom >&2\nexit 3\n")
    code, _, err = run_jv("wall", "random")
    assert code == 1 and "boom" in err


def test_apply_not_blocked_by_background_child(fake_home, run_jv):
    import time
    (fake_home / ".config/ml4w/scripts/ml4w-wallpaper").write_text("#!/bin/sh\nsleep 3 &\nexit 0\n")
    t = time.monotonic()
    assert run_jv("wall", "random")[0] == 0
    assert time.monotonic() - t < 2
