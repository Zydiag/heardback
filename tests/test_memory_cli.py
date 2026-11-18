"""Typed-only manual memory commands use bounded reads and exclusive private creation."""
from concurrent.futures import ThreadPoolExecutor
import stat

from jv import memory


def test_memory_init_is_private_and_wins_concurrent_create_race(monkeypatch, tmp_path):
    config_home = tmp_path / "config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("HOME", str(tmp_path))

    def initialize():
        try:
            memory._init_file()
            return "created"
        except FileExistsError:
            return "exists"

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: initialize(), range(8)))
    path = config_home / "heardback/memory.md"
    assert results.count("created") == 1 and results.count("exists") == 7
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(config_home.stat().st_mode) == 0o700


def test_memory_init_command_never_overwrites(capsys, monkeypatch, tmp_path):
    path = tmp_path / "config/heardback/memory.md"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    assert memory.main(["init"]) == 0
    path.write_text("hand-edited fixture", encoding="utf-8")
    before = path.read_bytes()
    assert memory.main(["init"]) == 1
    assert path.read_bytes() == before
    assert "already exists" in capsys.readouterr().err


def test_memory_show_is_bounded_and_never_prints_invalid_or_oversize_content(capsys, monkeypatch, tmp_path):
    home = tmp_path / "config"
    folder = home / "heardback"
    folder.mkdir(parents=True)
    path = folder / "memory.md"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home))
    path.write_bytes(("\u00e9" * 4096).encode("utf-8"))
    assert memory.main(["show"]) == 0
    out = capsys.readouterr().out
    assert out.endswith("\n") and len(out[:-1].encode("utf-8")) == 8192
    path.write_bytes(b"private oversize sentinel " + b"x" * 8192)
    assert memory.main(["show"]) == 1
    assert "private oversize sentinel" not in capsys.readouterr().err
    path.write_bytes(b"private invalid sentinel:\xff")
    assert memory.main(["show"]) == 1
    assert "private invalid sentinel" not in capsys.readouterr().err
